"""抖音适配器：浏览器打开首页，模拟点击「通知」「消息」入口，拦截/解析数据。

抖音 Web 端接口带 a_bogus 签名，私信数据走 WebSocket（protobuf），均无法直接构造请求。
经实际抓包确认（2026-10）：
- 评论/回复通知：点击首页右上角「通知」铃铛后，页面发出
  GET /aweme/v1/web/notice/ （notice_list_v2，type=31 为评论/回复类）
- 私信会话：点击「消息」后，会话列表渲染在 DOM 中（[data-e2e="conversation-item"]，
  文本结构为 昵称/相对时间/最后消息），数据本体走 WebSocket 无法直接拦截。

因此本适配器采用「真实点击 + 响应拦截 + DOM 解析」组合方案。
若平台改版，用 `python -m msgwatch probe douyin` 重新抓包定位。
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import random
import re

from .base import AuthError, NetworkError, PlatformError, get_logger
from .browser import _launch, _read_json
from ..models import Interaction

log = get_logger("douyin")

NOTICE_URL_KEYWORD = "/aweme/v1/web/notice/"


class DouyinAdapter:
    name = "douyin"
    label = "抖音"

    REQUIRED_COOKIES = {"sessionid", "sessionid_ss"}

    def __init__(self, pcfg: dict, capture_dir: str = ""):
        self.cfg = pcfg
        self.monitors = pcfg.get("monitors") or {}
        self.base_url = (pcfg.get("pages") or {}).get("comments") or "https://www.douyin.com/"
        self.headless = bool(pcfg.get("headless", True))
        self.wait = float(pcfg.get("page_wait_seconds", 6))
        self.capture_dir = capture_dir

    async def fetch(self) -> list[Interaction]:
        results, items = await self.probe()
        return items

    async def probe(self) -> tuple[dict, list[Interaction]]:
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        results: dict[str, list] = {}
        items: list[Interaction] = []
        try:
            ctx = await _launch(pw, self.cfg["browser_profile"], headless=self.headless)
            try:
                from .browser import inject_cookies

                await inject_cookies(ctx, self.cfg.get("cookies_file") or "")
                names = {c["name"] for c in await ctx.cookies()}
                if not (names & self.REQUIRED_COOKIES):
                    raise AuthError(
                        "douyin 登录态缺失（未找到 sessionid Cookie），"
                        "请运行 `python -m msgwatch login douyin` 重新登录"
                    )
                page = await ctx.new_page()
                await page.goto(self.base_url, wait_until="domcontentloaded", timeout=60_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass
                await asyncio.sleep(self.wait + random.uniform(0, 1.5))

                if self.monitors.get("comments", True):
                    results["comments"] = await self._capture_notice(page)
                if self.monitors.get("dms", True):
                    results["dms"] = await self._capture_conversations(page)

                if self.capture_dir:
                    self._dump(results)
                items = self._extract(results)
            finally:
                await ctx.close()
        except AuthError:
            raise
        except PlatformError:
            raise
        except Exception as e:
            raise NetworkError(f"douyin 浏览器采集异常：{e}") from e
        finally:
            await pw.stop()
        return results, items

    # ---------- 评论/回复通知 ----------

    async def _capture_notice(self, page) -> list:
        """点击「通知」铃铛，拦截页面自己发出的 notice 接口响应。"""
        all_r: list = []

        def _on_response(r):
            all_r.append(r)

        page.on("response", _on_response)
        try:
            loc = page.get_by_text("通知", exact=True).first
            await loc.click(timeout=10_000)
        except Exception as e:
            log.warning("douyin 点击「通知」失败：%s", e)
            page.remove_listener("response", _on_response)
            return []
        await asyncio.sleep(self.wait + random.uniform(0, 1.5))
        hits = []
        for r in all_r:
            if NOTICE_URL_KEYWORD in r.url:
                c = await _read_json(r)
                if c is not None and isinstance(c.data, dict):
                    code = c.data.get("status_code")
                    msg = str(c.data.get("status_msg") or "")
                    if code not in (0, None) and "登录" in msg:
                        page.remove_listener("response", _on_response)
                        raise AuthError(
                            "douyin 登录态已失效，请运行 `python -m msgwatch login douyin` 重新登录"
                        )
                    hits.append(c)
        page.remove_listener("response", _on_response)
        log.info("douyin [comments] 拦截 notice 响应 %d 个", len(hits))
        if not hits:
            log.warning("douyin 未捕获到通知接口响应（页面结构可能已变化），本轮评论无数据")
        return hits

    def _parse_notice(self, data: dict) -> list[Interaction]:
        out: list[Interaction] = []
        for it in data.get("notice_list_v2") or []:
            if it.get("type") != 31:  # 只取评论/回复类
                continue
            c = (it.get("comment") or {}).get("comment") or {}
            user = c.get("user") or {}
            text = str(c.get("text") or "").strip() or "[图片/表情评论]"
            reply = c.get("reply_comment")
            kind = "reply" if reply else "comment"
            aweme_id = c.get("aweme_id") or ""
            link = f"https://www.douyin.com/video/{aweme_id}" if aweme_id else ""
            ts = float(it.get("create_time") or 0)
            nid = str(it.get("nid") or c.get("cid") or "")
            out.append(
                Interaction(
                    platform=self.name,
                    kind=kind,
                    sender=str(user.get("nickname") or "未知用户"),
                    summary=text,
                    link=link,
                    ts=ts,
                    dedup_key=f"dy:notice:{nid}",
                    business="评论/回复通知",
                )
            )
        return out

    # ---------- 私信会话 ----------

    async def _capture_conversations(self, page) -> list[dict]:
        """点击「消息」，从面板 DOM 解析会话列表。"""
        try:
            await page.get_by_text("消息", exact=True).first.click(timeout=10_000)
        except Exception as e:
            log.warning("douyin 点击「消息」失败：%s", e)
            return []
        await asyncio.sleep(3 + random.uniform(0, 1.5))
        try:
            texts = await page.eval_on_selector_all(
                '[data-e2e="conversation-item"]',
                "els => els.map(e => (e.innerText || '').trim())",
            )
        except Exception as e:
            log.warning("douyin 会话面板解析失败：%s", e)
            texts = []
        log.info("douyin [dms] 面板会话 %d 个", len(texts))
        return [{"text": t} for t in texts if t]

    def _parse_conversations(self, rows: list[dict]) -> list[Interaction]:
        out: list[Interaction] = []
        for row in rows:
            lines = [ln.strip() for ln in str(row.get("text", "")).splitlines() if ln.strip()]
            if len(lines) < 2:
                continue
            sender = lines[0]
            content = "".join(lines[2:]) if len(lines) >= 3 else (lines[1] if len(lines) == 2 else "")
            # 行结构：昵称 / 相对时间 / 最后消息
            if len(lines) >= 3:
                content = " ".join(lines[2:])
            else:
                content = lines[1]
            if not content:
                continue
            ts = self._parse_relative_time(lines[1]) if len(lines) >= 3 else 0.0
            digest = hashlib.md5(f"{sender}|{content}".encode("utf-8", "ignore")).hexdigest()[:12]
            out.append(
                Interaction(
                    platform=self.name,
                    kind="dm",
                    sender=sender,
                    summary=content,
                    link="https://www.douyin.com/",
                    ts=ts,
                    # 会话级键：内容变化才产生新键（相对时间变化不会造成重复提醒）
                    dedup_key=f"dy:chat:{digest}",
                    business="私信",
                )
            )
        return out

    @staticmethod
    def _parse_relative_time(text: str) -> float:
        """解析面板里的相对时间（今天 12:23 / 昨天 12:23 / 3天前 / MM-DD）。"""
        now = datetime.datetime.now()
        text = (text or "").strip()
        try:
            m = re.match(r"^今天\s*(\d{1,2}):(\d{2})$", text)
            if m:
                return now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0).timestamp()
            m = re.match(r"^昨天\s*(\d{1,2}):(\d{2})$", text)
            if m:
                d = now - datetime.timedelta(days=1)
                return d.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0).timestamp()
            m = re.match(r"^(\d+)天前$", text)
            if m:
                return (now - datetime.timedelta(days=int(m.group(1)))).timestamp()
            m = re.match(r"^(\d{1,2})-(\d{2})$", text)
            if m:
                return now.replace(month=int(m.group(1)), day=int(m.group(2)), hour=12, second=0).timestamp()
        except ValueError:
            pass
        return 0.0

    # ---------- 汇总 ----------

    def _extract(self, results: dict) -> list[Interaction]:
        items: list[Interaction] = []
        seen: set[str] = set()
        for cap in results.get("comments", []):
            for it in self._parse_notice(cap.data):
                if it.dedup_key not in seen:
                    seen.add(it.dedup_key)
                    items.append(it)
        for row in results.get("dms", []):
            for it in self._parse_conversations([row]):
                if it.dedup_key not in seen:
                    seen.add(it.dedup_key)
                    items.append(it)
        return items

    def _dump(self, results: dict) -> None:
        import json
        import os
        import time as _time

        d = os.path.join(self.capture_dir)
        os.makedirs(d, exist_ok=True)
        stamp = _time.strftime("%Y%m%d_%H%M%S")
        try:
            with open(os.path.join(d, f"douyin_{stamp}.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "comments": [{"url": c.url, "data": c.data} for c in results.get("comments", [])],
                        "dms": results.get("dms", []),
                    },
                    f, ensure_ascii=False, indent=1,
                )
        except OSError as e:
            log.warning("诊断转储失败：%s", e)
