"""统一的数据模型：一条互动消息的规范化表示。"""
from __future__ import annotations

from dataclasses import dataclass, field

# 互动类型
KIND_COMMENT = "comment"   # 新评论（对内容的直接评论）
KIND_REPLY = "reply"       # 评论下的回复
KIND_DM = "dm"             # 私信 / 消息回复

KIND_LABELS = {
    KIND_COMMENT: "新评论",
    KIND_REPLY: "评论回复",
    KIND_DM: "私信",
}

PLATFORM_LABELS = {
    "bilibili": "B站",
    "xiaohongshu": "小红书",
    "douyin": "抖音",
}


@dataclass
class Interaction:
    """一条需要推送的互动消息。"""

    platform: str        # bilibili / xiaohongshu / douyin
    kind: str            # KIND_*
    sender: str          # 对方昵称
    summary: str         # 内容摘要
    link: str            # 原文链接（尽量给出；无则为空）
    ts: float            # 发生时间 epoch 秒
    dedup_key: str       # 全局唯一去重键
    business: str = ""   # 附加来源说明，如「视频」「动态」「笔记」

    @property
    def platform_label(self) -> str:
        return PLATFORM_LABELS.get(self.platform, self.platform)

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def monitor_id(self) -> str:
        """监控项标识，用于分监控项做首次基线（seed）与失败计数。"""
        return f"{self.platform}:{self.kind}"

    def happened_at(self) -> str:
        import datetime
        return datetime.datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M")
