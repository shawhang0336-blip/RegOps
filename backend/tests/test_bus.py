"""Bus 门禁层测试。

核心断言：字段名幻觉在工具执行（validator）之前就被物理拦截，
原因回喂模型；这不是写在 prompt 里的软约束。
"""

from app.bus import (
    Bus,
    code_set_values_adjudicator,
    cross_table_adjudicator,
    default_bus,
    field_whitelist_adjudicator,
    operator_whitelist_adjudicator,
    structure_block_adjudicator,
)
from app.generic_workflow import _call_validated
from app.generic_ai import LlmTimeoutError
from app.errors import WorkflowError


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
