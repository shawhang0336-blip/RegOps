"""进程内有界后台任务执行器（TaskRunner）。

统一接管此前散落在路由层的裸 `threading.Thread(..., daemon=True)`，提供：
1. 有界线程池（默认 4 并发），防止多任务同时触发时与内层并发池争抢线程与数据库连接；
2. 按任务键（如 `run:<id>`、`doc:prepare:<id>`、`doc:extract:<id>`）并发去重，杜绝重复点击并发写冲突；
3. FastAPI `lifespan` 退出时的统一关闭入口与测试态同步内联模式（`inline=True`）。
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, List, Optional, Set

logger = logging.getLogger(__name__)


class TaskRunner:
    """带任务键去重的有界后台线程池。"""

    def __init__(self, max_workers: int = 4, *, inline: bool = False) -> None:
        self._max_workers = max(1, int(max_workers))
        self._inline = inline
        self._lock = threading.Lock()
        self._active: Set[str] = set()
        self._executor: Optional[ThreadPoolExecutor] = None

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix="regops-bg",
            )
        return self._executor

    def is_active(self, key: str) -> bool:
        with self._lock:
            return key in self._active

    def active_keys(self) -> List[str]:
        with self._lock:
            return sorted(self._active)

    def submit(self, key: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        """提交后台任务。若同一 key 已在执行中则返回 False（去重跳过），否则返回 True。"""
        with self._lock:
            if key in self._active:
                return False
            self._active.add(key)

        def _wrapped() -> None:
            try:
                fn(*args, **kwargs)
            except Exception:  # noqa: BLE001
                logger.exception("Background task failed: %s", key)
            finally:
                with self._lock:
                    self._active.discard(key)

        if self._inline:
            _wrapped()
            return True

        try:
            with self._lock:
                executor = self._get_executor()
            executor.submit(_wrapped)
        except Exception:
            with self._lock:
                self._active.discard(key)
            raise
        return True

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=not wait)


_GLOBAL_RUNNER = TaskRunner(max_workers=4)


def task_runner() -> TaskRunner:
    """返回全局共享的后台任务执行器单例。"""
    return _GLOBAL_RUNNER
