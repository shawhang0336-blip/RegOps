"""Run：ProdAgent 运行实例生命周期微内核（第 7 个内核一等公民）。

职责边界（严格遵守「机制在内，策略在外」，零业务域 import）：
- `Interrupt`：一等人工中断指令对象，由节点通过 `Outcome(interrupt=...)` 声明，由 `Scheduler` 统一挂起落账；
- `CancellationToken` / `RunCancelledError` / `RunRegistry`：线程安全的活跃 Run 注册表与波次/节点边界取消机制；
- `create_run` / `fail_run` / `record_run_cancelled` / `cancel_run` / `recover_interrupted_runs`：
  统一 Run 创世、失败落账、取消与进程重启恢复原语，消除散落在 `main.py` / `api` / `engine` 的重复手拼事件；
- `submit_run`：基于有界 `TaskRunner` 的后台推进与防重入封装，自动挂接 `CancellationToken` 与异常兜底落账；
- `answer_interrupt`：通用中断幂等重入解析原语；
- `delete_run` / `replay_run`：带可插拔业务钩子的统一删除与哈希链回放框架。
"""

import threading
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.infra.models import EventModel, RunModel
from app.infra.task_runner import TaskRunner, task_runner
from .bus import global_bus
from .event_log import (
    RunState,
    append_event,
    project_many_with,
    project_with,
    verify_chain,
)
from .interrupt import Interrupt, answer_interrupt, record_interrupt_answer  # noqa: F401


class RunCancelledError(Exception):
    """Run 在节点/波次边界收到取消信号后抛出的受控中止异常。"""

    def __init__(self, reason: str = "Run cancelled by operator") -> None:
        super().__init__(reason)
        self.reason = reason


class CancellationToken:
    """线程安全的 Run 级取消令牌，供 Scheduler 与执行节点轮询或检查。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str = ""

    def cancel(self, reason: str = "Run cancelled by operator") -> None:
        with self._lock:
            if not self._reason:
                self._reason = reason or "Run cancelled by operator"
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason or "Run cancelled by operator"

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise RunCancelledError(self.reason)


class RunRegistry:
    """进程内活跃 Run 注册表：追踪正在执行的 Run 与其取消令牌。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: Dict[str, CancellationToken] = {}

    def register(self, run_id: str, token: Optional[CancellationToken] = None) -> CancellationToken:
        active_token = token or CancellationToken()
        with self._lock:
            self._tokens[run_id] = active_token
        return active_token

    def unregister(self, run_id: str, token: Optional[CancellationToken] = None) -> None:
        with self._lock:
            current = self._tokens.get(run_id)
            if current is None:
                return
            if token is None or current is token:
                self._tokens.pop(run_id, None)

    def get_token(self, run_id: str) -> Optional[CancellationToken]:
        with self._lock:
            return self._tokens.get(run_id)

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._tokens

    def active_run_ids(self) -> List[str]:
        with self._lock:
            return sorted(self._tokens.keys())

    def request_cancel(self, run_id: str, reason: str = "Run cancelled by operator") -> bool:
        with self._lock:
            token = self._tokens.get(run_id)
        if token is None:
            return False
        token.cancel(reason)
        return True


_GLOBAL_REGISTRY = RunRegistry()


def run_registry() -> RunRegistry:
    """返回进程全局共享的活跃 Run 注册表单例。"""
    return _GLOBAL_REGISTRY


def create_run(
    session: Session,
    spec_id: str,
    initial_node: str = "created",
    initial_payload: Optional[Dict[str, Any]] = None,
    *,
    run_id: Optional[str] = None,
    bus: Any = None,
) -> RunModel:
    """创建 RunModel 身份并追加创世事件 RUN_CREATED（确保每个 Run 都有合法账本起点）。"""
    run = RunModel(id=run_id or str(uuid.uuid4()), spec_id=spec_id)
    session.add(run)
    session.flush()
    payload: Dict[str, Any] = {"spec_id": spec_id}
    if initial_payload:
        payload.update(initial_payload)
    append_event(session, run, "RUN_CREATED", initial_node, payload, bus=bus)
    return run


def fail_run(
    session: Session,
    run: RunModel,
    node: str,
    error: str,
    *,
    error_details: Optional[Dict[str, Any]] = None,
    resume_from_node: Optional[str] = None,
    resume_when: Optional[str] = None,
    bus: Any = None,
    commit: bool = True,
) -> EventModel:
    """统一失败落账原语：排空 Bus 拦截 → 写 RUN_FAILED → 广播 node.error & run.failed。"""
    active_bus = bus if bus is not None else global_bus()
    if hasattr(active_bus, "fire"):
        active_bus.fire("node.error", {"run_id": run.id, "node": node, "error": error})
    if hasattr(active_bus, "drain_rejections"):
        for rej in active_bus.drain_rejections():
            append_event(session, run, "BUS_REJECTED", node, rej, bus=active_bus)

    result_payload: Dict[str, Any] = {"error": error}
    if error_details is not None:
        result_payload["error_details"] = error_details
    event_payload: Dict[str, Any] = {"node": node, "result": result_payload}
    if resume_from_node is not None:
        event_payload["resume_from_node"] = resume_from_node
    if resume_when is not None:
        event_payload["resume_when"] = resume_when

    event = append_event(session, run, "RUN_FAILED", node, event_payload, bus=active_bus)
    if hasattr(active_bus, "fire"):
        active_bus.fire(
            "run.failed",
            {"run_id": run.id, "node": node, "status": "FAILED", "error": error},
        )
    if commit:
        session.commit()
    return event


def record_run_cancelled(
    session: Session,
    run: RunModel,
    node: str,
    reason: str = "Run cancelled by operator",
    *,
    resume_from_node: Optional[str] = None,
    resume_when: Optional[str] = None,
    bus: Any = None,
    commit: bool = True,
) -> EventModel:
    """落账受控取消事件 RUN_CANCELLED 并广播 run.cancelled，保留检查点以支持后续断点续跑。"""
    active_bus = bus if bus is not None else global_bus()
    event_payload: Dict[str, Any] = {
        "node": node,
        "reason": reason,
        "result": {"error": reason, "cancelled": True},
    }
    if resume_from_node is not None:
        event_payload["resume_from_node"] = resume_from_node
    if resume_when is not None:
        event_payload["resume_when"] = resume_when
    event = append_event(session, run, "RUN_CANCELLED", node, event_payload, bus=active_bus)
    if hasattr(active_bus, "fire"):
        active_bus.fire(
            "run.cancelled",
            {"run_id": run.id, "node": node, "status": "FAILED", "reason": reason},
        )
    if commit:
        session.commit()
    return event


def cancel_run(
    session: Session,
    run: RunModel,
    *,
    active_statuses: Iterable[str],
    node: str = "cancel",
    reason: str = "Run cancelled by operator",
    projector: Optional[Callable[[Session, RunModel], RunState]] = None,
    bus: Any = None,
    registry: Optional[RunRegistry] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """取消指定运行中 Run：若后台线程活跃则投递取消令牌，否则直接落账 RUN_CANCELLED。"""
    project_fn = projector or project_with
    reg = registry if registry is not None else run_registry()
    state = project_fn(session, run)
    allowed = set(active_statuses)
    if state.status not in allowed and not reg.is_active(run.id):
        raise ValueError(f"Run is not active (status={state.status})")

    signalled = reg.request_cancel(run.id, reason)
    if signalled:
        return {
            "run_id": run.id,
            "signalled": True,
            "cancelled": False,
            "status": state.status,
            "reason": reason,
        }

    record_run_cancelled(
        session,
        run,
        node=node,
        reason=reason,
        resume_from_node=state.resume_from_node,
        resume_when=state.resume_when,
        bus=bus,
        commit=commit,
    )
    new_state = project_fn(session, run)
    return {
        "run_id": run.id,
        "signalled": False,
        "cancelled": True,
        "status": new_state.status,
        "reason": reason,
    }


def recover_interrupted_runs(
    session: Session,
    active_statuses: Iterable[str],
    *,
    projector_many: Optional[Callable[[Session, Iterable[RunModel]], Dict[str, RunState]]] = None,
    reason: str = "Run interrupted by process restart",
    bus: Any = None,
    commit: bool = True,
) -> List[str]:
    """进程重启中断恢复原语：将遗留在运行态的批次统一落账为失败，等待人工断点续跑。"""
    runs = session.query(RunModel).all()
    if not runs:
        return []
    project_fn = projector_many or project_many_with
    states = project_fn(session, runs)
    active = set(active_statuses)
    recovered: List[str] = []
    for run in runs:
        state = states[run.id]
        if state.status not in active:
            continue
        fail_run(
            session,
            run,
            node="startup",
            error=reason,
            resume_from_node=state.resume_from_node,
            resume_when=state.resume_when,
            bus=bus,
            commit=False,
        )
        recovered.append(run.id)
    if recovered and commit:
        session.commit()
    return recovered


def submit_run(
    run_id: str,
    session_factory: Callable[[], Session],
    driver_fn: Callable[[Session, RunModel, CancellationToken], Any],
    *,
    allowed_statuses: Iterable[str] = ("RUNNING", "READY_TO_AUDIT"),
    projector: Optional[Callable[[Session, RunModel], RunState]] = None,
    error_details_fn: Optional[Callable[[BaseException, str], Dict[str, Any]]] = None,
    runner: Optional[TaskRunner] = None,
    registry: Optional[RunRegistry] = None,
) -> bool:
    """统一后台提交入口：按 `run:<id>` 防重入 + 注册取消令牌 + 异常/取消统一落账。"""
    project_fn = projector or project_with
    active_runner = runner if runner is not None else task_runner()
    active_registry = registry if registry is not None else run_registry()
    allowed = set(allowed_statuses)

    def _worker() -> None:
        token = active_registry.register(run_id)
        try:
            with session_factory() as session:
                run = session.get(RunModel, run_id)
                if not run:
                    return
                if project_fn(session, run).status not in allowed:
                    return
                driver_fn(session, run, token)
        except RunCancelledError as exc:
            with session_factory() as session:
                run = session.get(RunModel, run_id)
                if run and project_fn(session, run).status in allowed:
                    st = project_fn(session, run)
                    record_run_cancelled(
                        session,
                        run,
                        node=st.current_node or "background_executor",
                        reason=exc.reason,
                        resume_from_node=st.resume_from_node,
                        resume_when=st.resume_when,
                    )
        except Exception as exc:  # noqa: BLE001
            with session_factory() as session:
                run = session.get(RunModel, run_id)
                if run and project_fn(session, run).status in allowed:
                    st = project_fn(session, run)
                    details = error_details_fn(exc, "background_executor") if error_details_fn else None
                    fail_run(
                        session,
                        run,
                        node="background_executor",
                        error=f"{type(exc).__name__}: {exc}",
                        error_details=details,
                        resume_from_node=st.resume_from_node,
                        resume_when=st.resume_when,
                    )
        finally:
            active_registry.unregister(run_id, token)

    return active_runner.submit(f"run:{run_id}", _worker)


def delete_run(
    session: Session,
    run: RunModel,
    *,
    active_statuses: Iterable[str],
    projector: Optional[Callable[[Session, RunModel], RunState]] = None,
    pre_delete_hooks: Optional[Iterable[Callable[[Session, RunModel, Dict[str, Any]], None]]] = None,
    post_commit_hooks: Optional[Iterable[Callable[[Session, Dict[str, Any]], None]]] = None,
    registry: Optional[RunRegistry] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """统一 Run 删除原语：运行态守卫 → 业务前置清理钩子 → 删事件与身份 → 提交 → 外部资源清理钩子。"""
    project_fn = projector or project_with
    reg = registry if registry is not None else run_registry()
    if project_fn(session, run).status in set(active_statuses) or reg.is_active(run.id):
        raise ValueError("Running validation batches cannot be deleted")

    context: Dict[str, Any] = {"run_id": run.id}
    for hook in pre_delete_hooks or ():
        hook(session, run, context)

    session.query(EventModel).filter(EventModel.run_id == run.id).delete(synchronize_session=False)
    session.delete(run)
    session.flush()
    if commit:
        session.commit()

    for hook in post_commit_hooks or ():
        hook(session, context)
    return context


def replay_run(
    session: Session,
    run: RunModel,
    *,
    projector: Optional[Callable[[Session, RunModel], RunState]] = None,
    registry: Optional[RunRegistry] = None,
) -> Dict[str, Any]:
    """统一回放与哈希链完整性校验原语。"""
    project_fn = projector or project_with
    reg = registry if registry is not None else run_registry()
    state = project_fn(session, run)
    return {
        "status": state.status,
        "current_node": state.current_node,
        "attempt": state.attempt,
        "result": state.result_json,
        "chain_valid": verify_chain(session, run),
        "active": reg.is_active(run.id),
    }
