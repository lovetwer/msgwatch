"""进程内共享上下文：调度器把 Store 挂到这里，适配器按需取用（写昵称缓存等）。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .store import Store

_store: Optional["Store"] = None


def set_current_store(store: "Store") -> None:
    global _store
    _store = store


def get_current_store() -> Optional["Store"]:
    return _store
