from .base import ApiError, AuthError, NetworkError, PlatformError
from .bilibili import BilibiliAdapter
from .douyin import DouyinAdapter
from .xiaohongshu import XiaohongshuAdapter

ADAPTERS = {
    "bilibili": BilibiliAdapter,
    "xiaohongshu": XiaohongshuAdapter,
    "douyin": DouyinAdapter,
}

__all__ = [
    "ADAPTERS",
    "ApiError",
    "AuthError",
    "NetworkError",
    "PlatformError",
    "BilibiliAdapter",
    "DouyinAdapter",
    "XiaohongshuAdapter",
]
