"""Step 0/1 Scheduler 编排、块级断点续跑、Worker 零写库与限流/两级预算熔断回归测试。"""

import time
import uuid

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.delivery.runs import trace_run
from app.catalog.pipeline_run import (
    list_document_runs,
    start_document_extract_run,
    start_document_prepare_run,
)
from app.infra.config import Settings
from app.infra.db import Base
from app.infra.db_tables import (
    RawBlockModel,
    SourceDocumentModel,
    SystemSettingModel,
    TableMetadataModel,
)
from app.infra.errors import ProviderAuthOrBillingError
from app.infra.llm import (
    RealDesignLlm,
    TokenBucketRateLimiter,
    call_llm_validated,
    llm_execution_scope,
)
from app.infra.task_runner import TaskRunner
from app.kernel.event_log import GENESIS_HASH
from app.kernel.run import (
    BudgetExceededError,
    BudgetGuard,
    BudgetLimits,
    CancellationToken,
    ChannelSpec,
    RunCancelledError,
    create_memory_context,
    merge_dict,
)
from app.kernel.scheduler import Outcome, Scheduler, Send, execute_wave
from app.policy.regops_bus import build_child_bus, default_bus
from app.policy.regops_plan import INGESTION_EXTRACT_PLAN, INGESTION_PREPARE_PLAN

def _make_wal_db(tmp_path, name: str):
    db_file = tmp_path / f"{name}.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return engine, factory


def test_execute_wave_contextvars_memory_context_and_post_llm_budget_barrier():
    """验证 execute_wave 自动复制 contextvars、纯内存 RunContext 哈希链，以及 BudgetGuard 恰好用满名额时不误杀 0-Token 收尾节点。"""
    bus = default_bus()
    guard = BudgetGuard(BudgetLimits(max_llm_calls=2), run_id="exact-limit-run").attach(bus)
    token = CancellationToken()

    class ExactLlm:
        def assign(self, *_args, **_kwargs):
            return {"fields": []}, {"provider": "test", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

    llm = ExactLlm()

    # 1) execute_wave 多线程无需手工 submit_with_context，也能自动穿透 llm_execution_scope 的 bus
    with llm_execution_scope(bus=bus, cancellation=token):
        results = execute_wave(
            ["a", "b"],
            lambda item: (item, call_llm_validated(llm, "assign_metadata", "instr", {})[1]),
            max_workers=2,
            cancellation=token,
        )
    assert len(results) == 2
    assert guard.snapshot()["llm_calls"] == 2

    # 2) llm_calls == max_llm_calls 时，check() 不在波次边界误杀 0-Token 后继节点，但 begin_call() 阻止第 3 次 LLM 调用
    bus.check_budget()
    with pytest.raises(BudgetExceededError):
        call_llm_validated(llm, "assign_metadata", "instr", {}, bus=bus)

    # 3) create_memory_context 零 DB 运行完整 Scheduler + 维护哈希链
    from dataclasses import replace
    from app.kernel.plan import PlanEdge, PlanNode, WorkflowPlan

    mem_plan = WorkflowPlan(
        name="mem_workflow",
        entry="start",
        nodes=(
            PlanNode("start", "启动", "stage"),
            PlanNode("finish", "收尾", "terminal"),
        ),
        edges=(PlanEdge("start", "finish", "always"),),
    )

    mem_ctx = create_memory_context(
        "mem_workflow",
        initial_node="start",
        channels={"items": ChannelSpec("items", merge_dict)},
        bus=bus,
        budget_guard=guard,
        reducer=lambda st, kind, _p: (
            replace(st, status="RUNNING", current_node="start")
            if kind == "RUN_CREATED"
            else (replace(st, status="COMPLETED", current_node="finish") if kind == "DONE" else st)
        ),
    )

    def _finish(_inp, _c):
        return Outcome(shared={"items": {"ok": 1}}, events=[("DONE", "finish", {"ok": True})])

    Scheduler(mem_plan, {"finish": _finish}).drive(mem_ctx)
    assert mem_ctx.is_in_memory is True
    assert mem_ctx.get("items") == {"ok": 1}
    assert mem_ctx.state.status == "COMPLETED"
    assert mem_ctx.memory_events[0].prev_hash == GENESIS_HASH


def test_rate_limiter_cooldown_billing_fast_fail_and_parent_child_bus_cascade(monkeypatch):
    """验证 RPM 令牌桶限速、429 冷却支持取消唤醒、402 欠费极速熔断与父子 Bus 级联预占/回滚。"""
    # 1) TokenBucketRateLimiter：120 RPM = 每 0.5s 补 1 个令牌；trigger 429 后可用 CancellationToken 秒级唤醒
    limiter = TokenBucketRateLimiter(rpm=120)
    assert limiter.acquire() == 0.0
    limiter.notify_rate_limited(retry_after=5.0)
    assert limiter.cooldown_remaining() > 1.0
    tok = CancellationToken()
    tok.cancel("stop waiting")
    t0 = time.monotonic()
    with pytest.raises(RunCancelledError):
        limiter.acquire(cancellation=tok)
    assert time.monotonic() - t0 < 0.5

    # 2) RealDesignLlm / call_llm_validated 遇到 HTTP 402 余额不足：直接抛 ProviderAuthOrBillingError，只调 1 次绝不空耗重试
    calls = {"n": 0}

    class FakeResp:
        is_error = True
        status_code = 402
        headers = {}
        text = '{"error":{"code":"insufficient_balance","message":"Insufficient Balance"}}'

        @staticmethod
        def json():
            return {"error": {"code": "insufficient_balance", "message": "Insufficient Balance"}}

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: (calls.__setitem__("n", calls["n"] + 1) or FakeResp()))
    real_llm = RealDesignLlm(Settings(llm_provider="openai", llm_api_key="sk-test"))
    with pytest.raises(ProviderAuthOrBillingError):
        call_llm_validated(real_llm, "assign_metadata", "instr", {}, backoff=(5.0,))
    assert calls["n"] == 1

    # 3) 两级父子 Bus：子 Bus 设 max_calls_per_table=1；第 1 次成功并推进父计数=1，第 2 次子超限时回滚父在途预占且仅拦子作用域
    parent_bus = default_bus()
    parent_guard = BudgetGuard(BudgetLimits(max_llm_calls=5), run_id="parent").attach(parent_bus)
    child_bus = build_child_bus(parent_bus, max_llm_calls=1, run_id="child-block")

    class OkLlm:
        def assign(self, *_a, **_kw):
            return {"fields": []}, {"usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}}

    ok_llm = OkLlm()
    call_llm_validated(ok_llm, "assign_metadata", "instr", {}, bus=child_bus)
    assert parent_guard.snapshot()["llm_calls"] == 1
    with pytest.raises(BudgetExceededError):
        call_llm_validated(ok_llm, "assign_metadata", "instr", {}, bus=child_bus)
    # 父预算仍剩余 4 次名额且未污染在途计数
    call_llm_validated(ok_llm, "assign_metadata", "instr", {}, bus=parent_bus)
    assert parent_guard.snapshot()["llm_calls"] == 2


def test_step1_ingestion_scheduler_plans_and_block_checkpoint_resume(tmp_path, monkeypatch):
    """验证 Step 0/1 INGESTION_PREPARE_PLAN / INGESTION_EXTRACT_PLAN 全流程、中断保留已完成块、0 Token 续跑与 trace 接口。"""
    engine, SessionFactory = _make_wal_db(tmp_path, "step1_sched")
    settings = Settings(blob_dir=str(tmp_path / "blobs"), llm_provider="scripted", llm_api_key="")
    try:
        doc_id = str(uuid.uuid4())
        b1_id = str(uuid.uuid4())
        b2_id = str(uuid.uuid4())

        with SessionFactory() as s:
            doc = SourceDocumentModel(
                id=doc_id,
                file_name="外管测试规范.pdf",
                file_type="pdf",
                sha256=uuid.uuid4().hex,
                blob_uri="blob://step1-test",
                spec_version="1.4",
                status="UPLOADED",
                block_count=2,
            )
            b1 = RawBlockModel(
                id=b1_id,
                document_id=doc_id,
                ordinal=0,
                block_type="STRUCTURED_SPEC",
                raw_content="表一 A1 字段1 AMOUNT 必填",
                raw_hash="hash-b1",
                source_locator={"page": 1, "table_code": "T_FIRST"},
                content_json={
                    "spec": {
                        "table_code": "T_FIRST",
                        "table_name": "首表",
                        "fields": [{"name": "AMOUNT"}],  # 缺类型 -> 触发 assign_metadata
                    }
                },
            )
            b2 = RawBlockModel(
                id=b2_id,
                document_id=doc_id,
                ordinal=1,
                block_type="STRUCTURED_SPEC",
                raw_content="表二 A2 字段2 CCY 必填",
                raw_hash="hash-b2",
                source_locator={"page": 2, "table_code": "T_SECOND"},
                content_json={
                    "spec": {
                        "table_code": "T_SECOND",
                        "table_name": "次表",
                        "fields": [{"name": "CCY"}],  # 缺类型 -> 触发 assign_metadata
                    }
                },
            )
            s.add_all(
                [
                    doc,
                    b1,
                    b2,
                    SystemSettingModel(
                        key="execution_budget",
                        value_json={
                            "max_llm_concurrency": 1,
                            "max_llm_calls": 2,  # b1 占 2 次 (assign + review)，b2 触发预算中断！
                            "max_total_tokens": 500000,
                            "max_elapsed_seconds": 300,
                        },
                    ),
                ]
            )
            s.commit()

        llm_calls = []

        class StubStep1Llm:
            def __init__(self, _settings):
                pass

            def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
                tcode = ((payload.get("table") or {}).get("table_code")) or ""
                llm_calls.append((tool_name, tcode))
                if tool_name == "assign_metadata":
                    raw = (payload.get("raw_fields") or [{}])[0]
                    fname = raw.get("name") or "F1"
                    return {
                        "table_definition": f"{tcode} 定义",
                        "fields": [
                            {
                                "field_cn": "金额" if fname == "AMOUNT" else "币种",
                                "field_en": fname,
                                "data_type": "VARCHAR",
                                "length": 16,
                                "decimal_places": 0,
                                "required": True,
                                "check_text_raw": "必填",
                                "confidence": 0.9,
                            }
                        ],
                    }, {"provider": "test", "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}
                return {"issues": []}, {"provider": "test", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

        import app.catalog.tables.service as tbl_svc

        monkeypatch.setattr(tbl_svc, "RealDesignLlm", StubStep1Llm)
        inline_runner = TaskRunner(inline=True)

        # Run 0：prepare Run 经 INGESTION_PREPARE_PLAN 执行并通过 trace_run 返回中文节点 span
        prep_run_id = start_document_prepare_run(
            doc_id,
            "外管测试规范.pdf",
            settings,
            session_factory=SessionFactory,
            runner=inline_runner,
            inspect_overview_fn=lambda *_a, **_kw: None,
            store_blocks_fn=lambda *_a, **_kw: None,
        )
        with SessionFactory() as s:
            prep_trace = trace_run(prep_run_id, session=s)
            assert [sp["node"] for sp in prep_trace["spans"]][:2] == ["prepare", "document_overview"]
            assert any(sp["node"] in {"initial_chunking", "prepare_completed"} for sp in prep_trace["spans"])

        # Run 1：max_llm_calls=2 -> 第 1 块完成，第 2 块预算熔断并保留 b1 检查点与已落库表
        run1_id = start_document_extract_run(
            doc_id,
            "外管测试规范.pdf",
            [b1_id, b2_id],
            ["metadata"],
            False,
            settings,
            session_factory=SessionFactory,
            runner=inline_runner,
        )
        assert len(llm_calls) == 2
        with SessionFactory() as s:
            runs = list_document_runs(s, doc_id)
            run1_info = next(r for r in runs if r["id"] == run1_id)
            assert run1_info["status"] == "FAILED"
            assert run1_info["chain_valid"] is True
            persisted_tables = s.query(TableMetadataModel).filter(TableMetadataModel.document_id == doc_id).all()
            assert [t.table_code for t in persisted_tables] == ["T_FIRST"]

            # 放宽预算，准备续跑
            setting = s.get(SystemSettingModel, "execution_budget")
            setting.value_json = {
                "max_llm_concurrency": 1,
                "max_llm_calls": 20,
                "max_total_tokens": 500000,
                "max_elapsed_seconds": 300,
            }
            s.commit()

        # Run 2：续跑应跳过 T_FIRST (0 次 LLM 调用)，只解析 T_SECOND (2 次 LLM 调用)
        llm_calls.clear()
        run2_id = start_document_extract_run(
            doc_id,
            "外管测试规范.pdf",
            [b1_id, b2_id],
            ["metadata"],
            False,
            settings,
            session_factory=SessionFactory,
            runner=inline_runner,
        )
        assert [tcode for _tool, tcode in llm_calls] == ["T_SECOND", "T_SECOND"]
        with SessionFactory() as s:
            runs = list_document_runs(s, doc_id)
            run2_info = next(r for r in runs if r["id"] == run2_id)
            assert run2_info["status"] == "COMPLETED"
            assert run2_info["chain_valid"] is True
            persisted_codes = sorted(
                t.table_code for t in s.query(TableMetadataModel).filter(TableMetadataModel.document_id == doc_id).all()
            )
            assert persisted_codes == ["T_FIRST", "T_SECOND"]

            extract_trace = trace_run(run2_id, session=s)
            assert extract_trace["spans"]
            assert any(sp["label"] == "单块字段与码表提取" for sp in extract_trace["spans"])
    finally:
        engine.dispose()
