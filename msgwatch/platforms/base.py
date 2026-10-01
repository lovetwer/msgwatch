"""平台适配器公共基类与工具。"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from ..models import Interaction


class PlatformError(Exception):
    """平台侧错误的基类。"""


class AuthError(PlatformError):
    """登录态失效 / 未登录。"""


class ApiError(PlatformError):
    """接口返回错误。"""


class NetworkError(PlatformError):
    """网络异常。"""


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"platform.{name}")


def walk_dicts(obj: Any) -> Iterable[dict]:
    """深度遍历 JSON，产出其中所有 dict 节点。"""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_dicts(v)


def _get_any(d: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _flatten_text(v: Any, depth: int = 0) -> str:
    """把 content 类字段尽量转成可读文本（兼容 dict/list 嵌套）。"""
    if depth > 3:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return ""
    if isinstance(v, list):
        parts = [_flatten_text(x, depth + 1) for x in v]
        return " ".join(p for p in parts if p)
    if isinstance(v, dict):
        for k in ("text", "content", "content_str", "description", "desc", "title", "message"):
            if k in v and isinstance(v[k], (str, list, dict)):
                t = _flatten_text(v[k], depth + 1)
                if t:
                    return t
    return ""


def _norm_ts(v: Any) -> float:
    """兼容秒/毫秒/字符串时间戳。"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    if n > 1e14:  # 微秒
        n /= 1_000_000
    elif n > 1e11:  # 毫秒
        n /= 1000
    if n <= 0:
        return 0.0
    return n


class GenericItemExtractor:
    """通用结构化提取器。

    小红书/抖音网页版的消息中心没有公开 API 文档，这里采用
    「按 URL 关键字过滤响应 + 按 JSON 字段特征识别条目」的通用策略：
    只要响应里存在同时具备 昵称 + 文本 + 时间 的对象，就视为一条候选消息。
    页面改版后通常无需改代码；若平台换了字段名，改本类的 KEY 配置即可。
    """

    NICK_KEYS: tuple[str, ...] = ("nickname", "nick_name", "show_name", "nickname_str")
    CONTENT_KEYS: tuple[str, ...] = ("comment_content", "content", "text", "comment_text", "message_content", "desc")
    TIME_KEYS: tuple[str, ...] = ("create_time", "created_at", "update_time", "updated_at", "time", "timestamp")
    ID_KEYS: tuple[str, ...] = ("id", "comment_id", "msg_id", "message_id", "notice_id")
    LINK_KEYS: tuple[str, ...] = ("share_url", "url", "link", "web_url", "note_url")
    # 嵌套用户对象中找昵称（如 item["user"]["nickname"]）
    NESTED_USER_KEYS: tuple[str, ...] = ("user", "user_info", "author", "sender", "from_user")
    # 这些字段非空时，评论条目视为「对评论的回复」而非新评论
    REPLY_HINT_KEYS: tuple[str, ...] = ("root_comment_id", "root_id", "reply_comment_id",
                                        "parent_comment_id", "reply_to_comment_id")

    def __init__(self, exclude_url_parts: tuple[str, ...] = ()):
        self.exclude_url_parts = exclude_url_parts

    def match_url(self, url: str) -> bool:
        return not any(p in url for p in self.exclude_url_parts)

    def sender_of(self, d: dict) -> str:
        v = _get_any(d, self.NICK_KEYS)
        if isinstance(v, str) and v.strip():
            return v.strip()
        for uk in self.NESTED_USER_KEYS:
            sub = d.get(uk)
            if isinstance(sub, dict):
                v = _get_any(sub, self.NICK_KEYS)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return ""

    def summary_of(self, d: dict) -> str:
        for k in self.CONTENT_KEYS:
            if k in d:
                t = _flatten_text(d[k])
                if t:
                    return t
        return ""

    def ts_of(self, d: dict) -> float:
        return _norm_ts(_get_any(d, self.TIME_KEYS))

    def id_of(self, d: dict) -> str:
        v = _get_any(d, self.ID_KEYS)
        return str(v) if v is not None else ""

    def link_of(self, d: dict) -> str:
        v = _get_any(d, self.LINK_KEYS)
        return v.strip() if isinstance(v, str) and v.startswith("http") else ""

    def is_candidate(self, d: dict) -> bool:
        """昵称 + 文本 + 有效时间戳 三者齐备才视为候选条目。"""
        if not self.sender_of(d):
            return False
        if not self.summary_of(d):
            return False
        ts = self.ts_of(d)
        return 1_000_000_000 < ts < 4_000_000_000  # 合理的 epoch 秒范围

    def extract(self, data: Any, url: str = "") -> list[dict]:
        out: list[dict] = []
        seen_ids: set[int] = set()
        for d in walk_dicts(data):
            if id(d) in seen_ids or not self.is_candidate(d):
                continue
            seen_ids.add(id(d))
            d["_matched_url"] = url
            out.append(d)
            if len(out) >= 50:
                break
        return out


def build_interaction(platform: str, kind: str, raw: dict, ex: GenericItemExtractor, business: str = "") -> Interaction:
    import hashlib
    import time as _time

    sender = ex.sender_of(raw) or "未知用户"
    summary = ex.summary_of(raw)
    ts = ex.ts_of(raw) or _time.time()
    link = ex.link_of(raw)
    rid = ex.id_of(raw)
    # 评论类条目若带有根评论 id，说明它是对评论的回复
    if kind == "comment":
        for k in getattr(ex, "REPLY_HINT_KEYS", ()):
            if raw.get(k):
                kind = "reply"
                break
    digest = hashlib.md5(f"{sender}|{summary}|{ts}".encode("utf-8", "ignore")).hexdigest()[:12]
    # 键不含 kind：同一条数据被评论页/私信页重复捕获时全局去重
    dedup_key = f"{platform}:{rid or digest}"
    return Interaction(
        platform=platform,
        kind=kind,
        sender=sender,
        summary=summary,
        link=link,
        ts=ts,
        dedup_key=dedup_key,
        business=business,
    )
