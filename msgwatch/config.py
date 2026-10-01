"""配置加载、默认值合并与校验。"""
from __future__ import annotations

import copy
import os
import sys

import yaml

DEFAULTS: dict = {
    "general": {
        "data_dir": "./data",
        "log_level": "INFO",
        "log_file": "./logs/msgwatch.log",
        "log_max_mb": 5,
        "log_backup_count": 3,
        # 首次运行时把已存在的历史消息记为“已见”，避免推送风暴
        "seed_on_first_run": True,
        # 超过该小时数的旧消息不再推送（防止补抓历史数据刷屏）
        "max_age_hours": 48,
        # 连续失败 N 次后告警
        "alert_fail_threshold": 3,
        # 同一告警的冷却时间（分钟）
        "alert_cooldown_minutes": 360,
        # 一次轮询最多逐条推送的数量，超出则合并为一条摘要
        "max_items_per_notify": 8,
    },
    "notify": {
        "channels": [],
    },
    "platforms": {
        "bilibili": {
            "enabled": False,
            "interval_seconds": 180,
            "monitors": {"comments": True, "dms": True},
            "cookie": "",
            "cookies_file": "./userdata/bilibili/cookies.json",
        },
        "xiaohongshu": {
            "enabled": False,
            "interval_seconds": 600,
            "monitors": {"comments": True, "dms": True},
            "browser_profile": "./userdata/xiaohongshu",
            "headless": True,
            "page_wait_seconds": 6,
            "pages": {
                "comments": "https://www.xiaohongshu.com/notification",
                "dms": "https://www.xiaohongshu.com/chat",
            },
        },
        "douyin": {
            "enabled": False,
            "interval_seconds": 600,
            "monitors": {"comments": True, "dms": True},
            "browser_profile": "./userdata/douyin",
            "headless": True,
            "page_wait_seconds": 6,
            "pages": {
                "comments": "https://www.douyin.com/",
                "dms": "https://www.douyin.com/",
            },
        },
    },
}

# 各通知渠道的必填字段
CHANNEL_REQUIRED = {
    "serverchan": ["sendkey"],
    "pushplus": ["token"],
    "wxpusher": ["app_token", "uids"],
    "wecom_app": ["corpid", "corpsecret", "agentid"],
    "wecom_webhook": [],  # key 与 url 二选一，见下方专项校验
    "email": ["smtp_host", "username", "password", "to"],
}


class ConfigError(Exception):
    pass


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_rel(path: str, base_dir: str) -> str:
    """把配置里的相对路径解析到 config.yaml 所在目录。"""
    if not isinstance(path, str) or not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(base_dir, path))


def _substitute_env(obj):
    """递归替换字符串里的 ${VAR} 占位符为环境变量值（CI 部署用，密钥不进仓库）。"""
    import re
    if isinstance(obj, dict):
        return {k: _substitute_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_env(v) for v in obj]
    if isinstance(obj, str):
        return re.sub(r"\$\{([A-Za-z0-9_]+)\}", lambda m: os.environ.get(m.group(1), ""), obj)
    return obj


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise ConfigError(f"配置文件不存在：{path}\n请先运行 `python -m msgwatch init` 生成并编辑 config.yaml")
    with open(path, "r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}
    if not isinstance(user_cfg, dict):
        raise ConfigError("配置文件格式错误：顶层必须是 YAML 映射")

    user_cfg = _substitute_env(user_cfg)
    cfg = _deep_merge(DEFAULTS, user_cfg)
    base_dir = os.path.dirname(os.path.abspath(path))

    # 相对路径统一解析到配置文件所在目录
    g = cfg["general"]
    for key in ("data_dir", "log_file"):
        g[key] = _resolve_rel(g[key], base_dir)
    platforms = cfg["platforms"]
    platforms["bilibili"]["cookies_file"] = _resolve_rel(platforms["bilibili"]["cookies_file"], base_dir)
    for name in ("xiaohongshu", "douyin"):
        p = platforms[name]
        p["browser_profile"] = _resolve_rel(p["browser_profile"], base_dir)
        pages = p.get("pages") or {}
        if pages.get("comments") and not str(pages["comments"]).startswith("http"):
            raise ConfigError(f"{name}.pages.comments 必须是完整 URL")
        if pages.get("dms") and not str(pages["dms"]).startswith("http"):
            raise ConfigError(f"{name}.pages.dms 必须是完整 URL")

    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    errors: list[str] = []
    warnings: list[str] = []

    # 通知渠道
    channels = cfg["notify"].get("channels") or []
    seen_types: set[str] = set()
    for i, ch in enumerate(channels):
        if not isinstance(ch, dict) or "type" not in ch:
            errors.append(f"notify.channels[{i}] 缺少 type 字段")
            continue
        t = ch["type"]
        if t not in CHANNEL_REQUIRED:
            errors.append(f"notify.channels[{i}].type 未知：{t}（可选：{', '.join(CHANNEL_REQUIRED)}）")
            continue
        if t in seen_types:
            warnings.append(f"notify.channels[{i}] 重复定义了 {t}")
        seen_types.add(t)
        if ch.get("enabled", False):
            for field in CHANNEL_REQUIRED[t]:
                if not ch.get(field):
                    errors.append(f"notify.channels[{i}]（{t}）已启用但缺少必填字段：{field}")
            if t == "wecom_webhook":
                if not ch.get("key") and not ch.get("url"):
                    errors.append(f"notify.channels[{i}]（wecom_webhook）key 与 url 至少填一个")
                elif ch.get("url") and "qyapi.weixin.qq.com/cgi-bin/webhook/send" not in str(ch["url"]):
                    warnings.append(f"notify.channels[{i}]（wecom_webhook）url 不是企业微信群机器人地址，请核对")
            if t == "wxpusher" and not ch.get("uids") and not ch.get("topic_ids"):
                errors.append(f"notify.channels[{i}]（wxpusher）uids 与 topic_ids 至少填一个")
            if t == "email" and ch.get("smtp_port") in (587,) and ch.get("use_ssl", False):
                warnings.append(f"notify.channels[{i}]（email）587 端口一般用 STARTTLS，建议 use_ssl: false, use_starttls: true")

    # 平台
    platforms = cfg["platforms"]
    any_enabled = False
    for name, p in platforms.items():
        if not p.get("enabled"):
            continue
        any_enabled = True
        interval = int(p.get("interval_seconds") or 0)
        if interval < 60:
            warnings.append(f"{name}.interval_seconds={interval} 过短，容易触发风控，建议 >= 120")
        if name == "bilibili":
            if not p.get("cookie") and not p.get("cookies_file"):
                errors.append("bilibili 已启用：请在 cookie 填入浏览器 Cookie（至少含 SESSDATA），或配置 cookies_file")
            elif p.get("cookie") and "SESSDATA=" not in str(p.get("cookie")):
                errors.append("bilibili.cookie 中未找到 SESSDATA=，请复制完整浏览器 Cookie")
        else:
            if not p.get("browser_profile"):
                errors.append(f"{name} 已启用但缺少 browser_profile")

    if not any_enabled:
        warnings.append("当前没有启用任何平台（platforms.*.enabled: true）")
    if not any(c.get("enabled") for c in channels):
        warnings.append("当前没有启用任何通知渠道（notify.channels.*.enabled: true），消息将只记录到日志")

    for w in warnings:
        print("[配置警告] " + w, file=sys.stderr)
    if errors:
        raise ConfigError("配置有误：\n  - " + "\n  - ".join(errors))


def example_config_text() -> str:
    """生成带完整注释的示例配置。"""
    example_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.yaml")
    with open(example_path, "r", encoding="utf-8") as f:
        return f.read()
