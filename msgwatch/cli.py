"""命令行入口。

用法：
  python -m msgwatch init                     # 生成 config.yaml
  python -m msgwatch login <platform>         # 打开浏览器登录（bilibili/xiaohongshu/douyin）
  python -m msgwatch run                      # 常驻运行
  python -m msgwatch once [--platform X]      # 单次检测（测试用）
  python -m msgwatch probe <platform>         # 测试抓取并导出原始响应（调试用）
  python -m msgwatch test-notify              # 发送一条测试通知
  python -m msgwatch status                   # 查看监控状态
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time

from . import __version__, notify
from .config import ConfigError, load_config
from .log import get_logger, setup_logging
from .platforms import ADAPTERS
from .store import Store

log = get_logger("cli")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_config_path(args_cfg: str | None) -> str:
    if args_cfg:
        return args_cfg
    cwd_cfg = os.path.join(os.getcwd(), "config.yaml")
    if os.path.exists(cwd_cfg):
        return cwd_cfg
    return os.path.join(PROJECT_ROOT, "config.yaml")


def _fix_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _open_store(cfg: dict) -> Store:
    return Store(os.path.join(cfg["general"]["data_dir"], "state.db"))


# ---------------- 子命令 ----------------

def cmd_init(args) -> int:
    target = os.path.join(os.getcwd(), "config.yaml")
    if os.path.exists(target):
        print(f"已存在 {target}，未做任何修改。")
        return 1
    src = os.path.join(PROJECT_ROOT, "config.example.yaml")
    if os.path.exists(src):
        shutil.copyfile(src, target)
    else:
        from .config import example_config_text

        with open(target, "w", encoding="utf-8") as f:
            f.write(example_config_text())
    print(f"已生成示例配置：{target}")
    print("请编辑该文件：填写通知渠道与平台凭据，然后把要启用的平台设为 enabled: true。")
    return 0


async def cmd_run(cfg_path: str) -> int:
    cfg = load_config(cfg_path)
    setup_logging(
        cfg["general"].get("log_level", "INFO"),
        cfg["general"].get("log_file", ""),
        cfg["general"].get("log_max_mb", 5),
        cfg["general"].get("log_backup_count", 3),
    )
    from .scheduler import Scheduler

    store = _open_store(cfg)
    try:
        # 每天清理一次过期的去重记录
        store.prune(int(cfg["general"].get("dedup_window_days", 30)))
        sched = Scheduler(cfg, store)
        await sched.run_forever()
    finally:
        store.close()
        await notify.close_client()
    return 0


async def cmd_once(cfg_path: str, platform: str | None) -> int:
    cfg = load_config(cfg_path)
    setup_logging(cfg["general"].get("log_level", "INFO"), cfg["general"].get("log_file", ""))
    from .scheduler import Scheduler

    store = _open_store(cfg)
    try:
        sched = Scheduler(cfg, store)
        failed = await sched.run_once(platform)
        if failed:
            print("本轮检测存在失败项，请查看日志。")
        else:
            print("本轮检测完成。")
        return 1 if failed else 0
    finally:
        store.close()
        await notify.close_client()


async def cmd_login(cfg_path: str, platform: str, timeout: int) -> int:
    from .platforms.browser import dump_cookies, login_flow

    cfg = load_config(cfg_path)
    if platform not in cfg["platforms"]:
        print(f"未知平台：{platform}（可选 bilibili/xiaohongshu/douyin）")
        return 2
    pcfg = cfg["platforms"][platform]

    if platform == "bilibili":
        profile = os.path.join(cfg["general"]["data_dir"], "..", "userdata", "bilibili")
        profile = os.path.normpath(profile)
        ok = await login_flow(platform, profile, "https://www.bilibili.com", {"SESSDATA"}, timeout,
                              "登录完成后本窗口会自动检测到 Cookie。")
        if ok:
            out = pcfg.get("cookies_file") or os.path.join(profile, "cookies.json")
            n = await dump_cookies(profile, out, "bilibili")
            print(f"已导出 {n} 条 Cookie 到 {out}")
        return 0 if ok else 1

    if platform == "xiaohongshu":
        from .platforms.browser import verify_xhs

        ok = await login_flow(
            platform, pcfg["browser_profile"], "https://www.xiaohongshu.com/explore", {"web_session"}, timeout,
            "小红书登录成功后网页右上角会出现头像。",
            verify=verify_xhs,
        )
        return 0 if ok else 1

    if platform == "douyin":
        ok = await login_flow(
            platform, pcfg["browser_profile"], "https://www.douyin.com/", {"sessionid", "sessionid_ss"}, timeout,
            "抖音支持扫码登录。")
        return 0 if ok else 1

    return 2


async def cmd_probe(cfg_path: str, platform: str) -> int:
    from .platforms.base import PlatformError

    cfg = load_config(cfg_path)
    setup_logging(cfg["general"].get("log_level", "INFO"), "")
    if platform not in ADAPTERS:
        print(f"未知平台：{platform}")
        return 2
    pcfg = cfg["platforms"][platform]
    capture_dir = os.path.join(cfg["general"]["data_dir"], "debug", platform)
    is_browser_adapter = hasattr(ADAPTERS[platform], "probe")
    if is_browser_adapter:
        adapter = ADAPTERS[platform](pcfg, capture_dir=capture_dir)
    else:
        adapter = ADAPTERS[platform](pcfg)

    try:
        if is_browser_adapter:
            results, items = await adapter.probe()
            print(f"\n==== {platform} 抓取结果（原始响应已存到 {capture_dir}）====")
            for kind, caps in results.items():
                print(f"[{kind}] 命中响应 {len(caps)} 条：")
                for c in caps[:10]:
                    if isinstance(c, dict):
                        print(f"    {str(c)[:120]}")
                    else:
                        print(f"    {c.url[:120]}")
            print(f"\n解析出互动 {len(items)} 条：")
            for it in items[:20]:
                print(f"    [{it.kind}] {it.sender}: {it.summary[:50]} @ {it.happened_at()}")
        else:
            items = await adapter.fetch()
            print(f"解析出互动 {len(items)} 条：")
            for it in items[:20]:
                print(f"    [{it.kind}] {it.sender}: {it.summary[:50]} @ {it.happened_at()}（{it.link}）")
    except PlatformError as e:
        print(f"抓取失败：{e}", file=sys.stderr)
        return 1
    return 0


async def cmd_test_notify(cfg_path: str) -> int:
    cfg = load_config(cfg_path)
    from .models import Interaction

    now = time.time()
    items = [
        Interaction("bilibili", "comment", "测试用户A", "这是一条测试评论：恭喜发财！", "https://www.bilibili.com", now,
                    f"test:{now}"),
        Interaction("xiaohongshu", "dm", "测试用户B", "这是一条测试私信：你好呀~", "", now - 5, f"test:{now - 5}"),
    ]
    status = await notify.dispatch(cfg, items)
    await notify.close_client()
    if not status:
        print("没有启用任何通知渠道（notify.channels.*.enabled），请先配置。")
        return 1
    ok = True
    for t, s in status.items():
        mark = "√" if s == "ok" else "×"
        if s != "ok":
            ok = False
        print(f"{mark} {t}: {s}")
    return 0 if ok else 1


def cmd_status(cfg_path: str) -> int:
    cfg = load_config(cfg_path)
    print(f"msgwatch v{__version__}")
    print(f"配置文件：{cfg_path}")
    enabled = [n for n, p in cfg["platforms"].items() if p.get("enabled")]
    print(f"启用平台：{', '.join(enabled) or '无'}")
    channels = [c["type"] for c in (cfg["notify"].get("channels") or []) if c.get("enabled")]
    print(f"通知渠道：{', '.join(channels) or '无'}")

    hb = os.path.join(cfg["general"]["data_dir"], "heartbeat.json")
    if os.path.exists(hb):
        try:
            with open(hb, "r", encoding="utf-8") as f:
                data = json.load(f)
            print(f"进程心跳：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data['ts']))}")
            for name, p in (data.get("platforms") or {}).items():
                ok = p.get("last_success_ts")
                print(f"  {name}: 上次成功={time.strftime('%m-%d %H:%M', time.localtime(ok)) if ok else '从未'}"
                      f" 连续失败={p.get('fail_count', 0)} 下次检测在 {p.get('next_run_in', '?')}s 后")
        except (OSError, ValueError):
            pass
    else:
        print("进程心跳：无（run 模式运行后生成）")

    db = os.path.join(cfg["general"]["data_dir"], "state.db")
    if os.path.exists(db):
        store = Store(db)
        try:
            print("监控项状态：")
            print(store.dump_status())
        finally:
            store.close()
    return 0


# ---------------- 参数解析 ----------------

def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()
    parser = argparse.ArgumentParser(
        prog="msgwatch",
        description="个人互动消息聚合监控：B站/小红书/抖音 新评论、评论回复、私信 → 微信/邮件通知",
    )
    parser.add_argument("--version", action="version", version=f"msgwatch {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="生成示例配置 config.yaml")

    def add_cfg(p):
        p.add_argument("--config", default=None, help="配置文件路径（默认当前目录或项目根的 config.yaml）")

    p = sub.add_parser("run", help="常驻运行，按各平台配置的间隔自动检测")
    add_cfg(p)

    p = sub.add_parser("once", help="立即执行一轮检测后退出（测试用）")
    add_cfg(p)
    p.add_argument("--platform", default=None, help="只检测指定平台")

    p = sub.add_parser("login", help="打开浏览器登录指定平台，保存登录态")
    add_cfg(p)
    p.add_argument("platform", choices=["bilibili", "xiaohongshu", "douyin"])
    p.add_argument("--timeout", type=int, default=300, help="等待登录的秒数，默认 300")

    p = sub.add_parser("probe", help="抓取一次并导出原始响应 JSON（排查适配问题用）")
    add_cfg(p)
    p.add_argument("platform", choices=["bilibili", "xiaohongshu", "douyin"])

    p = sub.add_parser("test-notify", help="向所有启用渠道发送测试通知")
    add_cfg(p)

    p = sub.add_parser("status", help="查看监控状态与统计")
    add_cfg(p)

    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            return cmd_init(args)
        cfg_path = resolve_config_path(args.config)
        if args.command == "run":
            return asyncio.run(cmd_run(cfg_path))
        if args.command == "once":
            return asyncio.run(cmd_once(cfg_path, args.platform))
        if args.command == "login":
            return asyncio.run(cmd_login(cfg_path, args.platform, args.timeout))
        if args.command == "probe":
            return asyncio.run(cmd_probe(cfg_path, args.platform))
        if args.command == "test-notify":
            return asyncio.run(cmd_test_notify(cfg_path))
        if args.command == "status":
            return cmd_status(cfg_path)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已退出。")
        return 0
    return 0
