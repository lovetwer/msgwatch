"""主调度器：为每个平台维护独立检测周期，处理去重、退避、告警。"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time

from . import notify
from .context import set_current_store
from .log import get_logger
from .models import Interaction
from .platforms import ADAPTERS, ApiError, AuthError, NetworkError
from .store import Store

log = get_logger("scheduler")


class Scheduler:
    def __init__(self, cfg: dict, store: Store):
        self.cfg = cfg
        self.store = store
        set_current_store(store)
        self.g = cfg["general"]
        self.adapters: dict[str, object] = {}
        self.next_run: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        for name, pcfg in cfg["platforms"].items():
            if pcfg.get("enabled"):
                self.adapters[name] = ADAPTERS[name](pcfg)
                self.next_run[name] = 0.0
                self._locks[name] = asyncio.Lock()

    # ---------------- 主循环 ----------------

    async def run_forever(self) -> None:
        self._acquire_lock()
        log.info("开始监控：%s（Ctrl+C 退出）", ", ".join(self.adapters) or "无")
        self._heartbeat()
        while True:
            now = time.time()
            due = [n for n, t in self.next_run.items() if now >= t]
            if due:
                await asyncio.gather(*(self.poll(name) for name in due))
            self._heartbeat()
            await asyncio.sleep(3)

    def _acquire_lock(self) -> None:
        """单实例锁：绑定本地端口，防止重复启动导致双倍请求频率和浏览器配置冲突。"""
        import socket

        port = int(self.g.get("lock_port", 28317))
        self._lock_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._lock_sock.bind(("127.0.0.1", port))
            self._lock_sock.listen(0)
        except OSError:
            raise SystemExit(
                f"另一个 msgwatch 实例已在运行（端口 {port} 被占用）。"
                "如需重启，请先结束旧的 pythonw 进程。"
            )

    async def run_once(self, only: str | None = None) -> bool:
        """单次全量轮询（once 模式）。返回是否有平台失败。"""
        if only:
            if only not in self.adapters:
                log.error("平台 %s 未启用或不存在（当前启用：%s）", only, ", ".join(self.adapters) or "无")
                return True
            return await self.poll(only)
        results = await asyncio.gather(*(self.poll(n) for n in self.adapters))
        return any(results)

    # ---------------- 单平台轮询 ----------------

    async def poll(self, name: str) -> bool:
        """返回 True 表示本次轮询失败。"""
        async with self._locks[name]:
            adapter = self.adapters[name]
            pcfg = self.cfg["platforms"][name]
            interval = int(pcfg.get("interval_seconds", 300))
            started = time.time()
            try:
                log.info("[%s] 开始检测…", name)
                items = await adapter.fetch()
                await self._handle_items(name, items)
                self._set_ok(name)
                # 成功后按 平台间隔±10% 抖动安排下一次，防止固定频率请求
                self.next_run[name] = time.time() + interval * random.uniform(0.9, 1.1)
                log.info("[%s] 检测完成，抓到 %d 条，用时 %.1fs", name, len(items), time.time() - started)
                return False
            except AuthError as e:
                await self._handle_failure(name, e, interval, auth=True)
            except (ApiError, NetworkError) as e:
                await self._handle_failure(name, e, interval)
            except Exception as e:
                await self._handle_failure(name, e, interval, detail=f"未预期异常：{type(e).__name__}: {e}")
            return True

    async def _handle_items(self, name: str, items: list[Interaction]) -> None:
        max_age_hours = float(self.g.get("max_age_hours", 48))
        cutoff = time.time() - max_age_hours * 3600
        seen_keys: set[str] = set()
        by_monitor: dict[str, list[Interaction]] = {}
        for it in items:
            if it.ts and it.ts < cutoff:
                continue  # 太旧的直接忽略，防止补抓历史数据刷屏
            if it.dedup_key in seen_keys:
                continue
            seen_keys.add(it.dedup_key)
            self.store.set_state(it.monitor_id)  # 确保监控项行存在
            by_monitor.setdefault(it.monitor_id, []).append(it)

        # 按监控项批量过滤：保证首次基线把当前全部消息一次性记为已见
        fresh: list[Interaction] = []
        for monitor, its in by_monitor.items():
            fresh.extend(
                self.store.filter_new(monitor, its, seed_on_first_run=bool(self.g.get("seed_on_first_run", True)))
            )

        if not fresh:
            return
        fresh.sort(key=lambda i: i.ts)
        log.info("[%s] 新互动 %d 条，准备推送", name, len(fresh))
        await notify.dispatch(self.cfg, fresh)
        self.store.mark_seen([i.dedup_key for i in fresh], notified=True)

    def _set_ok(self, name: str) -> None:
        for kind in ("comment", "reply", "dm"):
            self.store.set_state(f"{name}:{kind}", fail_count=0, last_success_ts=time.time())

    async def _handle_failure(self, name: str, err: Exception, interval: int, auth: bool = False,
                              detail: str = "") -> None:
        log.error("[%s] 检测失败：%s", name, detail or err)
        max_fail = 0
        for kind in ("comment", "reply", "dm"):
            mid = f"{name}:{kind}"
            st = self.store.get_state(mid)
            fc = int(st.get("fail_count") or 0) + 1
            max_fail = max(max_fail, fc)
            self.store.set_state(mid, fail_count=fc, last_fail_ts=time.time())
        # 指数退避：失败次数越多，重试间隔越长，避免反复触发风控
        multiplier = min(2 ** max(0, max_fail - 1), int(self.g.get("max_backoff_multiplier", 6)))
        delay = interval * multiplier * random.uniform(0.9, 1.1)
        self.next_run[name] = time.time() + delay
        log.warning("[%s] 将在 %.0f 秒后重试（退避 x%d）", name, delay, multiplier)

        threshold = int(self.g.get("alert_fail_threshold", 3))
        if auth:
            await self._alert_once(name, "登录态失效", str(detail or err))
        elif max_fail >= threshold:
            await self._alert_once(
                name, f"连续 {max_fail} 次检测失败", detail or str(err)
            )

    async def _alert_once(self, name: str, title: str, detail: str) -> None:
        """告警带冷却：同一平台在冷却期内不重复推送告警。"""
        cooldown = float(self.g.get("alert_cooldown_minutes", 360)) * 60
        st = self.store.get_state(f"{name}:comment")
        last = float(st.get("last_alert_ts") or 0)
        now = time.time()
        if now - last < cooldown:
            log.info("[%s] 告警冷却中，跳过推送：%s", name, title)
            return
        self.store.set_state(f"{name}:comment", last_alert_ts=now)
        body = f"**【{name}】{title}**\n\n{detail}\n\n请检查日志并运行 `python -m msgwatch status` 查看状态"
        log.error("[%s] 发送告警：%s", name, title)
        asyncio.create_task(notify.send_alert(self.cfg, f"【msgwatch告警】{name}", body))

    # ---------------- 心跳 ----------------

    def _heartbeat(self) -> None:
        try:
            data_dir = self.g["data_dir"]
            os.makedirs(data_dir, exist_ok=True)
            platforms = {}
            for name in self.adapters:
                st = self.store.get_state(f"{name}:comment") or {}
                platforms[name] = {
                    "last_success_ts": st.get("last_success_ts"),
                    "last_fail_ts": st.get("last_fail_ts"),
                    "fail_count": st.get("fail_count", 0),
                    "next_run_in": max(0, int(self.next_run.get(name, 0) - time.time())),
                }
            with open(os.path.join(data_dir, "heartbeat.json"), "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "platforms": platforms}, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
