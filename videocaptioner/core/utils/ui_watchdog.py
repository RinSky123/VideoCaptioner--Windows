"""主线程卡顿检测

界面"卡死"通常是 Qt 主线程被长时间阻塞。这个看门狗只做一件事：
用一个低开销定时器记录主线程被阻塞的时长，超过阈值就写入日志，
方便在出问题的电脑上定位是卡在什么场景、什么阶段。

只记录日志，不尝试自动恢复——强制恢复对未知原因更危险。
"""

import time
from typing import Optional

from PyQt5.QtCore import QObject, QTimer

from videocaptioner.core.utils.logger import setup_logger

logger = setup_logger("ui_watchdog")

# 触发告警的阻塞时长（毫秒）
STALL_WARN_MS = 1500
# 检测粒度（毫秒）；越小越精确，开销越高
_TICK_MS = 250


class UIWatchdog(QObject):
    """监测 Qt 事件循环的响应延迟"""

    def __init__(self, parent: Optional[QObject] = None, warn_ms: int = STALL_WARN_MS):
        super().__init__(parent)
        self.warn_ms = warn_ms
        self._last = time.monotonic()
        self._stalls = 0
        self._max_stall_ms = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._last = time.monotonic()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        now = time.monotonic()
        elapsed_ms = (now - self._last) * 1000
        self._last = now

        # 定时器本身有正常抖动，留出两个 tick 的余量
        threshold = max(self.warn_ms, _TICK_MS * 2)
        if elapsed_ms <= threshold:
            return

        self._stalls += 1
        self._max_stall_ms = max(self._max_stall_ms, elapsed_ms)
        logger.warning(
            "UI 卡顿: 主线程阻塞约 %d ms (累计 %d 次, 最长 %d ms)"
            % (int(elapsed_ms), self._stalls, int(self._max_stall_ms))
        )

    @property
    def stats(self) -> dict:
        return {"stalls": self._stalls, "max_stall_ms": int(self._max_stall_ms)}
