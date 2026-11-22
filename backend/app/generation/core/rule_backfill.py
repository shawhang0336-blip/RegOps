"""RegOps 规则确定性回填策略（策略层）。

将不需要也不应该完全依赖大模型自由发挥的三类确定性元数据规则抽离为独立策略：
1. 必填回填（_backfill_required_rules）：字典校验列以「必填」开头且未绑 REQUIRED 规则的字段；
2. 列宽上限回填（_backfill_length_rules）：VARCHAR 声明了列宽（1..4000）且未绑长度规则的字段；
3. 固定取值回填（_backfill_fixed_value_rules）：原文明确要求「本项固定为 XXX」且非条件句的字段。
"""

from typing import Any, Callable, Dict, List, Optional, Tuple

from app.infra.contracts import RuleDefinition, calculate_rule_hash
from app.infra.errors import WorkflowError
from app.generation.core.binding import bind_rules
from app.generation.core.rules import RuleValidationError


def _required_backfill_candidates(specs, rules) -> List[tuple]:
    """找出「原文句首写必填、但规则集里没有 REQUIRED」的字段。

    只认句首的「必填」：「非必填项，当…时必填」和「条件必填」都不是无条件必填，
    误补一条 REQUIRED 会把合法的空值判成违规，比漏补伤害更大。
    """
    covered = {(rule.table_code, rule.field) for rule in rules if rule.operator == "REQUIRED"}
    candidates = []
    for spec in specs:
        for field in spec.fields:
            if (spec.table_code, field.name) in covered:
                continue
            if not (field.check_text or "").strip().startswith("必填"):
                continue
            candidates.append((spec, field))
    return candidates


def _backfill_required_rules(
    session,
    run,
    specs,
    rules,
    upsert_rule: Callable,
    emit: Callable,
    events: Optional[List[tuple]] = None,
) -> List[RuleDefinition]:
    """按字典原文机械补齐必填规则。"""
    added: List[RuleDefinition] = []
    for spec, field in _required_backfill_candidates(specs, rules):
        text = (field.check_text or "").strip()
        rule_key = f"{spec.table_code}_{field.name}_REQUIRED"
        try:
            bound = bind_rules(
                spec,
                [
                    {
                        "field": field.name,
                        "operator": "REQUIRED",
                        "params": {},
                        "rule_key": rule_key,
                        "evidence_kind": "DICTIONARY",
                        "rationale": f"数据字典校验原文以「必填」开头：{text[:80]}（由元数据机械补齐，非 AI 生成）",
                    }
                ],
            )
        except (RuleValidationError, WorkflowError) as exc:
            emit(
                events,
                session,
                run,
                "RULE_BACKFILL_GAP",
                "rule_backfill",
                {"table_code": spec.table_code, "field": field.name, "error": str(exc)},
            )
            continue
        upsert_rule(session, run, bound[0], "APPROVED")
        added.append(bound[0])
    if added:
        session.flush()
        emit(
            events,
            session,
            run,
            "RULE_BACKFILL",
            "rule_backfill",
            {
                "kind": "required",
                "total": len(added),
                "rules": [
                    {"table_code": rule.table_code, "rule_key": rule.rule_key, "field": rule.field}
                    for rule in added
                ],
                "note": "原文句首为「必填」但 AI 未生成对应 REQUIRED 规则，已确定性补齐",
            },
        )
    return rules + added


def _length_backfill_candidates(specs, rules) -> List[tuple]:
    """找出「字典声明了长度、但规则集里没有任何长度约束」的文本字段。"""
    covered = {
        (rule.table_code, rule.field)
        for rule in rules
        if rule.operator in {"LENGTH", "LENGTH_MAX"}
    }
    candidates = []
    for spec in specs:
        for field in spec.fields:
            if (spec.table_code, field.name) in covered:
                continue
            if (field.data_type or "").upper() != "VARCHAR":
                continue
            if not (0 < (field.length or 0) <= 4000):
                continue
            candidates.append((spec, field))
    return candidates


def _declaration_quote(spec, field) -> str:
    """从字典原文里取该字段那一行做证据；取不到就退回类型声明本身。"""
    want = (field.name or "").lower()
    for line in (spec.dictionary_text or "").splitlines():
        low = line.lower()
        if want and want in low and str(field.length) in line:
            return " ".join(line.split())
    return f"{field.name} 类型（长度）列：{field.data_type}({field.length})"


def _backfill_length_rules(
    session,
    run,
    specs,
    rules,
    upsert_rule: Callable,
    emit: Callable,
    events: Optional[List[tuple]] = None,
) -> List[RuleDefinition]:
    """按类型（长度）列机械补齐 LENGTH_MAX。"""
    added: List[RuleDefinition] = []
    for spec, field in _length_backfill_candidates(specs, rules):
        rule_key = f"{spec.table_code}_{field.name}_LENGTH_MAX"
        try:
            bound = bind_rules(
                spec,
                [
                    {
                        "field": field.name,
                        "operator": "LENGTH_MAX",
                        "params": {"length": int(field.length)},
                        "rule_key": rule_key,
                        "evidence_kind": "DICTIONARY",
                        "rationale": (
                            f"数据字典类型（长度）列声明 {field.data_type}({field.length})，"
                            f"故 {field.name} 最多 {field.length} 位（由元数据机械补齐，非 AI 生成）"
                        ),
                    }
                ],
            )
        except (RuleValidationError, WorkflowError) as exc:
            emit(
                events,
                session,
                run,
                "RULE_BACKFILL_GAP",
                "rule_backfill",
                {"table_code": spec.table_code, "field": field.name, "operator": "LENGTH_MAX", "error": str(exc)},
            )
            continue
        bound[0].evidence.quote = _declaration_quote(spec, field)
        bound[0].rule_hash = calculate_rule_hash(bound[0], spec.source_sha256)
        upsert_rule(session, run, bound[0], "APPROVED")
        added.append(bound[0])
    if added:
        session.flush()
        emit(
            events,
            session,
            run,
            "RULE_BACKFILL",
            "rule_backfill",
            {
                "kind": "length_max",
                "total": len(added),
                "rules": [
                    {"table_code": rule.table_code, "rule_key": rule.rule_key, "field": rule.field}
                    for rule in added
                ],
                "note": "字典声明了列宽但 AI 未生成长度规则，已按 LENGTH_MAX 确定性补齐",
            },
        )
    return rules + added


def _fixed_value_candidates(specs, rules) -> List[tuple]:
    """找出「原文明确要求本项固定填某个值」的字段。"""
    from app.catalog.code_sets.parser import is_conditional_phrase, parse_fixed_value

    covered = {(rule.table_code, rule.field) for rule in rules if rule.operator == "ENUM"}
    candidates = []
    for spec in specs:
        for field in spec.fields:
            if (spec.table_code, field.name) in covered:
                continue
            value = parse_fixed_value(field.code_reference or "") or parse_fixed_value(field.check_text or "")
            if not value:
                continue
            if is_conditional_phrase(field.check_text or ""):
                continue
            candidates.append((spec, field, value))
    return candidates


def _backfill_fixed_value_rules(
    session,
    run,
    specs,
    rules,
    upsert_rule: Callable,
    emit: Callable,
    events: Optional[List[tuple]] = None,
) -> List[RuleDefinition]:
    """按原文的固定值要求补齐 ENUM。"""
    added: List[RuleDefinition] = []
    for spec, field, value in _fixed_value_candidates(specs, rules):
        rule_key = f"{spec.table_code}_{field.name}_FIXED_VALUE_ENUM"
        try:
            bound = bind_rules(
                spec,
                [
                    {
                        "field": field.name,
                        "operator": "ENUM",
                        "params": {"values": [value]},
                        "rule_key": rule_key,
                        "evidence_kind": "DICTIONARY",
                        "rationale": (
                            f"原文要求本项固定为「{value}」"
                            f"（{(field.code_reference or field.check_text)[:60]}），"
                            "故取值只能是这一个（由元数据机械补齐，非 AI 生成）"
                        ),
                    }
                ],
            )
        except (RuleValidationError, WorkflowError) as exc:
            emit(
                events,
                session,
                run,
                "RULE_BACKFILL_GAP",
                "rule_backfill",
                {"table_code": spec.table_code, "field": field.name, "value": value, "error": str(exc)},
            )
            continue
        upsert_rule(session, run, bound[0], "APPROVED")
        added.append(bound[0])
    if added:
        session.flush()
        emit(
            events,
            session,
            run,
            "RULE_BACKFILL",
            "rule_backfill",
            {
                "kind": "fixed_value",
                "total": len(added),
                "rules": [
                    {"table_code": rule.table_code, "rule_key": rule.rule_key, "field": rule.field}
                    for rule in added
                ],
                "note": "原文要求固定取值但 AI 未生成 ENUM 规则，已确定性补齐",
            },
        )
    return rules + added


def _code_set_enum_backfill_candidates(
    specs,
    rules,
    field_code_sets: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
) -> List[tuple]:
    """找出「字段已绑定已发布码表且有启用码值、但尚未有 ENUM 规则」的字段。"""
    if not field_code_sets:
        return []
    covered = {(rule.table_code, rule.field) for rule in rules if rule.operator == "ENUM"}
    candidates = []
    for spec in specs:
        for field in spec.fields:
            if (spec.table_code, field.name) in covered:
                continue
            code_set = field_code_sets.get((spec.table_code, field.name))
            if not code_set:
                continue
            values = [str(item["value"]) for item in (code_set.get("values") or []) if item.get("value") is not None]
            if not values:
                continue
            candidates.append((spec, field, code_set, values))
    return candidates


def _backfill_code_set_enum_rules(
    session,
    run,
    specs,
    rules,
    field_code_sets: Optional[Dict[Tuple[str, str], Dict[str, Any]]],
    upsert_rule: Callable,
    emit: Callable,
    events: Optional[List[tuple]] = None,
) -> List[RuleDefinition]:
    """按已发布码表确定性派生全量 ENUM 规则（执行期自动编译为 Oracle 码表 NOT EXISTS 子查询）。"""
    added: List[RuleDefinition] = []
    for spec, field, code_set, values in _code_set_enum_backfill_candidates(specs, rules, field_code_sets):
        rule_key = f"{spec.table_code}_{field.name}_CODE_SET_ENUM"
        code_name = code_set.get("name") or code_set.get("code") or "已发布码表"
        try:
            bound = bind_rules(
                spec,
                [
                    {
                        "field": field.name,
                        "operator": "ENUM",
                        "params": {"values": values},
                        "rule_key": rule_key,
                        "evidence_kind": "DICTIONARY",
                        "rationale": (
                            f"字段已绑定已发布码表「{code_name}（{code_set.get('code', '')}）」"
                            f"共 {len(values)} 项启用码值，取值必须落入码表白名单（由码表资产确定性派生，非 AI 生成）"
                        ),
                    }
                ],
            )
        except (RuleValidationError, WorkflowError) as exc:
            emit(
                events,
                session,
                run,
                "RULE_BACKFILL_GAP",
                "rule_backfill",
                {"table_code": spec.table_code, "field": field.name, "operator": "ENUM", "error": str(exc)},
            )
            continue
        upsert_rule(session, run, bound[0], "APPROVED")
        added.append(bound[0])
    if added:
        session.flush()
        emit(
            events,
            session,
            run,
            "RULE_BACKFILL",
            "rule_backfill",
            {
                "kind": "code_set_enum",
                "total": len(added),
                "rules": [
                    {"table_code": rule.table_code, "rule_key": rule.rule_key, "field": rule.field}
                    for rule in added
                ],
                "note": "字段已绑定已发布码表，已确定性派生 ENUM 白名单规则",
            },
        )
    return rules + added


def derive_deterministic_rules(
    session,
    run,
    specs,
    upsert_rule: Callable,
    emit: Callable,
    existing_rules: Optional[List[RuleDefinition]] = None,
    field_code_sets: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    events: Optional[List[tuple]] = None,
) -> List[RuleDefinition]:
    """前置确定性规则派生总入口：在 AI 规则绑定前直接产出四类机械规则。"""
    rules: List[RuleDefinition] = list(existing_rules or [])
    rules = _backfill_required_rules(session, run, specs, rules, upsert_rule, emit, events=events)
    rules = _backfill_length_rules(session, run, specs, rules, upsert_rule, emit, events=events)
    rules = _backfill_fixed_value_rules(session, run, specs, rules, upsert_rule, emit, events=events)
    rules = _backfill_code_set_enum_rules(
        session, run, specs, rules, field_code_sets, upsert_rule, emit, events=events
    )
    return rules
