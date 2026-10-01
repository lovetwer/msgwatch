"""临时诊断：找小红书网页版消息中心的真实入口。

打开 explore 页 → 确认登录态 → 点击「消息」 → 记录跳转 URL 与全部 API 请求。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msgwatch.platforms.browser import _launch, _read_json  # noqa: E402

OUT = os.path.join("data", "debug", "xiaohongshu_diag.json")


async def main() -> None:
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        ctx = await _launch(pw, "userdata/xiaohongshu", headless=True)
        page = await ctx.new_page()
        all_responses: list = []
        page.on("response", lambda r: all_responses.append(r))

        await page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=45_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await asyncio.sleep(4)

        base = len(all_responses)
        # 确认登录态
        for r in all_responses[:base]:
            if "/api/sns/web/v2/user/me" in r.url:
                c = await _read_json(r)
                if c and c.data:
                    nick = (c.data.get("data") or {}).get("nickname")
                    print("登录态 user/me:", c.data.get("code"), nick)

        # 找「消息」入口
        candidates = page.locator("text=消息")
        n = await candidates.count()
        print(f"找到含『消息』的元素 {n} 个")
        clicked = False
        for i in range(min(n, 6)):
            el = candidates.nth(i)
            try:
                if not await el.is_visible():
                    continue
                txt = (await el.inner_text()).strip()
                if txt in ("消息", "消息中心"):
                    print(f"点击第 {i} 个元素: {txt!r}")
                    await el.click()
                    clicked = True
                    break
            except Exception as e:
                print(f"元素 {i} 不可点: {e}")
        if not clicked:
            # 兜底：找链接 href 含 message/im/chat
            for kw in ("message", "/im", "chat"):
                link = page.locator(f'a[href*="{kw}"]').first
                if await link.count():
                    href = await link.get_attribute("href")
                    print(f"找到链接 href={href}，直接导航")
                    await page.goto(f"https://www.xiaohongshu.com{href}" if href.startswith("/") else href,
                                    wait_until="domcontentloaded")
                    clicked = True
                    break
        if not clicked:
            print("!! 没找到消息入口，保存当前页面所有链接供分析")
            hrefs = await page.eval_on_selector_all("a", "els => els.map(e => e.getAttribute('href'))")
            print([h for h in hrefs if h][:60])

        await asyncio.sleep(6)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        print("点击后 URL:", page.url)
        seen = []
        for r in all_responses[base:]:
            u = r.url
            if "edith.xiaohongshu.com/api" in u or "www.xiaohongshu.com/api" in u:
                seen.append(u)
        print(f"点击后新增业务 API {len(seen)} 个:")
        for u in seen[:40]:
            print("  ", u)

        # 保存响应体
        out = []
        for r in all_responses[base:]:
            c = await _read_json(r)
            if c is not None and ("edith" in c.url or "/api/" in c.url):
                out.append({"url": c.url, "data": c.data})
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("已保存", OUT)
        await ctx.close()
    finally:
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
