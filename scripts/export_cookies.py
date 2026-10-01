"""导出三平台 Cookie 为 GitHub Secrets 值（本地运行）。

用法: python scripts/export_cookies.py
输出每个平台的 Secret 名称与对应 JSON 值，复制到 GitHub 仓库的 Settings → Secrets。
"""
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from msgwatch.platforms.browser import dump_cookies  # noqa: E402


def read_bili() -> str:
    path = os.path.join(ROOT, "userdata", "bilibili", "cookies.json")
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return json.dumps(json.load(f), ensure_ascii=False)


async def dump(platform: str, profile: str, domain: str) -> str:
    out = os.path.join(ROOT, "data", f"cookies_{platform}.json")
    n = await dump_cookies(os.path.join(ROOT, profile), out, domain)
    with open(out, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"    ({platform}: {n} 条 Cookie)", file=sys.stderr)
    return json.dumps(data, ensure_ascii=False)


async def main() -> None:
    bili = read_bili()
    xhs = await dump("xiaohongshu", os.path.join("userdata", "xiaohongshu"), "xiaohongshu")
    dy = await dump("douyin", os.path.join("userdata", "douyin"), "douyin")

    for name, value in (
        ("MSGWATCH_BILI_COOKIES", bili),
        ("MSGWATCH_XHS_COOKIES", xhs),
        ("MSGWATCH_DY_COOKIES", dy),
    ):
        print(f"\n===== Secret 名称: {name} =====")
        print(value if value else "(空——请先在本地完成该平台登录)")

    print("\n另外还需要一个 Secret：")
    print("  MSGWATCH_WECOM_KEY = 你的企业微信群机器人 key（config.yaml 里已配的那串）")


if __name__ == "__main__":
    asyncio.run(main())
