"""哔哩哔哩适配器：直连官方 Web API（Cookie 认证）。

依据 bilibili-API-collect 社区文档实现：
- 回复我的（评论/评论回复）: GET https://api.bilibili.com/x/msgfeed/reply
- 私信会话列表: GET https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions
- 登录检查: GET https://api.bilibili.com/x/web-interface/nav
- 用户昵称: GET https://api.bilibili.com/x/web-interface/card?mid=...
"""
from __future__ import annotations

import asyncio
import json
import os

import httpx

from ..models import Interaction
from .base import ApiError, AuthError, NetworkError, get_logger

log = get_logger("bilibili")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

BUSINESS_BY_ID = {1: "视频评论", 11: "动态评论", 12: "专栏评论", 17: "动态"}

DM_TYPE_LABELS = {
    2: "[图片]",
    5: "[撤回消息]",
    6: "[表情]",
    7: "[分享]",
    9: "[小程序]",
    10: "[通知消息]",
    11: "[视频推送]",
    12: "[专栏推送]",
    13: "[图片卡片]",
    14: "[分享]",
    16: "[自动回复]",
    18: "[系统提示]",
    19: "[AI消息]",
}


class BilibiliAdapter:
    name = "bilibili"
    label = "B站"

    def __init__(self, pcfg: dict):
        self.cfg = pcfg
        self.monitors = pcfg.get("monitors") or {}
        self._cookie_str: str = ""
        self._load_cookies()

    # ---------- Cookie ----------

    def _load_cookies(self) -> None:
        pairs: dict[str, str] = {}
        cf = self.cfg.get("cookies_file")
        if cf and os.path.exists(cf):
            try:
                with open(cf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for c in data:
                    if isinstance(c, dict) and "bilibili" in (c.get("domain") or ""):
                        pairs[c["name"]] = c.get("value", "")
            except (OSError, ValueError, KeyError) as e:
                log.warning("读取 cookies_file 失败：%s", e)
        raw = str(self.cfg.get("cookie") or "").strip()
        if raw:
            for part in raw.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    pairs[k.strip()] = v.strip()
        self._cookie_str = "; ".join(f"{k}={v}" for k, v in pairs.items() if v)

    # ---------- HTTP 基础 ----------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={
                "User-Agent": UA,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Cookie": self._cookie_str,
            },
            follow_redirects=True,
        )

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict:
        try:
            r = await client.get(url, params=params)
        except httpx.HTTPError as e:
            raise NetworkError(f"B站网络异常：{e}") from e
        if r.status_code in (301, 302) or r.status_code == 412:
            raise ApiError(f"B站接口被拦截：HTTP {r.status_code}（可能触发风控，建议调大检测间隔）")
        try:
            j = r.json()
        except ValueError as e:
            raise ApiError(f"B站接口返回非 JSON（HTTP {r.status_code}）") from e
        code = j.get("code")
        if code == -101:
            raise AuthError("B站登录态已失效（SESSDATA 过期），请运行 `python -m msgwatch login bilibili` 重新登录")
        if code not in (0, None):
            raise ApiError(f"B站接口错误 code={code}: {j.get('message')}")
        return j.get("data") or {}

    # ---------- 各监控项 ----------

    async def fetch(self) -> list[Interaction]:
        items: list[Interaction] = []
        async with self._client() as client:
            nav = await self._get(client, "https://api.bilibili.com/x/web-interface/nav")
            if not nav.get("isLogin"):
                raise AuthError("B站未登录，请运行 `python -m msgwatch login bilibili` 或检查 cookie 配置")
            me = nav.get("mid")

            if self.monitors.get("comments", True):
                feed = await self._get(
                    client,
                    "https://api.bilibili.com/x/msgfeed/reply",
                    params={"build": 0, "mobi_app": "web"},
                )
                items.extend(self._parse_reply_feed(feed))
            if self.monitors.get("dms", True):
                sessions = await self._get(
                    client,
                    "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions",
                    params={
                        "session_type": 1,
                        "group_fold": 1,
                        "unfollow_fold": 0,
                        "sort_rule": 2,
                        "build": 0,
                        "mobi_app": "web",
                    },
                )
                items.extend(await self._parse_sessions(client, sessions, me))
            await asyncio.sleep(1)
        return items

    def _parse_reply_feed(self, data: dict) -> list[Interaction]:
        out: list[Interaction] = []
        for it in data.get("items") or []:
            user = it.get("user") or {}
            item = it.get("item") or {}
            nid = it.get("id")
            if nid is None:
                continue
            sender = user.get("nickname") or f"UID{user.get('mid', '?')}"
            summary = item.get("source_content") or item.get("message") or item.get("title") or ""
            root_id = item.get("root_id") or 0
            kind = "reply" if root_id else "comment"
            biz_name = item.get("business") or BUSINESS_BY_ID.get(item.get("business_id"), "")
            title = (item.get("title") or "").strip()
            business = biz_name
            if title and title != summary:
                business = f"{biz_name}「{title[:30]}」" if biz_name else title[:30]
            out.append(
                Interaction(
                    platform=self.name,
                    kind=kind,
                    sender=sender,
                    summary=summary,
                    link=item.get("uri") or "",
                    ts=float(it.get("reply_time") or 0),
                    dedup_key=f"bili:{'r' if root_id else 'c'}:{nid}",
                    business=business,
                )
            )
        return out

    async def _parse_sessions(self, client: httpx.AsyncClient, data: dict, me: int | None) -> list[Interaction]:
        out: list[Interaction] = []
        for s in data.get("session_list") or []:
            if s.get("session_type") != 1:       # 只处理用户会话
                continue
            if s.get("system_msg_type"):         # 跳过系统助手会话
                continue
            lm = s.get("last_msg")
            if not lm:
                continue
            sender = lm.get("sender_uid") or 0
            if not sender or (me and sender == me):  # 自己发的消息不提醒
                continue
            if lm.get("msg_status") == 2:        # 被系统撤回并隐藏
                continue
            talker = s.get("talker_id")
            ts = float(lm.get("timestamp") or (s.get("session_ts") or 0) / 1_000_000)
            seq = lm.get("msg_key") or lm.get("msg_seqno") or int(ts)
            out.append(
                Interaction(
                    platform=self.name,
                    kind="dm",
                    sender=await self._username(client, talker),
                    summary=self._render_dm_content(lm),
                    link=f"https://message.bilibili.com/whisper/mid{talker}",
                    ts=ts,
                    dedup_key=f"bili:dm:{talker}:{seq}",
                )
            )
        return out

    def _render_dm_content(self, lm: dict) -> str:
        t = lm.get("msg_type")
        raw = lm.get("content") or ""
        try:
            c = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            c = {}
        if t in (1, 6):
            return str(c.get("content") or "").strip() or "[空消息]"
        if t == 7:
            title = c.get("title") or ""
            return f"[分享] {title}".strip()
        return DM_TYPE_LABELS.get(t, f"[消息类型{t}]")

    async def _username(self, client: httpx.AsyncClient, mid) -> str:
        from ..context import get_current_store

        store = get_current_store()
        if store:
            cached = store.get_user(self.name, mid)
            if cached:
                return cached
        try:
            data = await self._get(client, "https://api.bilibili.com/x/web-interface/card", params={"mid": mid})
            name = ((data.get("card") or {}).get("name")) or f"UID{mid}"
        except (ApiError, NetworkError):
            name = f"UID{mid}"
        if store:
            store.put_user(self.name, mid, name)
        return name
