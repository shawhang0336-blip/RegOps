"""Bus 门禁层测试。

核心断言：字段名幻觉在工具执行（validator）之前就被物理拦截，
原因回喂模型；这不是写在 prompt 里的软约束。
"""

from app.kernel.bus import Bus, global_bus
from app.policy.regops_bus import (
    code_set_values_adjudicator,
    cross_table_adjudicator,
    default_bus,
    field_whitelist_adjudicator,
    operator_whitelist_adjudicator,
    structure_block_adjudicator,
)
from app.generation.workflow import _call_validated
from app.infra.llm import LlmTimeoutError
from app.infra.errors import WorkflowError


def test_field_whitelist_adjudicator_blocks_hallucination():
    payload = {"field_names": ["CUSTCODE", "ACTIONTYPE"]}
    reason = field_whitelist_adjudicator(
        "assign_rules",
        {"rules": [{"field": "ORG_CODE_ERR", "operator": "REQUIRED", "params": {}}]},
        payload,
    )
    assert reason is not None
    assert "ORG_CODE_ERR" in reason
    assert "字段白名单" in reason


def test_field_whitelist_adjudicator_checks_nested_args():
    payload = {"field_names": ["STARTDATE", "ENDDATE"]}
    reason = field_whitelist_adjudicator(
        "assign_rules",
        {"rules": [{"field": "STARTDATE", "operator": "FIELD_COMPARE",
                    "params": {"right_field": "END_DATE_TYPO", "operator": "GE"}}]},
        payload,
    )
    assert reason is not None
    assert "END_DATE_TYPO" in reason


def test_field_whitelist_allows_valid_names():
    payload = {"field_names": ["CUSTCODE", "ACTIONTYPE"]}
    assert field_whitelist_adjudicator(
        "assign_rules",
        {"rules": [{"field": "CUSTCODE", "operator": "REQUIRED", "params": {}}]},
        payload,
    ) is None
    # 没有 field_names 的阶段（如审计用例）跳过字段裁决
    assert field_whitelist_adjudicator("assign_audit_cases", {"cases": []}, {}) is None


def test_cross_table_adjudicator_blocks_unconfirmed_table():
    payload = {"table_codes": ["SAFE_6_1", "SAFE_6_4"]}
    reason = cross_table_adjudicator(
        "assign_rules",
        {"rules": [{"table_code": "SAFE_6_1", "params": {"references_table": "SAFE_9_9"}}]},
        payload,
    )
    assert reason is not None
    assert "SAFE_9_9" in reason


def test_bus_records_rejection_and_stops_at_first():
    calls = []
    bus = Bus().add_adjudicator(field_whitelist_adjudicator).add_observer(calls.append)
    reason = bus.adjudicate(
        "assign_rules",
        {"rules": [{"field": "BAD_FIELD"}]},
        {"field_names": ["GOOD"]},
    )
    assert reason is not None
    assert len(calls) == 1
    assert calls[0]["tool"] == "assign_rules"
    assert "BAD_FIELD" in calls[0]["reason"]


def test_call_validated_feeds_bus_rejection_back_to_model():
    """Bus 拒绝后走校验失败重试路径：原因回喂模型，第二次给合法参数即通过。"""
    seen_feedback = []

    class Llm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            seen_feedback.append(feedback)
            if feedback is None:
                args = {"rules": [{"field": "HALLUCINATED"}]}
            else:
                args = {"rules": [{"field": "CUSTCODE"}]}
            return args, {"provider": "TEST"}

    result, trace = _call_validated(
        Llm(),
        "assign_rules",
        "instr",
        {"field_names": ["CUSTCODE"]},
        lambda args: args,
        backoff=(0, 0, 0),
    )
    assert result["rules"][0]["field"] == "CUSTCODE"
    assert trace["attempts"] == 2
    assert "bus_rejected" in trace
    assert len(trace["bus_rejections"]) == 1
    assert "字段白名单" in (seen_feedback[1] or "")


def test_field_whitelist_covers_schema_generators_and_audit_cases():
    payload = {"field_names": ["CUSTCODE", "ACTIONTYPE"]}
    assert field_whitelist_adjudicator(
        "assign_schema",
        {"fields": [{"field": "FAKE_COL", "storage_type": "TEXT", "length": 10, "decimal_places": 0}]},
        payload,
    ) is not None
    assert field_whitelist_adjudicator(
        "assign_generators",
        {"generators": [{"field": "FAKE_GEN", "generator": "FIXED", "params": {"value": "A"}}]},
        payload,
    ) is not None
    assert field_whitelist_adjudicator(
        "assign_audit_cases",
        {"cases": [{"case_id": "C1", "overrides": {"FAKE_OVERRIDE": "X"}, "expected_rule_keys": ["R1"]}]},
        {"table": payload},
    ) is not None


def test_operator_and_code_set_and_structure_adjudicators():
    assert operator_whitelist_adjudicator(
        "assign_rules",
        {"rules": [{"field": "CUSTCODE", "operator": "LENGTH", "params": {"length": 18}}]},
        {},
    ) is not None
    assert operator_whitelist_adjudicator(
        "assign_rules",
        {"rules": [{"field": "CUSTCODE", "operator": "LENGTH_MAX", "params": {"length": 18}}]},
        {},
    ) is None

    cs_payload = {"published_code_set": {"code": "ACTION_CS", "values": ["A", "C", "D"]}}
    assert code_set_values_adjudicator(
        "assign_rules",
        {"rules": [{"field": "ACTIONTYPE", "operator": "ENUM", "params": {"values": ["A", "Z"]}}]},
        cs_payload,
    ) is not None
    assert code_set_values_adjudicator(
        "assign_generators",
        {"generators": [{"field": "ACTIONTYPE", "generator": "ENUM", "params": {"values": ["X"]}}]},
        cs_payload,
    ) is not None
    assert code_set_values_adjudicator(
        "assign_rules",
        {"rules": [{"field": "ACTIONTYPE", "operator": "ENUM", "params": {"values": ["A", "C"]}}]},
        cs_payload,
    ) is None

    assert structure_block_adjudicator(
        "assign_document_structure",
        {"blocks": [{"source_block_ids": ["b_unknown"]}]},
        {"candidate_blocks": [{"id": "b1"}]},
    ) is not None
    assert structure_block_adjudicator(
        "assign_document_structure",
        {"blocks": [{"source_block_ids": ["b1"]}]},
        {"candidate_blocks": [{"id": "b1"}]},
    ) is None


def test_bus_full_lifecycle_fire_subscribe_isolation_and_global_bubbling():
    """验证 Bus 全生命周期广播：
    1. 精确主题订阅与 '*' 通配订阅；
    2. 旁观者抛异常时被严格隔离，绝不影响主流程；
    3. tool.before / tool.rejected / tool.after 全生命周期信号；
    4. 子 Bus 向 global_bus 冒泡，以及 document.progress 进度广播。
    """
    parent = Bus()
    child = default_bus(parent=parent)

    seen_topics = []
    parent_topics = []

    def bad_subscriber(_ev):
        raise RuntimeError("broken observer should never crash workflow")

    child.subscribe("*", bad_subscriber)
    child.subscribe("*", lambda ev: seen_topics.append(ev["topic"]))
    parent.subscribe("*", lambda ev: parent_topics.append(ev["topic"]))

    class Llm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            if feedback is None:
                return {"rules": [{"field": "FAKE_FIELD"}]}, {"provider": "TEST"}
            return {"rules": [{"field": "CUSTCODE"}]}, {"provider": "TEST"}

    child.fire("node.enter", {"run_id": "r1", "node": "rule_binder"})
    result, _ = _call_validated(
        Llm(),
        "assign_rules",
        "instr",
        {"field_names": ["CUSTCODE"], "table_code": "SAFE_6_1"},
        lambda args: args,
        backoff=(0, 0, 0),
        bus=child,
    )
    child.fire("node.exit", {"run_id": "r1", "node": "rule_binder", "status": "RUNNING"})

    assert result["rules"][0]["field"] == "CUSTCODE"
    assert seen_topics == [
        "node.enter",
        "tool.before",
        "tool.rejected",
        "tool.before",
        "tool.after",
        "node.exit",
    ]
    assert parent_topics == seen_topics
    assert len(child.history(topic="tool.rejected")) == 1
    assert len(parent.history(run_id="r1")) == 2

    # 验证阶段一 import_progress 亦通过 global_bus 广播 document.progress
    from app.ingestion.import_progress import finish, forget, start, update

    doc_events = []
    handler = lambda ev: doc_events.append(ev)
    global_bus().subscribe("document.progress", handler)
    try:
        start("doc-bus-test", "spec.pdf")
        update("doc-bus-test", stage=2, detail="结构体检中", log="开始体检")
        finish("doc-bus-test", status="DONE", detail="完成")
    finally:
        global_bus().unsubscribe("document.progress", handler)
        forget("doc-bus-test")

    assert len(doc_events) == 3
    assert [e["stage"] for e in doc_events] == [1, 2, 4]
    assert doc_events[-1]["status"] == "DONE"


def test_policy_deep_audit_cross_fk_fixed_code_set_and_channel_redirect_recovery():
    """回归：policy 深审 5 项修复：
    1. field_whitelist_adjudicator 不误拦异名跨表外键的 references_field；
    2. cross_table_adjudicator 拦截跨表规则源字段 field 幻觉；
    3. code_set_values_adjudicator 拦截 FIXED 生成器编造非法码值（允许可选空串）；
    4. recover_channels 在 GO_REDIRECT 缺少/读取失败报告时保守清空下游单表缓存，且清理前序自愈报告；
    5. regops_plan 折叠防缺 target KeyError、支持 ORACLE_ASSETS_PREPARED.pending_count、防 ingestion 非 dict result 崩溃。
    """
    import uuid
    import pytest
    from app.infra.db_tables import RunModel
    from app.kernel.event_log import RunState, append_event
    from app.policy.regops_channels import recover_channels
    from app.policy.regops_plan import _apply, fold_ingestion_event
    from .helpers import make_session

    # 1) 异名外键 CUST_ID -> T_PARENT.ID 不被当前表 field_whitelist 误拦
    assert (
        field_whitelist_adjudicator(
            "assign_rules",
            {
                "rules": [
                    {
                        "field": "CUST_ID",
                        "operator": "FOREIGN_KEY",
                        "params": {"references_table": "T_PARENT", "references_field": "ID"},
                    }
                ]
            },
            {"field_names": ["CUST_ID", "AMT"]},
        )
        is None
    )

    # 2) cross_table_adjudicator 拦截跨表规则源字段幻觉
    bad_src = cross_table_adjudicator(
        "assign_rules",
        {
            "rules": [
                {
                    "table_code": "T_CHILD",
                    "field": "GHOST_SRC",
                    "operator": "FOREIGN_KEY",
                    "params": {"references_table": "T_PARENT", "references_field": "ID"},
                }
            ]
        },
        {"table_codes": ["T_CHILD", "T_PARENT"], "table_fields": {"T_CHILD": ["CUST_ID"], "T_PARENT": ["ID"]}},
    )
    assert bad_src is not None and "GHOST_SRC" in bad_src

    # 3) code_set_values_adjudicator 拦截 FIXED 生成器非法码值，允许合规值与空串
    cs_payload = {"published_code_sets": {"TXCCY": {"code": "CCY", "values": ["CNY", "USD"]}}}
    bad_fixed = code_set_values_adjudicator(
        "assign_generators",
        {"generators": [{"field": "TXCCY", "generator": "FIXED", "params": {"value": "RMB"}}]},
        cs_payload,
    )
    assert bad_fixed is not None and "RMB" in bad_fixed
    assert (
        code_set_values_adjudicator(
            "assign_generators",
            {"generators": [{"field": "TXCCY", "generator": "FIXED", "params": {"value": "CNY"}}]},
            cs_payload,
        )
        is None
    )
    assert (
        code_set_values_adjudicator(
            "assign_generators",
            {"generators": [{"field": "TXCCY", "generator": "FIXED", "params": {"value": ""}}]},
            cs_payload,
        )
        is None
    )

    # 4) recover_channels：GO_REDIRECT 无报告时仍清空下游单表缓存防误跳重造
    session = make_session()
    run = RunModel(id=str(uuid.uuid4()), spec_id="spec-policy-audit")
    session.add(run)
    session.flush()
    append_event(session, run, "TABLE_GENERATORS_ASSIGNED", "data_table_worker", {"table_code": "T1", "generators": [{"field": "A"}]})
    append_event(session, run, "TABLE_AUDIT_CASES_ASSIGNED", "audit_table_worker", {"table_code": "T1", "cases": [{"case_id": "C1"}]})
    append_event(session, run, "GO_REDIRECT", "oracle_executor", {"target": "data_generator_binder"})
    session.commit()
    shared = recover_channels(session, run, settings=None)
    assert shared["table_generators_done"] == {}
    assert shared["table_audit_cases_done"] == {}

    # 5) regops_plan 折叠健壮性
    st_assets = _apply(RunState(status="RUNNING"), "ORACLE_ASSETS_PREPARED", {"pending_count": 2})
    assert st_assets.when == "pending_rules"
    with pytest.raises(ValueError, match="GO_REDIRECT"):
        _apply(RunState(), "GO_REDIRECT", {})
    st_ing = fold_ingestion_event(RunState(), "RUN_FAILED", {"node": "extract", "result": "plain-string-error"})
    assert st_ing.status == "FAILED" and st_ing.result_json["error"] == "plain-string-error"

