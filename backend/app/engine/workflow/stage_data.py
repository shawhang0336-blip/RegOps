"""阶段二子模块 3：黄金合规数据生成（单表 BSP Worker / 跨表外键对齐 / 三类确定性规则回填）。"""

import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func

from app.assets.code_sets.oracle import load_published_code_sets
from app.engine.binding import (
    GeneratorBinding,
    _validate_generator_item,
    bind_generators,
    find_golden_rule_violations,
    materialize_golden_record,
)
from app.engine.rule_backfill import (
    _backfill_fixed_value_rules as _policy_backfill_fixed_value_rules,
    _backfill_length_rules as _policy_backfill_length_rules,
    _backfill_required_rules as _policy_backfill_required_rules,
)
from app.engine.rules import RuleValidationError
from app.engine.workflow.runtime import (
    _call_validated,
    _code_set_context,
    _emit,
    _fanout,
    _upsert_rule,
)
from app.engine.workflow.stage_plan_schema import _provider_name
from app.infra.asset_models import GeneratorDefinitionModel
from app.infra.domain import RuleDefinition, canonical_json
from app.infra.llm import spec_payload
from app.kernel.blob_store import LocalBlobStore

_GENERATOR_BATCH_SIZE = 12
_SEMANTIC_GENERATOR_OPERATORS = {
    "REGEXP",
    "NUM_RANGE",
    "FIELD_COMPARE",
    "COND_REQUIRED",
    "COND_VALUE",
    "ARITHMETIC_BALANCE",
    "UNIQUE_KEY",
    "FOREIGN_KEY",
}


def _rule_related_fields(rule: RuleDefinition) -> List[str]:
    """返回单条规则涉及的所有字段名（用于耦合分组与黄金自洽门禁检查）。"""
    params = rule.params or {}
    names = [rule.field]
    if rule.operator == "FIELD_COMPARE" and params.get("right_field"):
        names.append(params["right_field"])
    elif rule.operator in {"COND_REQUIRED", "COND_VALUE"} and params.get("if_field"):
        names.append(params["if_field"])
    elif rule.operator == "ARITHMETIC_BALANCE":
        for term in params.get("terms") or []:
            if isinstance(term, dict) and term.get("field"):
                names.append(term["field"])
        if params.get("target_field"):
            names.append(params["target_field"])
    elif rule.operator == "UNIQUE_KEY":
        for kf in params.get("key_fields") or []:
            if kf:
                names.append(kf)
    return list(dict.fromkeys(names))


def _next_asset_version(session, model, run_id, table_code) -> int:
    current = (
        session.query(func.max(model.version))
        .filter(model.run_id == run_id, model.table_code == table_code)
        .scalar()
    )
    return int(current or 0) + 1


def _coupled_field_names(
    spec,
    table_rules: List[RuleDefinition],
    heal_report: Optional[Dict[str, Any]] = None,
) -> set:
    """识别需要 AI 联合语义赋值的字段（正则、数值区间、跨字段比较、算术平衡、条件约束、唯一键、外键或误报自愈字段）。"""
    coupled = set()
    rule_by_key = {rule.rule_key: rule for rule in table_rules}
    for rule in table_rules:
        if rule.operator in _SEMANTIC_GENERATOR_OPERATORS:
            coupled.update(_rule_related_fields(rule))
    if heal_report:
        for case in heal_report.get("cases", []):
            if case.get("table_code") != spec.table_code or case.get("case_kind") != "GOLDEN":
                continue
            for rule_key in case.get("unexpected_rules") or []:
                rule = rule_by_key.get(rule_key)
                if rule:
                    coupled.update(_rule_related_fields(rule))
    return coupled


def _deterministic_generator_item(
    field,
    table_code: str,
    table_rules: List[RuleDefinition],
    field_code_sets: Dict[Any, Any],
    required_ref_values: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """对无复杂语义约束的字段直接确定性构造合规生成器，减少 AI 杂活并保证码表对齐。"""
    if field.name in required_ref_values:
        return {"field": field.name, "generator": "FIXED", "params": {"value": required_ref_values[field.name]}}

    field_rules = [rule for rule in table_rules if rule.field == field.name]
    forbidden = {
        str(val)
        for rule in field_rules
        if rule.operator == "NOT_IN"
        for val in (rule.params.get("values") or [])
    }
    length_limits = [int(field.length)] if field.length else []
    for rule in field_rules:
        if rule.operator in {"LENGTH_MAX", "LENGTH"} and rule.params.get("length"):
            length_limits.append(int(rule.params["length"]))
    max_len = min(length_limits) if length_limits else 64

    code_set = field_code_sets.get((table_code, field.name))
    if code_set and code_set.get("values"):
        candidates = [
            str(item["value"])
            for item in code_set["values"]
            if item.get("value") is not None and str(item["value"]) not in forbidden
        ]
        if not candidates:
            return None
        return {"field": field.name, "generator": "ENUM", "params": {"values": candidates[:10]}}

    enum_rules = [rule for rule in field_rules if rule.operator == "ENUM" and rule.params.get("values")]
    if enum_rules:
        allowed_sets = [
            [str(v) for v in rule.params["values"] if str(v) not in forbidden]
            for rule in enum_rules
        ]
        common = allowed_sets[0]
        for other in allowed_sets[1:]:
            other_set = set(other)
            common = [v for v in common if v in other_set]
        if not common:
            return None
        if len(common) == 1:
            return {"field": field.name, "generator": "FIXED", "params": {"value": common[0]}}
        return {"field": field.name, "generator": "ENUM", "params": {"values": common[:10]}}

    if (field.data_type or "").upper() == "NUMBER":
        for cand in (1, 10, 2, 100):
            if str(cand) not in forbidden:
                return {"field": field.name, "generator": "FIXED", "params": {"value": cand}}
        return None

    exact_lengths = [
        int(rule.params["length"]) for rule in field_rules if rule.operator == "LENGTH" and rule.params.get("length")
    ]
    if exact_lengths:
        exact = exact_lengths[0]
        candidate = "A" * exact
        if candidate not in forbidden:
            return {"field": field.name, "generator": "FIXED", "params": {"value": candidate}}
        return None

    for base in ("SAMPLE_OK", "OK", "A", "B", "1", "V"):
        cand = base[: max(1, max_len)]
        if cand and cand not in forbidden:
            return {"field": field.name, "generator": "FIXED", "params": {"value": cand}}
    return None


def _partition_coupled_chunks(
    ai_fields: List[Any], table_rules: List[RuleDefinition], batch_size: int
) -> List[List[Any]]:
    """按跨字段约束连通分量分组打包，确保 FIELD_COMPARE / COND_REQUIRED 关联字段绝不被拆到不同批次。"""
    if len(ai_fields) <= batch_size:
        return [ai_fields] if ai_fields else []
    field_by_name = {f.name: f for f in ai_fields}
    order_index = {f.name: idx for idx, f in enumerate(ai_fields)}
    adj: Dict[str, set] = {f.name: set() for f in ai_fields}
    for rule in table_rules:
        related = [name for name in _rule_related_fields(rule) if name in adj]
        if len(related) < 2:
            continue
        anchor = related[0]
        for peer in related[1:]:
            adj[anchor].add(peer)
            adj[peer].add(anchor)

    visited = set()
    components: List[List[Any]] = []
    for f in ai_fields:
        if f.name in visited:
            continue
        stack = [f.name]
        comp_names = []
        visited.add(f.name)
        while stack:
            cur = stack.pop()
            comp_names.append(cur)
            for nxt in adj[cur]:
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append(nxt)
        comp_names.sort(key=lambda n: order_index[n])
        components.append([field_by_name[n] for n in comp_names])

    chunks: List[List[Any]] = []
    current: List[Any] = []
    for comp in components:
        if current and len(current) + len(comp) > batch_size:
            chunks.append(current)
            current = []
        if len(comp) >= batch_size:
            chunks.append(comp)
        else:
            current.extend(comp)
    if current:
        chunks.append(current)
    return chunks


def bind_table_data_worker(
    spec,
    *,
    llm,
    active_rules: List[RuleDefinition],
    golden_overrides: Dict[str, Any],
    field_code_sets: Dict[Any, Any],
    plans: Optional[Dict[str, Any]] = None,
    bus: Any = None,
    heal_report: Optional[Dict[str, Any]] = None,
) -> Tuple[List[GeneratorBinding], Dict[str, Any]]:
    """纯单表黄金造数 Worker（零 DB 写操作，可安全并发运行在 Scheduler BSP Send 波次内）。"""
    table_rules = [rule for rule in active_rules if rule.table_code == spec.table_code]
    rule_views = [rule.model_dump() for rule in table_rules]
    required_refs = golden_overrides.get(spec.table_code, {})
    golden_count = int(((plans or {}).get(spec.table_code) or {}).get("golden_count", 1))
    coupled = _coupled_field_names(spec, table_rules, heal_report=heal_report)

    by_field_name: Dict[str, Dict[str, Any]] = {}
    ai_fields = []
    for field in spec.fields:
        if field.name in coupled:
            ai_fields.append(field)
            continue
        det_item = _deterministic_generator_item(field, spec.table_code, table_rules, field_code_sets, required_refs)
        if det_item is not None:
            try:
                binding = GeneratorBinding.model_validate(det_item)
                _validate_generator_item(field, binding)
                det_item["params"] = binding.params
                by_field_name[field.name] = det_item
                continue
            except Exception:
                pass
        ai_fields.append(field)

    if by_field_name and not ai_fields:
        prelim_bindings = [GeneratorBinding.model_validate(by_field_name[f.name]) for f in spec.fields]
        prelim_record = materialize_golden_record(spec, prelim_bindings, 0)
        prelim_violations = find_golden_rule_violations(
            spec, table_rules, prelim_record, prelim_bindings, golden_count, golden_overrides=required_refs
        )
        if prelim_violations:
            conflict_fields = {v["field"] for v in prelim_violations}
            for field in spec.fields:
                if field.name in conflict_fields:
                    by_field_name.pop(field.name, None)
                    ai_fields.append(field)

    batch_traces: List[Dict[str, Any]] = []
    if by_field_name:
        batch_traces.append(
            {
                "strategy": "DETERMINISTIC_GENERATORS",
                "field_count": len(by_field_name),
                "fields": sorted(by_field_name),
            }
        )

    heal_feedback_cases = []
    if heal_report:
        heal_feedback_cases = [
            case
            for case in heal_report.get("cases", [])
            if case.get("table_code") == spec.table_code
            and case.get("case_kind") == "GOLDEN"
            and case.get("unexpected_rules")
        ]

    chunks = _partition_coupled_chunks(ai_fields, table_rules, _GENERATOR_BATCH_SIZE)
    for chunk in chunks:
        chunk_names = [f.name for f in chunk]
        chunk_name_set = set(chunk_names)
        chunk_by_name = {f.name: f for f in chunk}
        det_golden_preview = (
            materialize_golden_record(
                spec,
                [GeneratorBinding.model_validate(item) for item in by_field_name.values()],
                0,
            )
            if by_field_name
            else {}
        )
        chunk_code_sets = {
            f.name: _code_set_context(field_code_sets, spec.table_code, f.name)["published_code_set"]
            for f in chunk
            if (spec.table_code, f.name) in field_code_sets
        }
        payload: Dict[str, Any] = {
            **spec_payload(spec),
            "target_fields": chunk_names,
            "fields": [f.model_dump() for f in chunk],
            "deterministic_golden_values": det_golden_preview,
            "approved_rules": rule_views,
            "required_reference_values": required_refs,
            "generation_plan": plans.get(spec.table_code) if plans else None,
        }
        if chunk_code_sets:
            payload["published_code_sets"] = chunk_code_sets
            if len(chunk) == 1 and chunk[0].name in chunk_code_sets:
                payload["published_code_set"] = chunk_code_sets[chunk[0].name]
        if heal_feedback_cases:
            payload["oracle_false_positive_feedback"] = heal_feedback_cases

        def validate_batch(args, _chunk_names=chunk_names, _chunk_name_set=chunk_name_set, _chunk_by_name=chunk_by_name):
            raw_items = [item for item in (args.get("generators") or []) if isinstance(item, dict)]
            items = [item for item in raw_items if item.get("field") in _chunk_name_set]
            returned_fields = [item.get("field") for item in items]
            missing = [name for name in _chunk_names if name not in set(returned_fields)]
            if missing or len(returned_fields) != len(_chunk_names):
                raise RuleValidationError(
                    f"Generator response must cover target_fields {_chunk_names} exactly once "
                    f"(missing={missing}, matched={returned_fields})"
                )
            validated_items: Dict[str, Dict[str, Any]] = {}
            for item in items:
                binding = GeneratorBinding.model_validate(item)
                _validate_generator_item(_chunk_by_name[binding.field], binding)
                item["params"] = binding.params
                validated_items[binding.field] = item

            merged_map = {**by_field_name, **validated_items}
            merged_bindings = [GeneratorBinding.model_validate(v) for v in merged_map.values()]
            covered_fields = set(merged_map)
            checkable_rules = []
            for rule in table_rules:
                needed = set(_rule_related_fields(rule))
                if needed.issubset(covered_fields):
                    checkable_rules.append(rule)
            sample_rec = materialize_golden_record(spec, merged_bindings, 0)
            violations = find_golden_rule_violations(
                spec, checkable_rules, sample_rec, merged_bindings, golden_count, golden_overrides=required_refs
            )
            if violations:
                summary = "；".join(v["message"] for v in violations[:5])
                raise RuleValidationError(
                    f"黄金样本自洽门禁拦截（测试数据违反已批准规则，请修正生成器参数）：{summary}"
                )
            return validated_items

        instruction = (
            f"【严格范围】本次仅需为 target_fields（{chunk_names}）输出黄金合规生成器，严禁重复输出 deterministic_golden_values 中已确定的字段。"
            "确保所有目标字段与 deterministic_golden_values、approved_rules、required_reference_values 同时满足，"
            "并且跨字段比较（FIELD_COMPARE）、算术平衡（ARITHMETIC_BALANCE）与条件约束（COND_REQUIRED/COND_VALUE）彼此自洽、零违规。"
            "重要：当字段参与 FIELD_COMPARE / ARITHMETIC_BALANCE / COND_REQUIRED / COND_VALUE 且 golden_count > 1 时，"
            "请对参与关联的字段统一使用 FIXED 生成器避免交叉失配；若字段参与 UNIQUE_KEY 且 golden_count > 1，请对其使用 SEQUENCE 生成器确保每条黄金样本唯一。"
            "FIXED 使用 params.value；ENUM 使用 params.values；SEQUENCE 使用 params.start/step。"
            "若提供 published_code_sets/published_code_set，对应字段的 ENUM values 只能取其白名单子集（或使用 FIXED 填其中一个白名单合法码值）。"
        )
        if heal_feedback_cases:
            instruction += "注意：上轮 Oracle 审计发现黄金样本触发了误报（见 oracle_false_positive_feedback），请重点修正导致误报的字段取值。"

        validated_map, trace = _call_validated(
            llm,
            "assign_generators",
            instruction,
            payload,
            validate_batch,
            bus=bus,
        )
        by_field_name.update(validated_map)
        batch_traces.append(trace)

    generator_items = [by_field_name[field.name] for field in spec.fields]
    generators = bind_generators(spec, generator_items)
    ai_traces = [tr for tr in batch_traces if tr.get("strategy") != "DETERMINISTIC_GENERATORS"]
    first_ai = ai_traces[0] if ai_traces else None
    return generators, {
        "provider": _provider_name(llm, first_ai) if first_ai else "DETERMINISTIC",
        "model": first_ai.get("model", "unknown") if first_ai else "rule-engine",
        "tool_name": "assign_generators",
        "batch_count": len(batch_traces),
        "deterministic_field_count": len(spec.fields) - len(ai_fields),
        "ai_field_count": len(ai_fields),
        "batches": batch_traces,
        "arguments": {"generators": generator_items},
    }


def join_table_data_barrier(
    session,
    run,
    specs,
    active_rules: List[RuleDefinition],
    golden_overrides: Dict[str, Any],
    table_generators: Dict[str, List[Any]],
    *,
    plans: Optional[Dict[str, Any]] = None,
    events: Optional[List[tuple]] = None,
    blob_store: Optional[LocalBlobStore] = None,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """黄金造数波次屏障汇聚：物化黄金记录 → 外键对齐 → 落库 GeneratorDefinitionModel → 发射 GENERATORS_ASSIGNED。"""
    generators: Dict[str, List[GeneratorBinding]] = {
        spec.table_code: [
            item if isinstance(item, GeneratorBinding) else GeneratorBinding.model_validate(item)
            for item in table_generators[spec.table_code]
        ]
        for spec in specs
    }
    golden_records = {
        spec.table_code: materialize_golden_record(spec, generators[spec.table_code]) for spec in specs
    }
    for table_code, overrides in golden_overrides.items():
        if table_code in golden_records:
            golden_records[table_code].update(overrides)
            for field_name, val in overrides.items():
                if val is not None:
                    fixed = GeneratorBinding(field=field_name, generator="FIXED", params={"value": val})
                    generators[table_code] = [fixed if g.field == field_name else g for g in generators[table_code]]

    fk_rules = [rule for rule in active_rules if rule.operator == "FOREIGN_KEY"]
    if fk_rules:
        spec_by_code = {spec.table_code: spec for spec in specs}
        for _ in range(max(1, len(specs))):
            changed = False
            for rule in fk_rules:
                ref_table = (rule.params or {}).get("references_table")
                ref_field = (rule.params or {}).get("references_field") or rule.field
                if ref_table not in golden_records or rule.table_code not in golden_records:
                    continue
                parent_val = golden_records[ref_table].get(ref_field)
                if parent_val is None:
                    continue
                child_spec = spec_by_code.get(rule.table_code)
                if not child_spec or not any(f.name == rule.field for f in child_spec.fields):
                    continue
                if golden_records[rule.table_code].get(rule.field) != parent_val:
                    golden_records[rule.table_code][rule.field] = parent_val
                    changed = True
                parent_gen = next((g for g in generators[ref_table] if g.field == ref_field), None)
                aligned_gen = (
                    parent_gen.model_copy(update={"field": rule.field})
                    if parent_gen is not None
                    else GeneratorBinding(field=rule.field, generator="FIXED", params={"value": parent_val})
                )
                generators[rule.table_code] = [
                    aligned_gen if g.field == rule.field else g for g in generators[rule.table_code]
                ]
            if not changed:
                break

    for spec in specs:
        session.add(
            GeneratorDefinitionModel(
                id=str(uuid.uuid4()),
                run_id=run.id,
                table_code=spec.table_code,
                version=_next_asset_version(session, GeneratorDefinitionModel, run.id, spec.table_code),
                definition_json={
                    "generators": [item.model_dump() for item in generators[spec.table_code]],
                    "golden_record": golden_records[spec.table_code],
                    "generation_plan": (plans or {}).get(spec.table_code),
                },
            )
        )
    session.flush()
    generator_payload = {
        spec.table_code: {
            "generators": [item.model_dump() for item in generators[spec.table_code]],
            "golden_record": golden_records[spec.table_code],
        }
        for spec in specs
    }
    event_payload: Dict[str, Any] = {"tables": generator_payload}
    if blob_store is not None:
        event_payload["tables_blob"] = blob_store.put(canonical_json(generator_payload).encode("utf-8"))
    _emit(events, session, run, "GENERATORS_ASSIGNED", "data_barrier", event_payload)
    _emit(
        events,
        session,
        run,
        "BSP_BARRIER_JOIN",
        "data_barrier",
        {"stage": "data_generator_binder", "table_codes": list(generators)},
    )
    return golden_records, generator_payload


def _data_stage(
    session,
    run,
    specs,
    llm,
    active_rules,
    golden_overrides,
    plans=None,
    concurrency: int = 4,
    bus=None,
    events: Optional[List[tuple]] = None,
    blob_store: Optional[LocalBlobStore] = None,
    heal_report: Optional[Dict[str, Any]] = None,
):
    _, field_code_sets = load_published_code_sets(session, specs)

    def worker(spec):
        return bind_table_data_worker(
            spec,
            llm=llm,
            active_rules=active_rules,
            golden_overrides=golden_overrides,
            field_code_sets=field_code_sets,
            plans=plans,
            bus=bus,
            heal_report=heal_report,
        )

    results = _fanout(session, run, "data_generator_binder", specs, worker, max_workers=concurrency, events=events, bus=bus)
    table_generators = {spec.table_code: result for spec, (result, _) in zip(specs, results)}
    return join_table_data_barrier(
        session,
        run,
        specs,
        active_rules,
        golden_overrides,
        table_generators,
        plans=plans,
        events=events,
        blob_store=blob_store,
    )


def _backfill_required_rules(session, run, specs, rules, events: Optional[List[tuple]] = None) -> List[RuleDefinition]:
    return _policy_backfill_required_rules(session, run, specs, rules, _upsert_rule, _emit, events=events)


def _backfill_length_rules(session, run, specs, rules, events: Optional[List[tuple]] = None) -> List[RuleDefinition]:
    return _policy_backfill_length_rules(session, run, specs, rules, _upsert_rule, _emit, events=events)


def _backfill_fixed_value_rules(session, run, specs, rules, events: Optional[List[tuple]] = None) -> List[RuleDefinition]:
    return _policy_backfill_fixed_value_rules(session, run, specs, rules, _upsert_rule, _emit, events=events)
