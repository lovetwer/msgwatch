"""Playwright 浏览器采集：持久化配置 + 页面导航 + XHR 响应拦截。

用于小红书/抖音这类没有公开 API、前端请求自带签名的平台：
不自己构造接口请求，而是真实打开消息中心页面，拦截页面自己发出的
JSON 响应再解析。抗签名/加密变更的能力最强。

诊断模式（capture_dir 非空）会把页面发过的全部 JSON 响应落盘，
便于排查「页面实际调用了哪些接口」。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from dataclasses import dataclass, field

from .base import AuthError, NetworkError, get_logger

log = get_logger("browser")

REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""

# 单页最多解析响应体的数量上限（防止诊断模式解析几百个静态请求）
_MAX_JSON_PER_PAGE = 120


@dataclass
class CapturedResponse:
    url: str
    data: object = None
    error: str = ""


@dataclass
class PageSpec:
    kind: str          # comments / dms
    url: str
    url_keywords: tuple[str, ...]   # 响应 URL 至少包含其一才进入解析
    wait_seconds: float = 6.0


@dataclass
class PageCapture:
    """单个页面的采集结果：hits 供解析；all_json 为诊断用全量。"""
    spec: PageSpec
    hits: list[CapturedResponse] = field(default_factory=list)
    all_json: list[CapturedResponse] = field(default_factory=list)


_SKIP_URL_PARTS = ("poll", "stream", "/ws", "event", "trace", "report", "telemetry")


async def _read_json(resp) -> CapturedResponse | None:
    try:
        url = resp.url
        ct = (resp.headers or {}).get("content-type", "")
        if "json" not in ct or any(p in url for p in _SKIP_URL_PARTS):
            return None
        # 长连接/SSE 的 body 永远读不完，必须限时
        return CapturedResponse(url=url, data=await asyncio.wait_for(resp.json(), timeout=5))
    except asyncio.TimeoutError:
        return None
    except Exception as e:
        return CapturedResponse(url=getattr(resp, "url", ""), error=str(e))


def _url_match(url: str, keywords: tuple[str, ...]) -> bool:
    return any(k in url for k in keywords)


async def inject_cookies(ctx, cookies_file: str) -> None:
    """把导出的 Cookie JSON 注入浏览器上下文（CI 无持久化登录配置时使用）。"""
    if not cookies_file or not os.path.exists(cookies_file):
        return
    import json as _json

    with open(cookies_file, "r", encoding="utf-8") as f:
        cookies = _json.load(f)
    if isinstance(cookies, list) and cookies:
        await ctx.add_cookies(cookies)
        log.info("已注入 %d 条 Cookie（%s）", len(cookies), cookies_file)


async def _launch(pw, profile_dir: str, headless: bool):
    os.makedirs(profile_dir, exist_ok=True)
    return await pw.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        headless=headless,
        user_agent=REAL_UA,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport={"width": 1380, "height": 860},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
        ],
    )


async def check_login(profile_dir: str, required_cookies: set[str], headless: bool = True) -> bool:
    """打开一个空白页检查持久化配置里的登录 Cookie 是否存在。"""
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        ctx = await _launch(pw, profile_dir, headless)
        try:
            names = {c["name"] for c in await ctx.cookies()}
            return bool(names & required_cookies)
        finally:
            await ctx.close()
    finally:
        await pw.stop()


async def capture_pages(
    platform: str,
    profile_dir: str,
    specs: list[PageSpec],
    required_cookies: set[str],
    headless: bool = True,
    capture_dir: str = "",
    cookies_file: str = "",
) -> dict[str, list[CapturedResponse]]:
    """依次打开各页面并收集命中的 JSON 响应。返回 {kind: [CapturedResponse]}。

    cookies_file 非空时先注入导出的 Cookie（CI 环境：无持久化登录配置）。
    抛出 AuthError（未登录）或 NetworkError（页面加载失败等）。
    """
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    page_captures: list[PageCapture] = []
    try:
        ctx = await _launch(pw, profile_dir, headless)
        try:
            await inject_cookies(ctx, cookies_file)
            # 登录态预检查
            names = {c["name"] for c in await ctx.cookies()}
            if not (names & required_cookies):
                raise AuthError(
                    f"{platform} 登录态缺失（未找到 {', '.join(sorted(required_cookies))} Cookie），"
                    f"请运行 `python -m msgwatch login {platform}` 重新登录"
                )

            page = await ctx.new_page()
            await page.add_init_script(STEALTH_JS)

            # 全局监听：记录本页所有响应对象（是否 JSON 在读取时判断）
            all_responses: list = []
            page.on("response", lambda resp: all_responses.append(resp))

            for spec in specs:
                start = len(all_responses)
                final_url = ""
                for attempt in (1, 2):
                    try:
                        await page.goto(spec.url, wait_until="domcontentloaded", timeout=45_000)
                        final_url = page.url
                        break
                    except Exception as e:
                        if attempt == 2:
                            log.warning("%s 打开页面失败(%s)：%s", platform, spec.url, e)
                        else:
                            # 国际网络偶发超时，重试一次
                            await asyncio.sleep(5)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass  # 消息页常有长连接，networkidle 可能永不触发

                # 滚动触发懒加载
                for _ in range(2):
                    await page.mouse.wheel(0, 900)
                    await asyncio.sleep(random.uniform(0.8, 1.5))

                await asyncio.sleep(spec.wait_seconds + random.uniform(0, 1.5))

                cap = PageCapture(spec=spec)
                # 登录页重定向检测：被踢到登录页说明登录态失效，报错而不是静默抓空
                low = (final_url or "").lower()
                if any(m in low for m in ("/login", "passport", "sign_in", "signin", "need_login")):
                    raise AuthError(
                        f"{platform} 登录态已失效（页面被重定向到 {final_url}），"
                        f"请运行 `python -m msgwatch login {platform}` 重新登录"
                    )
                for resp in all_responses[start:]:
                    if len(cap.all_json) >= _MAX_JSON_PER_PAGE:
                        break
                    c = await _read_json(resp)
                    if c is None:
                        continue
                    cap.all_json.append(c)
                    if _url_match(c.url, spec.url_keywords):
                        cap.hits.append(c)
                page_captures.append(cap)
                if final_url and final_url != spec.url:
                    log.info("%s [%s] 页面跳转到 %s", platform, spec.kind, final_url)
                log.info(
                    "%s [%s] JSON 响应 %d 个，其中命中关键字 %d 个（%s）",
                    platform, spec.kind, len(cap.all_json), len(cap.hits), spec.url,
                )
        finally:
            await ctx.close()
    except AuthError:
        raise
    except Exception as e:
        raise NetworkError(f"{platform} 浏览器采集异常：{e}") from e
    finally:
        await pw.stop()

    if capture_dir:
        _dump_captures(platform, page_captures, capture_dir)
    return {cap.spec.kind: cap.hits for cap in page_captures}


def _dump_captures(platform: str, page_captures: list[PageCapture], capture_dir: str) -> None:
    os.makedirs(capture_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for cap in page_captures:
        path = os.path.join(capture_dir, f"{platform}_{cap.spec.kind}_{stamp}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "page_url": cap.spec.url,
                        "kind": cap.spec.kind,
                        "responses": [{"url": c.url, "data": c.data} for c in cap.all_json],
                    },
                    f, ensure_ascii=False, indent=1,
                )
        except (OSError, TypeError, ValueError) as e:
            log.warning("诊断转储失败 %s：%s", path, e)


async def login_flow(
    platform: str,
    profile_dir: str,
    login_url: str,
    required_cookies: set[str],
    timeout_seconds: int = 300,
    extra_hint: str = "",
    verify=None,
) -> bool:
    """打开有头浏览器让用户手动登录，轮询 Cookie 直到登录成功。

    verify: 可选的异步回调(page) -> bool。某些平台（如小红书）给未登录访客也发
    同名会话 Cookie，此时必须用页面内 fetch 调用户信息接口做二次确认。
    """
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        ctx = await _launch(pw, profile_dir, headless=False)
        page = await ctx.new_page()
        await page.goto(login_url, wait_until="domcontentloaded", timeout=45_000)
        print(f"\n请在打开的浏览器窗口中登录 {platform}（超时 {timeout_seconds} 秒）。{extra_hint}")
        deadline = time.time() + timeout_seconds
        last_hint = 0.0
        while time.time() < deadline:
            names = {c["name"] for c in await ctx.cookies()}
            if names & required_cookies:
                if verify is None:
                    print(f"√ 检测到登录态（{', '.join(sorted(names & required_cookies))}），已保存到浏览器配置目录。")
                    return True
                try:
                    ok = await verify(page)
                except Exception:
                    ok = False
                if ok:
                    print("√ 登录已通过接口验证，登录态已保存到浏览器配置目录。")
                    return True
                if time.time() - last_hint > 30:
                    last_hint = time.time()
                    print("… Cookie 已出现但登录验证未通过（可能是游客会话），请继续完成登录 …")
            await asyncio.sleep(3)
        print("× 等待登录超时。已保留当前浏览器配置，可重新运行本命令继续。")
        return False
    finally:
        await pw.stop()


# 各站点「已登录」的页面内验证：导航到目标页，若停留在登录页则视为未登录。
# 不用页面内 fetch（部分站点的安全组件会拦截脚本发起的请求）。
async def verify_xhs(page) -> bool:
    try:
        await page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2)
        return "/login" not in page.url
    except Exception:
        return False


async def dump_cookies(profile_dir: str, out_path: str, domain_contains: str = "") -> int:
    """把持久化配置里的 Cookie 导出到 JSON 文件（供 B站 HTTP 适配器使用）。"""
    import json as _json

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        ctx = await _launch(pw, profile_dir, headless=True)
        try:
            cookies = [c for c in await ctx.cookies() if domain_contains in (c.get("domain") or "")]
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                _json.dump(cookies, f, ensure_ascii=False, indent=1)
            return len(cookies)
        finally:
            await ctx.close()
    finally:
        await pw.stop()
