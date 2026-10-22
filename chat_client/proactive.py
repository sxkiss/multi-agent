"""
@input: json, os, random, threading, time, typing
@position: Proactive layer — 按活跃时段+间隔主动推送（提醒/关怀）
@auto-doc: Update header and folder INDEX.md when this file changes

（picker.py 打分选片段 + scheduler.py 活跃时段+随机间隔 + service.py 推送）。

bt 此前只有被动响应。本模块提供轻量主动关怀：
- 活跃时段：仅在配置的时间窗内触发（如 09:00-21:00）
- 随机间隔：每次触发后按 [min,max] 随机等待，避免机械感
- 回调推送：触发时调用注入的 push 回调（如发微信）
- 开关：默认关闭（config.proactive_enabled），需显式开启

设计取舍：用 EpisodePicker 按情绪/时间衰减从记忆里选片段；bt 简化为
「按预设话题池随机选 + 回调推送」，够单用户场景用。
"""
import logging
import random
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ProactiveService:
    def __init__(self, config: dict[str, Any], push: Callable[[str], Any] | None = None):
        self.config = config or {}
        self.push = push
        self.enabled = bool(self.config.get("proactive_enabled", False))
        self.start_hour = int(self.config.get("proactive_start_hour", 9))
        self.end_hour = int(self.config.get("proactive_end_hour", 21))
        self.min_interval = int(self.config.get("proactive_min_interval", 3600))
        self.max_interval = int(self.config.get("proactive_max_interval", 14400))
        self.topics: list[str] = list(self.config.get("proactive_topics") or [])
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_fire = 0.0
        self._next_at = 0.0

    def _in_active_window(self, now: float | None = None) -> bool:
        h = time.localtime(now or time.time()).tm_hour
        if self.start_hour <= self.end_hour:
            return self.start_hour <= h < self.end_hour
        return h >= self.start_hour or h < self.end_hour  # 跨午夜

    def _next_interval(self) -> int:
        lo = min(self.min_interval, self.max_interval)
        hi = max(self.min_interval, self.max_interval)
        return random.randint(lo, hi)

    def start(self) -> bool:
        """启动后台线程。返回是否实际启动。"""
        if not self.enabled:
            logger.info("[Proactive] 未启用（config.proactive_enabled=false）")
            return False
        if not self.topics:
            logger.warning("[Proactive] 无话题池（config.proactive_topics），不启动")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._next_at = time.time() + self._next_interval()
        self._thread = threading.Thread(target=self._loop, name="proactive", daemon=True)
        self._thread.start()
        logger.info("[Proactive] 已启动（活跃时段 %d:00-%d:00）", self.start_hour, self.end_hour)
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(30)
            now = time.time()
            if now < self._next_at:
                continue
            if not self._in_active_window(now):
                self._next_at = now + 300  # 非活跃时段，5 分钟后再看
                continue
            topic = random.choice(self.topics)
            try:
                if self.push:
                    self.push(topic)
                self._last_fire = now
            except Exception as e:
                logger.warning("[Proactive] 推送失败: %s", e)
            self._next_at = now + self._next_interval()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "active_window": f"{self.start_hour}:00-{self.end_hour}:00",
            "in_window_now": self._in_active_window(),
            "topics": len(self.topics),
            "last_fire": self._last_fire,
            "next_at": self._next_at,
        }

    def stop(self) -> None:
        self._stop.set()
