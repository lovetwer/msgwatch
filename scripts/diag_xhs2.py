"""临时诊断2：小红书 /chat 页内的评论/互动通知标签页接口。"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msgwatch.platforms.browser import _launch, _read_json  # noqa: E402

OUT = os.path.join("data", "debug", "xiaohongshu_diag2.json")


async def main() -> None:
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        ctx = await _launch(pw, "userdata/xiaohongshu", headless=True)
        page = await ctx.new_page()
        all_r: list = []
        page.on("response", lambda r: all_r.append(r))

        await page.goto("https://www.xiaohongshu.com/chat", wait_until="domcontentloaded", timeout=45_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await asyncio.sleep(5)
        print("页面URL:", page.url)
        base = len(all_r)

        # 列出页面里的候选标签文本
        texts = await page.eval_on_selector_all(
            "div,span,li,tab",
            "els => els.map(e => (e.textContent||'').trim()).filter(t => t && t.length<=10)"
        )
        interesting = sorted({t for t in texts if any(k in t for k in ("评论", "赞", "关注", "私信", "通知", "@"))})
        print("候选标签文本:", interesting[:30])

        # 逐个点击含「评论」的元素
        for kw in ("评论和@", "评论和提及", "评论", "收到的评论"):
            loc = page.get_by_text(kw, exact=False)
            cnt = await loc.count()
            if cnt:
                try:
                    await loc.first.click()
                    print(f"已点击: {kw}")
                    break
                except Exception as e:
                    print(f"点击 {kw} 失败: {e}")

        await asyncio.sleep(6)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        seen = []
        for r in all_r[base:]:
            u = r.url
            if "edith.xiaohongshu.com/api" in u:
                seen.append(u)
        print(f"点击后新增业务 API {len(seen)} 个:")
        for u in seen[:30]:
            print("  ", u[:130])

        out = []
        for r in all_r[base:]:
            c = await _read_json(r)
            if c is not None and "edith" in c.url:
                out.append({"url": c.url, "data": c.data})
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("已保存", OUT)
        await ctx.close()
    finally:
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
