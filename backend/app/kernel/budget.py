"""BudgetGuard：ProdAgent 横切成本统计与熔断守卫微内核。

设计原则（机制在内，策略在外）：
- 通过订阅 `Bus` 生命周期事件（`tool.after` / `tool.rejected` / `node.exit`）无侵入统计单次 Run 的
  LLM 调用次数、Prompt/Completion/Total Tokens、累计模型耗时、执行步数与门禁拦截次数；
- 在波次边界（`Scheduler`）与模型调用前（`Bus.check_budget()`）执行确定性阈值检查，
  超限时广播 `budget.exceeded` 并抛出 `BudgetExceededError` 立即熔断；
- 零业务依赖，任何接入 `Bus` 的工作流（无论 RegOps 还是通用 Agent）均可直接挂载。
"""

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .bus import Bus


@dataclass(frozen=True)
class BudgetLimits:
    """运行期成本上限配置；任一项为 None 或 <= 0 表示仅统计不熔断。"""

    max_llm_calls: Optional[int] = None
    max_total_tokens: Optional[int] = None
    max_elapsed_seconds: Optional[float] = None
    max_steps: Optional[int] = None

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "BudgetLimits":
        data = raw or {}

        def _pos_int(key: str) -> Optional[int]:
            val = data.get(key)
            if val is None or val == "":
                return None
            iv = int(val)
            return iv if iv > 0 else None

        def _pos_float(key: str) -> Optional[float]:
            val = data.get(key)
            if val is None or val == "":
                return None
            fv = float(val)
            return fv if fv > 0 else None

        return cls(
            max_llm_calls=_pos_int("max_llm_calls"),
            max_total_tokens=_pos_int("max_total_tokens"),
            max_elapsed_seconds=_pos_float("max_elapsed_seconds"),
            max_steps=_pos_int("max_steps"),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "max_llm_calls": self.max_llm_calls,
            "max_total_tokens": self.max_total_tokens,
            "max_elapsed_seconds": self.max_elapsed_seconds,
            "max_steps": self.max_steps,
        }


class BudgetExceededError(RuntimeError):
    """成本或步数超出 BudgetLimits 时抛出的内核级熔断异常。"""

    def __init__(self, metric: str, used: Any, limit: Any, snapshot: Dict[str, Any]) -> None:
        super().__init__(f"执行预算超限（{metric}={used}，上限={limit}），内核已触发熔断。")
        self.metric = metric
        self.used = used
        self.limit = limit
        self.snapshot = snapshot


class BudgetGuard:
    """线程安全的横切成本观测与熔断器：挂载到 Run 级 Bus 上自动记账并执行阈值检查。"""

    def __init__(self, limits: Optional[BudgetLimits] = None, *, run_id: Optional[str] = None) -> None:
        self.limits = limits or BudgetLimits()
        self.run_id = run_id
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._llm_calls = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0
        self._total_llm_seconds = 0.0
        self._steps = 0
        self._tool_rejections = 0
        self._by_tool: Dict[str, int] = {}
        self._bus: Optional[Bus] = None

    def attach(self, bus: Bus) -> "BudgetGuard":
        """挂载到指定 Bus：订阅工具/节点事件并注册为主动预算检查钩子。"""
        self._bus = bus
        bus.subscribe("tool.after", self._on_tool_after)
        bus.subscribe("tool.rejected", self._on_tool_rejected)
        bus.subscribe("node.exit", self._on_node_exit)
        if hasattr(bus, "attach_budget"):
            bus.attach_budget(self)
        return self

    def _on_tool_after(self, envelope: Dict[str, Any]) -> None:
        tool = str(envelope.get("tool") or "unknown")
        attempts = max(1, int(envelope.get("attempts") or 1))
        elapsed = float(envelope.get("elapsed_seconds") or 0.0)
        usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
        prompt_t = int(usage.get("prompt_tokens") or 0)
        comp_t = int(usage.get("completion_tokens") or 0)
        total_t = int(usage.get("total_tokens") or (prompt_t + comp_t))
        with self._lock:
            self._llm_calls += attempts
            self._prompt_tokens += prompt_t
            self._completion_tokens += comp_t
            self._total_tokens += total_t
            self._total_llm_seconds += elapsed
            self._by_tool[tool] = self._by_tool.get(tool, 0) + attempts

    def _on_tool_rejected(self, _envelope: Dict[str, Any]) -> None:
        with self._lock:
            self._tool_rejections += 1

    def _on_node_exit(self, _envelope: Dict[str, Any]) -> None:
        with self._lock:
            self._steps += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            elapsed = round(time.monotonic() - self._started_at, 2)
            return {
                "llm_calls": self._llm_calls,
                "prompt_tokens": self._prompt_tokens,
                "completion_tokens": self._completion_tokens,
                "total_tokens": self._total_tokens,
                "total_llm_seconds": round(self._total_llm_seconds, 2),
                "elapsed_seconds": elapsed,
                "steps": self._steps,
                "tool_rejections": self._tool_rejections,
                "by_tool": dict(self._by_tool),
                "limits": self.limits.as_dict(),
            }

    def check(self, bus: Optional[Bus] = None) -> None:
        """检查是否触达任一预算上限；超限时广播 budget.exceeded 并抛出 BudgetExceededError。"""
        snap = self.snapshot()
        breach = None
        if self.limits.max_llm_calls is not None and snap["llm_calls"] >= self.limits.max_llm_calls:
            breach = ("max_llm_calls", snap["llm_calls"], self.limits.max_llm_calls)
        elif self.limits.max_total_tokens is not None and snap["total_tokens"] >= self.limits.max_total_tokens:
            breach = ("max_total_tokens", snap["total_tokens"], self.limits.max_total_tokens)
        elif self.limits.max_elapsed_seconds is not None and snap["elapsed_seconds"] >= self.limits.max_elapsed_seconds:
            breach = ("max_elapsed_seconds", snap["elapsed_seconds"], self.limits.max_elapsed_seconds)
        elif self.limits.max_steps is not None and snap["steps"] >= self.limits.max_steps:
            breach = ("max_steps", snap["steps"], self.limits.max_steps)
        if breach is not None:
            metric, used, limit = breach
            active_bus = bus or self._bus
            if active_bus is not None and hasattr(active_bus, "fire"):
                active_bus.fire(
                    "budget.exceeded",
                    {
                        "run_id": self.run_id,
                        "metric": metric,
                        "used": used,
                        "limit": limit,
                        "snapshot": snap,
                    },
                )
            raise BudgetExceededError(metric, used, limit, snap)
