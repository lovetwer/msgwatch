"""小红书适配器：浏览器打开消息中心 + 拦截页面自身请求的 JSON 响应。

小红书 Web API 全部带 X-s/X-t/X-s-common 签名且无公开文档，直接逆向维护成本高。
本适配器不构造任何签名请求，只真实访问消息页并截获页面自己发出的响应。

经实际抓包确认（2026-10）：
- 通知中心（评论和@）: 页面 https://www.xiaohongshu.com/notification
  接口 edith.xiaohongshu.com/api/sns/web/v1/you/mentions
  条目字段: id, type(comment/item=新评论, comment/comment=评论回复), title,
            time(秒), user_info.nickname, comment_info.content, item_info.id(笔记)
- 私信会话列表: 页面 https://www.xiaohongshu.com/chat
  接口 edith.xiaohongshu.com/api/im/web/v3/chats
  条目字段: user_id, last_msg_content, last_msg_time(毫秒), info.nickname

若平台调整，用 `python -m msgwatch probe xiaohongshu` 重新抓包定位。
"""
from __future__ import annotations

import hashlib

from .base import get_logger, walk_dicts
from .browser import PageSpec, capture_pages
from ..models import Interaction

log = get_logger("xiaohongshu")


class XiaohongshuAdapter:
    name = "xiaohongshu"
    label = "小红书"

    REQUIRED_COOKIES = {"web_session"}
    # 各页面响应 URL 的精确命中关键字
    COMMENT_URL_KEYWORDS = ("you/mentions",)
    DM_URL_KEYWORDS = ("im/web/v3/chats",)

    def __init__(self, pcfg: dict, capture_dir: str = ""):
        self.cfg = pcfg
        self.monitors = pcfg.get("monitors") or {}
        self.pages = pcfg.get("pages") or {}
        self.headless = bool(pcfg.get("headless", True))
        self.wait = float(pcfg.get("page_wait_seconds", 6))
        self.capture_dir = capture_dir

    async def fetch(self) -> list[Interaction]:
        results, items = await self.probe()
        return items

    async def probe(self) -> tuple[dict, list[Interaction]]:
        specs: list[PageSpec] = []
        if self.monitors.get("comments", True):
            specs.append(PageSpec(
                "comments",
                self.pages.get("comments", "https://www.xiaohongshu.com/notification"),
                self.COMMENT_URL_KEYWORDS, self.wait,
            ))
        if self.monitors.get("dms", True):
            specs.append(PageSpec(
                "dms",
                self.pages.get("dms", "https://www.xiaohongshu.com/chat"),
                self.DM_URL_KEYWORDS, self.wait,
            ))
        specs = [s for s in specs if s.url]
        if not specs:
            return {}, []
        results = await capture_pages(
            self.name,
            self.cfg["browser_profile"],
            specs,
            self.REQUIRED_COOKIES,
            headless=self.headless,
            capture_dir=self.capture_dir,
            cookies_file=self.cfg.get("cookies_file") or "",
        )
        return results, self._extract(results)

    def _extract(self, results: dict) -> list[Interaction]:
        items: list[Interaction] = []
        seen: set[str] = set()
        for cap in results.get("comments", []):
            for raw in self._mention_items(cap.data):
                it = self._mention_to_interaction(raw)
                if it and it.dedup_key not in seen:
                    seen.add(it.dedup_key)
                    items.append(it)
        for cap in results.get("dms", []):
            for raw in self._chat_items(cap.data):
                it = self._chat_to_interaction(raw)
                if it and it.dedup_key not in seen:
                    seen.add(it.dedup_key)
                    items.append(it)
        return items

    # ---------- 评论和@ 通知 ----------

    @staticmethod
    def _mention_items(data) -> list[dict]:
        out = []
        for d in walk_dicts(data):
            if (
                isinstance(d.get("comment_info"), dict)
                and isinstance(d.get("user_info"), dict)
                and isinstance(d.get("time"), (int, float))
                and str(d.get("type", "")).startswith("comment")
            ):
                out.append(d)
        return out

    def _mention_to_interaction(self, d: dict) -> Interaction | None:
        user = d.get("user_info") or {}
        comment = d.get("comment_info") or {}
        item = d.get("item_info") or {}
        summary = str(comment.get("content") or "").strip()
        if not summary:
            return None
        # comment/item = 评论了你的笔记；comment/comment = 回复了你的评论
        kind = "reply" if "comment/comment" == d.get("type") or "回复" in str(d.get("title", "")) else "comment"
        note_id = item.get("id") or ""
        link = f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ""
        note_title = str(item.get("content") or "").strip()
        business = f"笔记「{note_title[:20]}」" if note_title else ""
        ts = float(d.get("time") or 0)
        rid = str(d.get("id") or comment.get("id") or "")
        digest = hashlib.md5(f"{user.get('nickname')}|{summary}|{ts}".encode("utf-8", "ignore")).hexdigest()[:12]
        return Interaction(
            platform=self.name,
            kind=kind,
            sender=str(user.get("nickname") or "未知用户"),
            summary=summary,
            link=link,
            ts=ts,
            dedup_key=f"xhs:notice:{rid or digest}",
            business=business,
        )

    # ---------- 私信会话 ----------

    @staticmethod
    def _chat_items(data) -> list[dict]:
        out = []
        for d in walk_dicts(data):
            if isinstance(d.get("last_msg_content"), (str, dict)) and isinstance(d.get("info"), dict):
                out.append(d)
        return out

    def _chat_to_interaction(self, d: dict) -> Interaction | None:
        info = d.get("info") or {}
        content = d.get("last_msg_content") or ""
        if isinstance(content, dict):
            content = content.get("content") or content.get("text") or "[消息]"
        content = str(content).strip()
        if not content:
            return None
        ts_ms = d.get("last_msg_time") or d.get("update_time") or 0
        ts = float(ts_ms) / 1000 if ts_ms > 1e11 else float(ts_ms or 0)
        uid = str(d.get("user_id") or info.get("userid") or "")
        digest = hashlib.md5(f"{info.get('nickname')}|{content}|{ts}".encode("utf-8", "ignore")).hexdigest()[:12]
        return Interaction(
            platform=self.name,
            kind="dm",
            sender=str(info.get("nickname") or "未知用户"),
            summary=content,
            link=f"https://www.xiaohongshu.com/chat" if uid else "",
            ts=ts,
            # 会话级键：同会话新消息会因 ts 变化生成新键，从而再次提醒
            dedup_key=f"xhs:chat:{uid}:{digest}",
            business="私信",
        )
