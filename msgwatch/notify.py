"""通知渠道：Server酱 / PushPlus / WxPusher / 企业微信自建应用 / 邮件。

所有渠道统一接收 markdown 文本；邮件渠道使用 HTML。
渠道发送失败会重试一次并记录日志，不影响其他渠道。
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

import httpx

from .models import Interaction

log = logging.getLogger("notify")

_TIMEOUT = httpx.Timeout(15.0)
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ---------------- 文案格式化 ----------------

def clip(text: str, n: int = 80) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def item_markdown(it: Interaction) -> str:
    lines = [
        f"**{it.platform_label} · {it.kind_label}**（{it.sender}）",
        f"> {clip(it.summary, 120) or '（无文字内容）'}",
        f"- 时间：{it.happened_at()}",
    ]
    if it.business:
        lines.insert(1, f"来自：{it.business}")
    if it.link:
        lines.append(f"- 链接：{it.link}")
    return "\n".join(lines)


def build_markdown(items: list[Interaction], max_items: int = 8) -> tuple[str, str]:
    """返回 (title, markdown_body)。超出 max_items 时合并为摘要。"""
    if len(items) == 1:
        it = items[0]
        return f"【{it.platform_label}·{it.kind_label}】{it.sender}", item_markdown(it)

    title = f"【互动汇总】{len(items)} 条新消息"
    parts = [item_markdown(it) for it in items[:max_items]]
    if len(items) > max_items:
        parts.append(f"……另有 {len(items) - max_items} 条，请打开对应 App 查看")
    return title, "\n\n---\n\n".join(parts)


def build_email_html(items: list[Interaction]) -> tuple[str, str]:
    if len(items) == 1:
        it = items[0]
        title = f"【{it.platform_label}·{it.kind_label}】{it.sender}"
    else:
        title = f"【互动汇总】{len(items)} 条新消息"
    rows = []
    for it in items:
        link = f'<a href="{html.escape(it.link)}">打开原文</a>' if it.link else "-"
        rows.append(
            "<tr>"
            f"<td>{html.escape(it.platform_label)}</td>"
            f"<td>{html.escape(it.kind_label)}</td>"
            f"<td>{html.escape(it.sender)}</td>"
            f"<td>{html.escape(clip(it.summary, 200))}</td>"
            f"<td>{html.escape(it.business)}</td>"
            f"<td>{it.happened_at()}</td>"
            f"<td>{link}</td>"
            "</tr>"
        )
    body = (
        "<html><body style='font-family:sans-serif'>"
        "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
        "<tr style='background:#f2f2f2'><th>平台</th><th>类型</th><th>昵称</th>"
        "<th>内容摘要</th><th>来源</th><th>时间</th><th>链接</th></tr>"
        + "".join(rows)
        + "</table>"
        "<p style='color:#888;font-size:12px'>由 msgwatch 自动发送</p></body></html>"
    )
    return title, body


# ---------------- 各渠道实现 ----------------

async def send_serverchan(ch: dict, title: str, markdown: str) -> None:
    key = ch["sendkey"]
    url = f"https://sctapi.ftqq.com/{key}.send"
    r = await _get_client().post(url, data={"title": title[:32], "desp": markdown})
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Server酱返回错误: {data}")


async def send_pushplus(ch: dict, title: str, markdown: str) -> None:
    r = await _get_client().post(
        "https://www.pushplus.plus/send",
        json={"token": ch["token"], "title": title[:100], "content": markdown, "template": "markdown"},
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 200:
        raise RuntimeError(f"PushPlus返回错误: {data}")


async def send_wxpusher(ch: dict, title: str, markdown: str) -> None:
    payload = {
        "appToken": ch["app_token"],
        "summary": title,
        "content": markdown,
        "contentType": 3,  # markdown
    }
    if ch.get("uids"):
        payload["uids"] = ch["uids"]
    if ch.get("topic_ids"):
        payload["topicIds"] = ch["topic_ids"]
    r = await _get_client().post("https://wxpusher.zjiecode.com/api/send/message", json=payload)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"WxPusher返回错误: {data}")


# 企业微信 access_token 缓存
_wecom_tokens: dict[str, tuple[str, float]] = {}


async def _wecom_token(ch: dict) -> str:
    cache_key = ch["corpid"] + ":" + str(ch["agentid"])
    hit = _wecom_tokens.get(cache_key)
    if hit and hit[1] > time.time() + 120:
        return hit[0]
    r = await _get_client().post(
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        params={"corpid": ch["corpid"], "corpsecret": ch["corpsecret"]},
    )
    r.raise_for_status()
    data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"企业微信获取token失败: {data}")
    _wecom_tokens[cache_key] = (data["access_token"], time.time() + int(data.get("expires_in", 7200)))
    return data["access_token"]


async def send_wecom_app(ch: dict, title: str, markdown: str) -> None:
    token = await _wecom_token(ch)
    payload = {
        "touser": ch.get("touser", "@all"),
        "msgtype": "markdown",
        "agentid": int(ch["agentid"]),
        "markdown": {"content": f"**{title}**\n{markdown}"},
    }
    r = await _get_client().post(
        "https://qyapi.weixin.qq.com/cgi-bin/message/send", params={"access_token": token}, json=payload
    )
    r.raise_for_status()
    data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"企业微信发送失败: {data}")


def _wecom_webhook_url(ch: dict) -> str:
    if ch.get("key"):
        return f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={ch['key']}"
    return ch["url"]


async def send_wecom_webhook(ch: dict, title: str, markdown: str) -> None:
    """企业微信群机器人 Webhook：POST JSON 即可，限频约 20 条/分钟。"""
    content = f"**{title}**\n{markdown}"
    r = await _get_client().post(
        _wecom_webhook_url(ch), json={"msgtype": "markdown", "markdown": {"content": content}}
    )
    r.raise_for_status()
    data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"企业微信机器人发送失败: {data}")


def send_email_sync(ch: dict, title: str, body_html: str) -> None:
    import smtplib
    from email.header import Header
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.utils import formataddr

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = formataddr(("msgwatch", ch["username"]))
    msg["To"] = ", ".join(ch["to"])
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    port = int(ch.get("smtp_port", 465))
    use_ssl = bool(ch.get("use_ssl", True))
    use_starttls = bool(ch.get("use_starttls", False))
    if use_ssl and not use_starttls:
        server = smtplib.SMTP_SSL(ch["smtp_host"], port, timeout=20)
    else:
        server = smtplib.SMTP(ch["smtp_host"], port, timeout=20)
        if use_starttls:
            server.starttls()
    try:
        server.login(ch["username"], ch["password"])
        server.sendmail(ch["username"], list(ch["to"]), msg.as_string())
    finally:
        server.quit()


async def send_email(ch: dict, title: str, markdown: str, body_html: str | None = None) -> None:
    await asyncio.to_thread(send_email_sync, ch, title, body_html or f"<pre>{html.escape(markdown)}</pre>")


_senders = {
    "serverchan": lambda ch, t, m, h: send_serverchan(ch, t, m),
    "pushplus": lambda ch, t, m, h: send_pushplus(ch, t, m),
    "wxpusher": lambda ch, t, m, h: send_wxpusher(ch, t, m),
    "wecom_app": lambda ch, t, m, h: send_wecom_app(ch, t, m),
    "wecom_webhook": lambda ch, t, m, h: send_wecom_webhook(ch, t, m),
    "email": lambda ch, t, m, h: send_email(ch, t, m, h),
}

# 各渠道的消息到达媒介说明（用于 README 与提示）
CHANNEL_HINTS = {
    "serverchan": "Server酱，消息发送到微信（服务号「方糖」）",
    "pushplus": "PushPlus，消息发送到微信（其服务号）",
    "wxpusher": "WxPusher，消息发送到微信（关注 WxPusher 服务号接收）",
    "wecom_app": "企业微信自建应用，消息发送到企业微信App（可选微信插件）",
    "wecom_webhook": "企业微信群机器人，消息发送到该企业微信群",
    "email": "邮件",
}


def enabled_channels(cfg: dict, only_types: list[str] | None = None) -> list[dict]:
    out = []
    for ch in cfg["notify"].get("channels") or []:
        if ch.get("enabled") and (only_types is None or ch.get("type") in only_types):
            out.append(ch)
    return out


async def dispatch(cfg: dict, items: list[Interaction], only_types: list[str] | None = None) -> dict[str, str]:
    """把一批消息推送到所有启用渠道。返回 {渠道名: 状态}。"""
    channels = enabled_channels(cfg, only_types)
    if not channels or not items:
        return {}
    title, markdown = build_markdown(items, int(cfg["general"].get("max_items_per_notify", 8)))
    return await _send_all(channels, title, markdown)


async def send_alert(cfg: dict, title: str, detail: str) -> dict[str, str]:
    channels = enabled_channels(cfg)
    if not channels:
        return {}
    return await _send_all(channels, title, detail)


async def _send_all(channels: list[dict], title: str, markdown: str) -> dict[str, str]:
    status: dict[str, str] = {}
    for ch in channels:
        t = ch["type"]
        try:
            await _senders[t](ch, title, markdown, None)
            status[t] = "ok"
            log.info("通知已通过渠道 %s 发送：%s", t, title)
        except Exception as first_err:
            log.warning("渠道 %s 发送失败(%s)，重试一次", t, first_err)
            try:
                await asyncio.sleep(3)
                await _senders[t](ch, title, markdown, None)
                status[t] = "ok"
                log.info("渠道 %s 重试成功", t)
            except Exception as e2:
                status[t] = f"失败: {e2}"
                log.error("渠道 %s 重试仍失败：%s", t, e2)
    return status
