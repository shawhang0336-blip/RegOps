import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from .blobs import LocalBlobStore
from .code_set_oracle import compile_code_set_oracle_ddl, load_published_code_sets, validate_code_set_usage
from .compiler import compile_rule_query
from .asset_models import ApprovalModel, ArtifactModel, AuditCaseDefinitionModel, GenerationPlanModel, GeneratorDefinitionModel, SchemaDefinitionModel
from .config import Settings
from .domain import RuleDefinition, SpecDocument, calculate_rule_hash, canonical_json
from .errors import WorkflowError
from .events import append_event
from .generic_ai import LlmTimeoutError, RealDesignLlm, spec_payload
from .generic_domain import (
    bind_audit_cases,
    bind_generators,
    bind_rules,
    bind_schema,
    materialize_golden_record,
)
from .generic_oracle import GenericOracleExecutor, compile_ai_schema_mysql_ddl, compile_ai_schema_oracle_ddl
from .models import EventModel, RuleModel, RunModel, SpecificationModel
from .rules import RuleValidationError


def _upsert_rule(session: Session, run: RunModel, rule: RuleDefinition, status: str) -> None:
    existing = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.rule_hash == rule.rule_hash)
        .one_or_none()
    )
    if existing:
        if existing.status == "REJECTED" and status == "PENDING":
            existing.status = "PENDING"
        return
    session.add(
        RuleModel(
            row_id=str(uuid.uuid4()),
            run_id=run.id,
            spec_id=run.spec_id,
            table_code=rule.table_code,
            rule_key=rule.rule_key,
            rule_hash=rule.rule_hash,
            source=rule.source,
            status=status,
            definition_json=rule.model_dump(),
        )
    )
    session.flush()


def _pending_rules(session: Session, run_id: str) -> List[RuleModel]:
    return session.query(RuleModel).filter(RuleModel.run_id == run_id, RuleModel.status == "PENDING").all()


def _specs(session: Session, spec_id: str) -> List[SpecDocument]:
    row = session.get(SpecificationModel, spec_id)
    if not row or "tables" not in row.content_json:
        raise WorkflowError("Confirmed specification snapshot not found")
    if not row.content_json.get("selection_confirmed"):
        raise WorkflowError("Tables must be confirmed before any AI binding or DDL execution")
    return [SpecDocument.model_validate(item) for item in row.content_json["tables"]]


def _code_set_context(field_code_sets, table_code: str, field: str) -> Dict[str, Any]:
    code_set = field_code_sets.get((table_code, field))
    if not code_set:
        return {}
    return {
        "published_code_set": {
            "code": code_set["code"],
            "name": code_set["name"],
            "version": code_set["version"],
            "values": [item["value"] for item in code_set["values"]],
        }
    }


STAGE_BY_TOOL = {
    "assign_generation_plan": "generation_planner",
    "assign_schema": "schema_mapper",
    "assign_rules": "rule_binder",
    "assign_generators": "data_generator_binder",
    "assign_audit_cases": "audit_case_binder",
}

ERROR_SUGGESTIONS = {
    "timeout": "模型响应超时（已重试一次仍失败）。可调大 LLM_TIMEOUT，或检查模型服务状态与队列。",
    "model_error": "模型调用失败或未返回工具调用。检查 API key、模型名与服务可用性。",
    "validation": "AI 返回的参数连续两次未通过确定性校验。检查工具契约与输入 payload。",
    "internal": "非模型错误。查看 message 与后端日志定位。",
}


def _locate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    table_code = payload.get("table_code") or (payload.get("table") or {}).get("table_code")
    fields = payload.get("fields") or []
    field = None
    if isinstance(fields, list) and len(fields) == 1 and isinstance(fields[0], dict):
        field = fields[0].get("name")
    return {"table_code": table_code, "field": field}


def _fail_context(tool_name: str, payload: Dict[str, Any], error_kind: str, retried: bool = False) -> Dict[str, Any]:
    context = {
        "tool_name": tool_name,
        "stage": STAGE_BY_TOOL.get(tool_name, tool_name),
        "error_kind": error_kind,
        "retried": retried,
    }
    context.update(_locate_payload(payload))
    return context


def _error_details(exc: BaseException, fallback_stage: str) -> Dict[str, Any]:
    context = getattr(exc, "context", None) or {}
    kind = context.get("error_kind") or "internal"
    return {
        "stage": context.get("stage") or fallback_stage,
        "error_type": type(exc).__name__,
        "error_kind": kind,
        "tool_name": context.get("tool_name"),
        "table_code": context.get("table_code"),
        "field": context.get("field"),
        "retried_after_timeout": context.get("retried"),
        "suggestion": ERROR_SUGGESTIONS.get(kind, ERROR_SUGGESTIONS["internal"]),
    }


# 瞬时错误（网络/超时/模型不调工具）的重试预算。线上 8 次失败里 6 次属于这类，
# 给 4 次机会 + 退避能把「一次抖动毁掉整批」的概率压到很低。
_TRANSIENT_ATTEMPTS = 4
# 参数校验失败的重试预算：把拒绝原因回喂给模型，通常第二次就能改对
_VALIDATION_ATTEMPTS = 3
_BACKOFF_SECONDS = (2.0, 5.0, 10.0)
_NO_TOOL_CALL_HINT = (
    "上一次没有返回工具调用。必须通过工具返回结构化参数，禁止输出自然语言或 SQL；"
    "字段名、规则参数必须严格取自输入。"
)


def _backoff_wait(steps, attempt: int) -> None:
    index = min(max(attempt - 1, 0), len(steps) - 1)
    delay = steps[index] if steps else 0
    if delay:
        time.sleep(delay)


def _call_validated(llm, tool_name, instruction, payload, validator, timeout=None, backoff=None):
    """带退避的模型调用。

    原来只重试 2 次、HTTP 错误直接抛、超时只给 1 次机会。线上 8 次失败里有 6 次是
    这类瞬时错误（"The read operation timed out" / "Model returned no tool call"），
    一次网络抖动就整批 FAILED，代价太大。现在按错误性质分开退避：

    - 网络/超时：重试 4 次，超时窗口逐次放宽，不回喂 feedback（模型侧可能压根没收到）；
    - 模型没调工具：重试 4 次，给一句更硬的指令（这是行为问题，不是网络问题）；
    - 参数校验不过：重试 3 次，把拒绝原因回喂给模型。
    """
    feedback = None
    rejection = None
    last_kind = "validation"
    last_error = ""
    retried_after_timeout = False
    transient_used = 0
    validation_used = 0
    wait_steps = _BACKOFF_SECONDS if backoff is None else backoff

    for attempt in range(1, _TRANSIENT_ATTEMPTS + _VALIDATION_ATTEMPTS + 1):
        # 超时过就放宽窗口，避免同一个请求反复被同一个超时掐掉
        attempt_timeout = timeout
        if timeout and transient_used:
            attempt_timeout = int(min(timeout * (1 + 0.5 * transient_used), timeout * 2))
        try:
            if attempt_timeout is None:
                arguments, trace = llm.assign(tool_name, instruction, payload, feedback)
            else:
                arguments, trace = llm.assign(tool_name, instruction, payload, feedback, timeout=attempt_timeout)
            result = validator(arguments)
            if rejection:
                trace["previous_rejection"] = rejection
            if retried_after_timeout:
                trace["retried_after_timeout"] = True
            if attempt > 1:
                trace["attempts"] = attempt
            return result, trace
        except LlmTimeoutError as exc:
            transient_used += 1
            retried_after_timeout = True
            last_kind, last_error = "timeout", str(exc)
            if transient_used >= _TRANSIENT_ATTEMPTS:
                break
            _backoff_wait(wait_steps, transient_used)
        except (httpx.HTTPError, TimeoutError, ConnectionError) as exc:
            transient_used += 1
            last_kind, last_error = "transport", f"{type(exc).__name__}: {exc}"
            if transient_used >= _TRANSIENT_ATTEMPTS:
                break
            _backoff_wait(wait_steps, transient_used)
        except RuntimeError as exc:
            # 模型没返回工具调用：给一句更硬的指令再来一次，而不是直接判死
            transient_used += 1
            last_kind, last_error = "model_no_tool_call", str(exc)
            feedback = _NO_TOOL_CALL_HINT
            if transient_used >= _TRANSIENT_ATTEMPTS:
                break
            _backoff_wait(wait_steps, transient_used)
        except (RuleValidationError, ValueError, KeyError, TypeError) as exc:
            validation_used += 1
            rejection = str(exc)
            feedback = rejection
            last_kind, last_error = "validation", rejection
            if validation_used >= _VALIDATION_ATTEMPTS:
                break

    if last_kind == "timeout":
        message = f"{tool_name} model call timed out after retry ({transient_used} 次): {last_error}"
    elif last_kind == "validation":
        message = f"{tool_name} 连续 {validation_used} 次未通过参数校验：{last_error}"
    else:
        message = f"{tool_name} 连续 {transient_used} 次没有拿到可用返回（{last_kind}）：{last_error}"
    raise WorkflowError(message, context=_fail_context(tool_name, payload, last_kind, retried=transient_used > 1))


def _fanout(session: Session, run: RunModel, stage: str, specs: List[SpecDocument], worker) -> List[Any]:
    append_event(
        session,
        run,
        "BSP_FANOUT",
        stage,
        {"stage": stage, "table_codes": [spec.table_code for spec in specs]},
    )
    with ThreadPoolExecutor(max_workers=min(4, len(specs))) as executor:
        futures = [executor.submit(worker, spec) for spec in specs]
        results = [future.result() for future in futures]
    for spec, (_, trace) in zip(specs, results):
        append_event(
            session,
            run,
            "AI_TOOL_ASSIGNED",
            stage,
            {"table_code": spec.table_code, **trace},
        )
    return results


def _validate_plan(args, budget):
    plan = {
        "mandatory_rule_target": int(args["mandatory_rule_target"]),
        "expanded_rule_target": int(args["expanded_rule_target"]),
        "golden_count": int(args["golden_count"]),
        "mutations_per_rule": int(args["mutations_per_rule"]),
        "rationale": args["rationale"],
    }
    if plan["mandatory_rule_target"] < 0 or plan["expanded_rule_target"] < 0:
        raise RuleValidationError("Rule targets cannot be negative")
    if plan["mandatory_rule_target"] + plan["expanded_rule_target"] > budget["max_rules_per_table"]:
        raise RuleValidationError("Rule target exceeds the configured budget")
    if plan["golden_count"] < 1 or plan["mutations_per_rule"] < 1:
        raise RuleValidationError("Golden count and mutations must be positive")
    estimated = plan["golden_count"] + plan["mutations_per_rule"] * (plan["mandatory_rule_target"] + plan["expanded_rule_target"])
    if estimated > budget["max_cases_per_table"]:
        raise RuleValidationError("Estimated audit cases exceed the configured budget")
    return plan


def _plan_stage(session, run, specs, llm, budget):
    def worker(spec):
        return _call_validated(
            llm,
            "assign_generation_plan",
            "根据表复杂度自行决定必做规则数、拓展规则数、黄金数据量和每规则变异数，并说明理由；不得超出输入预算。",
            {**spec_payload(spec), "budget": budget},
            lambda args: _validate_plan(args, budget),
        )

    results = _fanout(session, run, "generation_planner", specs, worker)
    plans = {spec.table_code: result for spec, (result, _) in zip(specs, results)}
    append_event(
        session,
        run,
        "GENERATION_PLANNED",
        "plan_barrier",
        {"tables": plans, "budget": budget},
    )
    for spec, (_, trace) in zip(specs, results):
        session.add(
            GenerationPlanModel(
                id=str(uuid.uuid4()),
                run_id=run.id,
                table_code=spec.table_code,
                plan_json=plans[spec.table_code],
                trace_json=trace,
            )
        )
    session.flush()
    append_event(session, run, "BSP_BARRIER_JOIN", "plan_barrier", {"stage": "generation_planner", "table_codes": list(plans)})
    return plans


def _schema_stage(session, run, specs, llm):
    def worker(spec):
        return _call_validated(
            llm,
            "assign_schema",
            "为每个白名单字段赋予物理存储类型和长度。必须逐字段全覆盖，不得改变数值字段语义。",
            spec_payload(spec),
            lambda args: bind_schema(spec, args.get("fields", [])),
        )

    results = _fanout(session, run, "schema_mapper", specs, worker)
    schemas = {spec.table_code: result for spec, (result, _) in zip(specs, results)}
    for spec in specs:
        session.add(
            SchemaDefinitionModel(
                id=str(uuid.uuid4()),
                run_id=run.id,
                table_code=spec.table_code,
                version=1,
                definition_json={"fields": [item.model_dump() for item in schemas[spec.table_code]]},
            )
        )
    session.flush()
    append_event(
        session,
        run,
        "SCHEMA_ASSIGNED",
        "schema_barrier",
        {"tables": {code: [item.model_dump() for item in items] for code, items in schemas.items()}},
    )
    append_event(
        session,
        run,
        "BSP_BARRIER_JOIN",
        "schema_barrier",
        {"stage": "schema_mapper", "table_codes": list(schemas)},
    )
    return schemas


def _rules_stage(session, run, specs, llm, plans=None) -> int:
    _, field_code_sets = load_published_code_sets(session, specs)

    def worker(spec):
        bound_rules = []
        batch_traces = []
        gaps: List[Dict[str, Any]] = []
        fields = spec.fields

        def bind_field(field):
            try:
                return _bind_one_field(field)
            except WorkflowError as exc:
                # 单个字段绑不出规则不该毁掉整张表：记账、跳过，让人在规则确认环节补
                gaps.append(
                    {
                        "field": field.name,
                        "error": str(exc),
                        "context": getattr(exc, "context", {}) or {},
                    }
                )
                return [], {
                    "tool_name": "assign_rules",
                    "skipped": True,
                    "field": field.name,
                    "error": str(exc),
                }

        def _bind_one_field(field):
            field_rules = []
            traces = []
            for _ in range(3):
                payload = {
                    **spec_payload(spec),
                    "fields": [field.model_dump()],
                    "field_names": [item.name for item in spec.fields],
                    "generation_plan": (plans or {}).get(spec.table_code) or {"mutations_per_rule": 1},
                    "existing_rule_keys": [rule.rule_key for rule in field_rules],
                    **_code_set_context(field_code_sets, spec.table_code, field.name),
                }
                rules, trace = _call_validated(
                    llm,
                    "assign_rules",
                    "根据生成计划返回本字段下一条最重要的规则，最多一条；已有规则不得重复，没有更多可靠规则就返回空数组。显性原文与推断拓展必须分开。若提供 published_code_set，ENUM 参数只能取其 values 白名单。COND_REQUIRED 的 if_field 必须严格取自输入 field_names 列表，不得使用简称或臆造字段名。",
                    payload,
                    lambda args: bind_rules(spec, args.get("rules", [])),
                )
                traces.append(trace)
                if not rules:
                    break
                field_rules.extend(rules)
            return field_rules, {
                "provider": "GLM",
                "model": traces[0]["model"] if traces else "unknown",
                "tool_name": "assign_rules",
                "rounds": traces,
                "arguments": {"rules": [rule.model_dump() for rule in field_rules]},
            }

        with ThreadPoolExecutor(max_workers=min(4, len(fields))) as field_executor:
            field_results = list(field_executor.map(bind_field, fields))
        for rules, trace in field_results:
            bound_rules.extend(rules)
            batch_traces.append(trace)
        key_counts = {}
        for rule in bound_rules:
            key_counts[rule.rule_key] = key_counts.get(rule.rule_key, 0) + 1
            if key_counts[rule.rule_key] > 1:
                rule.rule_key = f"{rule.rule_key}_{key_counts[rule.rule_key]}"
                rule.rule_hash = calculate_rule_hash(rule, spec.source_sha256)
        if not bound_rules and gaps:
            raise WorkflowError(
                f"{spec.table_code} 的 {len(gaps)} 个字段全部未能绑定规则，"
                f"首个失败：{gaps[0]['error']}",
                context={"stage": "rule_binder", "table_code": spec.table_code, "field": gaps[0]["field"]},
            )
        return bound_rules, {
            "provider": "GLM",
            "model": (batch_traces[0].get("model") or "unknown") if batch_traces else "unknown",
            "tool_name": "assign_rules",
            "batch_count": len(batch_traces),
            "batches": batch_traces,
            "gaps": gaps,
            "arguments": {"rules": [rule.model_dump() for rule in bound_rules]},
        }

    results = _fanout(session, run, "rule_binder", specs, worker)
    gaps_by_table = {
        spec.table_code: trace["gaps"]
        for spec, (_, trace) in zip(specs, results)
        if trace.get("gaps")
    }
    if gaps_by_table:
        append_event(
            session,
            run,
            "RULE_BIND_GAPS",
            "rule_binder",
            {
                "total": sum(len(items) for items in gaps_by_table.values()),
                "tables": gaps_by_table,
                "note": "这些字段没有绑定到规则（模型调用失败或多次校验不过），需要在规则确认环节人工补充",
            },
        )
    for spec, (rules, _) in zip(specs, results):
        for rule in rules:
            _upsert_rule(session, run, rule, "APPROVED" if rule.source == "DOC_EXPLICIT" else "PENDING")
    append_event(
        session,
        run,
        "BSP_BARRIER_JOIN",
        "rule_barrier",
        {
            "stage": "rule_binder",
            "table_results": [
                {"table_code": spec.table_code, "rule_count": len(rules)}
                for spec, (rules, _) in zip(specs, results)
            ],
        },
    )
    cross_rules = []
    golden_overrides = {}
    if len(specs) > 1:
        selected = {spec.table_code for spec in specs}
        payload = {
            "tables": [spec_payload(spec) for spec in specs],
            "instruction": "只装配有明确依据的跨表 FOREIGN_KEY 规则，并给必要的黄金记录引用值覆盖。",
        }

        def cross_validator(args):
            bound = []
            for item in args.get("rules", []):
                target = next((spec for spec in specs if spec.table_code == item.get("table_code")), None)
                if not target:
                    raise RuleValidationError("Cross rule target table is outside the confirmed set")
                bound.extend(bind_rules(target, [item], selected))
            overrides = args.get("golden_overrides", {})
            if any(code not in selected for code in overrides):
                raise RuleValidationError("Golden override references an unconfirmed table")
            return (bound, overrides)

        (cross_rules, golden_overrides), trace = _call_validated(
            llm,
            "assign_rules",
            "装配跨表 FOREIGN_KEY 规则。rule 必须额外带 table_code；references_table 必须来自已确认表。可返回 golden_overrides 对齐引用值。",
            payload,
            cross_validator,
        )
        append_event(session, run, "AI_TOOL_ASSIGNED", "cross_rule_binder", trace)
        for rule in cross_rules:
            _upsert_rule(session, run, rule, "APPROVED" if rule.source == "DOC_EXPLICIT" else "PENDING")
    append_event(
        session,
        run,
        "CROSS_RULES_ASSIGNED",
        "cross_rule_binder",
        {"rule_keys": [rule.rule_key for rule in cross_rules], "golden_overrides": golden_overrides},
    )
    _dedupe_approved_rules(session, run)
    bound = session.query(RuleModel).filter(RuleModel.run_id == run.id).all()
    pending_count = len(_pending_rules(session, run.id))
    append_event(
        session,
        run,
        "RULES_BOUND",
        "rule_barrier",
        {
            "pending_count": pending_count,
            "rule_set": [
                {"rule_key": rule.rule_key, "rule_hash": rule.rule_hash, "source": rule.source, "table_code": rule.table_code}
                for rule in sorted(bound, key=lambda item: (item.table_code, item.rule_key))
            ],
        },
    )
    return pending_count


def _ensure_oracle_assets(session, run, specs, schema_payload, rules, settings):
    attempt = run.attempt + 1
    existing = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id, EventModel.kind == "ORACLE_ASSETS_PREPARED")
        .order_by(EventModel.seq.desc())
        .first()
    )
    if existing and existing.payload_json.get("attempt") == attempt:
        return existing.payload_json["assets"]

    from .generic_domain import SchemaBinding

    typed_schemas = {
        code: [SchemaBinding.model_validate(item) for item in items]
        for code, items in schema_payload.items()
    }
    code_sets, _ = load_published_code_sets(session, specs)
    assets = GenericOracleExecutor(settings.oracle_sandbox_url).prepare_assets(
        run.id, attempt, specs, typed_schemas, code_sets=code_sets, rules=rules
    )
    blobs = LocalBlobStore(settings.blob_dir)
    ddl_uri = blobs.put(
        "\n\n".join(compile_ai_schema_mysql_ddl(spec.table_code, typed_schemas[spec.table_code]) for spec in specs).encode()
    )
    oracle_ddl_parts = [
        compile_code_set_oracle_ddl(assets["code_set_tables"][code_set["code"]]) for code_set in code_sets
    ] + [
        compile_ai_schema_oracle_ddl(assets["target_tables"][spec.table_code], typed_schemas[spec.table_code])
        for spec in specs
    ]
    oracle_ddl_uri = blobs.put("\n\n".join(oracle_ddl_parts).encode())
    queries = []
    for rule in rules:
        sql, params = compile_rule_query(rule, "oracle", assets["physical_tables"])
        queries.append({"rule_key": rule.rule_key, "table_code": rule.table_code, "sql": sql, "params": params})
    sql_uri = blobs.put(canonical_json({"dialect": "oracle", "queries": queries}).encode())
    result = {
        "ddl_blob": ddl_uri,
        "oracle_ddl_blob": oracle_ddl_uri,
        "validation_sql_blob": sql_uri,
        "oracle_tables": assets["physical_tables"],
        "oracle_target_tables": assets["target_tables"],
        "oracle_code_set_tables": assets["code_set_tables"],
        "oracle_precheck": assets["empty_run"],
    }
    for artifact_type, uri in [("MYSQL_DDL", ddl_uri), ("ORACLE_DDL", oracle_ddl_uri), ("VALIDATION_SQL", sql_uri)]:
        session.add(ArtifactModel(
            id=str(uuid.uuid4()), run_id=run.id, artifact_type=artifact_type, blob_uri=uri,
            sha256=uri.removeprefix("blob://"), version=attempt,
        ))
    append_event(
        session, run, "ORACLE_ASSETS_PREPARED", "oracle_asset_builder",
        {"attempt": attempt, "assets": assets, "result": result},
    )
    session.flush()
    return assets


def _next_asset_version(session, model, run_id, table_code) -> int:
    current = session.query(func.max(model.version)).filter(model.run_id == run_id, model.table_code == table_code).scalar()
    return int(current or 0) + 1


def _data_stage(session, run, specs, llm, active_rules, golden_overrides, plans=None):
    _, field_code_sets = load_published_code_sets(session, specs)

    def worker(spec):
        rule_views = [rule.model_dump() for rule in active_rules if rule.table_code == spec.table_code]

        def bind_field(field):
            payload = {
                **spec_payload(spec),
                "fields": [field.model_dump()],
                "approved_rules": rule_views,
                "required_reference_values": golden_overrides.get(spec.table_code, {}),
                "generation_plan": plans.get(spec.table_code),
                **_code_set_context(field_code_sets, spec.table_code, field.name),
            }

            def validate_one(args):
                items = args.get("generators", [])
                if len(items) != 1 or items[0].get("field") != field.name:
                    raise RuleValidationError("Generator response must contain exactly the requested field")
                return items[0]

            return _call_validated(
                llm,
                "assign_generators",
                "只为输入的一个字段赋予黄金合规生成器。FIXED 使用 params.value；ENUM 使用 params.values。必须满足相关规则和引用值。若提供 published_code_set，ENUM 的 values 只能是其子集。",
                payload,
                validate_one,
            )

        with ThreadPoolExecutor(max_workers=min(4, len(spec.fields))) as field_executor:
            field_results = list(field_executor.map(bind_field, spec.fields))
        generator_items = [item for item, _ in field_results]
        generators = bind_generators(spec, generator_items)
        return generators, {
            "provider": "GLM",
            "model": field_results[0][1]["model"],
            "tool_name": "assign_generators",
            "batch_count": len(field_results),
            "batches": [trace for _, trace in field_results],
            "arguments": {"generators": generator_items},
        }

    results = _fanout(session, run, "data_generator_binder", specs, worker)
    generators = {spec.table_code: result for spec, (result, _) in zip(specs, results)}
    golden_records = {
        spec.table_code: materialize_golden_record(spec, generators[spec.table_code]) for spec in specs
    }
    for table_code, overrides in golden_overrides.items():
        golden_records[table_code].update(overrides)
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
    append_event(
        session,
        run,
        "GENERATORS_ASSIGNED",
        "data_barrier",
        {
            "tables": {
                spec.table_code: {
                    "generators": [item.model_dump() for item in generators[spec.table_code]],
                    "golden_record": golden_records[spec.table_code],
                }
                for spec in specs
            }
        },
    )
    append_event(
        session,
        run,
        "BSP_BARRIER_JOIN",
        "data_barrier",
        {"stage": "data_generator_binder", "table_codes": list(generators)},
    )
    return golden_records


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


def _backfill_required_rules(session, run, specs, rules) -> List[RuleDefinition]:
    """按字典原文机械补齐必填规则。

    必填是最不需要推理的一条：原文写「必填项」就是必填。但 AI 会漏——
    实测 SAFE_6_7_1_2 的 BASERE（担保责任余额）校验原文是
    「必填项，0<=担保责任余额<=保函金额。」，元数据 required=True，
    planner 也明说「BASERE 必填」，最后规则集里一条 BASERE 规则都没有。
    这种漏项不该靠 AI 的注意力，靠原文前缀就能判，所以在这里确定性补齐，
    并留一条 RULE_BACKFILL 事件说明是补的不是 AI 生成的。
    """
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
            append_event(
                session,
                run,
                "RULE_BACKFILL_GAP",
                "rule_backfill",
                {"table_code": spec.table_code, "field": field.name, "error": str(exc)},
            )
            continue
        _upsert_rule(session, run, bound[0], "APPROVED")
        added.append(bound[0])
    if added:
        session.flush()
        append_event(
            session,
            run,
            "RULE_BACKFILL",
            "rule_backfill",
            {
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
    """找出「字典声明了长度、但规则集里没有任何长度约束」的文本字段。

    「字符型，28」能推出且只能推出「最多 28 位」，这是机械结论，不该由模型决定
    要不要生成。实测同一份原文三次运行给出了三个答案：
    LENGTH=28（错，误杀短值）、LENGTH_MAX=28（对）、完全没有长度规则（漏）。
    """
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
            # 长度 0 或超 Oracle VARCHAR2 上限的不是「声明了列宽」，交给码值/格式规则去管
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


def _backfill_length_rules(session, run, specs, rules) -> List[RuleDefinition]:
    """按类型（长度）列机械补齐 LENGTH_MAX。

    LENGTH 已从模型的算子白名单里移除，因为「字符型，N」是列宽上限而不是定长，
    卡成「必须正好 N 位」会把所有短值判违规。但移除之后模型干脆不生成任何长度
    规则了（实测第三个 run 的 EXGUARANCODE 只剩 REQUIRED），
    等于用漏规则换掉错规则。所以这里按声明确定性补上。
    """
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
            append_event(
                session,
                run,
                "RULE_BACKFILL_GAP",
                "rule_backfill",
                {"table_code": spec.table_code, "field": field.name, "operator": "LENGTH_MAX", "error": str(exc)},
            )
            continue
        bound[0].evidence.quote = _declaration_quote(spec, field)
        bound[0].rule_hash = calculate_rule_hash(bound[0], spec.source_sha256)
        _upsert_rule(session, run, bound[0], "APPROVED")
        added.append(bound[0])
    if added:
        session.flush()
        append_event(
            session,
            run,
            "RULE_BACKFILL",
            "rule_backfill",
            {
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
    """找出「原文明确要求本项固定填某个值」的字段。

    这类表述原来被当成码表引用，在码值页堆出一堆空占位表等人去补，
    其实根本没有码表可补。无条件固定值直接生成 ENUM 即可；
    条件性的（「如为个人主体，则填写 N/A」）不在这里处理——非个人主体时
    要填真实名称，卡成固定值会把正常数据全判违规。
    """
    from .code_service import is_conditional_phrase, parse_fixed_value

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


def _backfill_fixed_value_rules(session, run, specs, rules) -> List[RuleDefinition]:
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
            append_event(
                session,
                run,
                "RULE_BACKFILL_GAP",
                "rule_backfill",
                {"table_code": spec.table_code, "field": field.name, "value": value, "error": str(exc)},
            )
            continue
        _upsert_rule(session, run, bound[0], "APPROVED")
        added.append(bound[0])
    if added:
        session.flush()
        append_event(
            session,
            run,
            "RULE_BACKFILL",
            "rule_backfill",
            {
                "total": len(added),
                "rules": [
                    {"table_code": rule.table_code, "rule_key": rule.rule_key, "field": rule.field}
                    for rule in added
                ],
                "note": "原文要求固定取值但 AI 未生成 ENUM 规则，已确定性补齐",
            },
        )
    return rules + added


def _audit_stage(session, run, specs, llm, active_rules, golden_records, plans=None):
    _, field_code_sets = load_published_code_sets(session, specs)

    def worker(spec):
        table_rules = [rule for rule in active_rules if rule.table_code == spec.table_code]
        active_keys = {rule.rule_key for rule in table_rules}
        rule_index = {rule.rule_key: (rule.field, rule.operator, rule.params) for rule in table_rules}

        gaps: List[Dict[str, Any]] = []

        def bind_cases(target_rule=None):
            try:
                return _bind_one_case(target_rule)
            except WorkflowError as exc:
                label = target_rule.rule_key if target_rule else "GOLDEN"
                gaps.append({"target": label, "error": str(exc)})
                if target_rule is None:
                    # 黄金用例是整表基线，缺了就没法判"无违规"这一侧
                    raise
                return [], {
                    "tool_name": "assign_audit_cases",
                    "skipped": True,
                    "target": label,
                    "error": str(exc),
                }

        def _bind_one_case(target_rule=None):
            payload = {
                "table": spec_payload(spec),
                "golden_record": golden_records[spec.table_code],
                "active_rules": [rule.model_dump() for rule in table_rules],
                "target_rule": target_rule.model_dump() if target_rule else None,
                "generation_plan": (plans or {}).get(spec.table_code) or {"mutations_per_rule": 1},
                "published_code_sets": {
                    field.name: _code_set_context(field_code_sets, spec.table_code, field.name)["published_code_set"]
                    for field in spec.fields
                    if (spec.table_code, field.name) in field_code_sets
                },
            }
            instruction = (
                "针对 target_rule 按 generation_plan.mutations_per_rule 设计单点变异用例。"
                "expected_rule_keys 至少要包含 target_rule 自身（这条用例必须打死它）；"
                "同一变异值顺带触发的其它规则不必全部列出，也不要把它们当成额外义务。"
                "变异值必须是真正违规的取值（例如置空用 null、超长要真的超过声明长度），"
                "用空格或制表符冒充空值不算违规。ENUM 负样本应使用 published_code_sets 之外的明确非法值。"
                if target_rule
                else "设计一个无覆盖的黄金合规审计用例，expected_rule_keys 必须为空（黄金用例要求零命中）。"
            )
            return _call_validated(
                llm,
                "assign_audit_cases",
                instruction,
                payload,
                lambda args: bind_audit_cases(spec, args.get("cases", []), active_keys, rule_index),
            )

        targets = [None] + table_rules
        with ThreadPoolExecutor(max_workers=min(4, len(targets))) as audit_executor:
            audit_results = list(audit_executor.map(bind_cases, targets))
        case_items = []
        case_ids = set()
        traces = []
        for cases, trace in audit_results:
            traces.append(trace)
            for case in cases:
                if case.case_id in case_ids:
                    case.case_id = f"{case.case_id}_{len(case_ids) + 1}"
                case_ids.add(case.case_id)
                case_items.append(case)
        return case_items, {
            "provider": "GLM",
            "model": traces[0].get("model") if traces else "unknown",
            "tool_name": "assign_audit_cases",
            "batch_count": len(traces),
            "batches": traces,
            "gaps": gaps,
            "arguments": {"cases": [case.model_dump() for case in case_items]},
        }

    results = _fanout(session, run, "audit_case_binder", specs, worker)
    audit_gaps = {
        spec.table_code: trace["gaps"]
        for spec, (_, trace) in zip(specs, results)
        if trace.get("gaps")
    }
    if audit_gaps:
        append_event(
            session,
            run,
            "AUDIT_CASE_GAPS",
            "audit_case_binder",
            {
                "total": sum(len(items) for items in audit_gaps.values()),
                "tables": audit_gaps,
                "note": "这些规则没有生成对应的变异用例，审计覆盖会不完整",
            },
        )
    cases = {spec.table_code: result for spec, (result, _) in zip(specs, results)}
    for spec in specs:
        session.add(
            AuditCaseDefinitionModel(
                id=str(uuid.uuid4()),
                run_id=run.id,
                table_code=spec.table_code,
                version=_next_asset_version(session, AuditCaseDefinitionModel, run.id, spec.table_code),
                definition_json={"cases": [case.model_dump() for case in cases[spec.table_code]]},
            )
        )
    session.flush()
    append_event(
        session,
        run,
        "AUDIT_CASES_ASSIGNED",
        "audit_barrier",
        {"tables": {code: [item.model_dump() for item in items] for code, items in cases.items()}},
    )
    append_event(
        session,
        run,
        "BSP_BARRIER_JOIN",
        "audit_barrier",
        {"stage": "audit_case_binder", "table_codes": list(cases)},
    )
    return cases


def _dedupe_approved_rules(session: Session, run: RunModel) -> int:
    rows = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED")
        .order_by(RuleModel.source.desc(), RuleModel.created_at, RuleModel.rule_key)
        .all()
    )
    seen = set()
    superseded = []
    for row in rows:
        definition = row.definition_json
        signature = (
            row.table_code,
            definition.get("field"),
            definition.get("operator"),
            canonical_json(definition.get("params", {})),
        )
        if signature in seen:
            row.status = "SUPERSEDED"
            superseded.append({"rule_key": row.rule_key, "rule_hash": row.rule_hash})
        else:
            seen.add(signature)
    if superseded:
        append_event(session, run, "RULE_DUPLICATES_SUPERSEDED", "rule_barrier", {"rules": superseded})
        session.flush()
    return len(superseded)


def _rebind_failed_rules(session, run, specs, llm, report) -> int:
    changed_count = _dedupe_approved_rules(session, run)
    # 只修「该命中却没命中」的规则。黄金用例上的误报（unexpected_rules）不进自动修正：
    # 让 AI 去「调到不报」等于允许它把规则改弱以通过测试，那条规则就废了，
    # 而且原文证据还挂着，事后完全看不出来。误报交给人判断。
    failed_keys = sorted(
        {rule_key for case in report["cases"] for rule_key in case["missing_rules"]}
    )
    if not failed_keys:
        return 0
    rows = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.rule_key.in_(failed_keys), RuleModel.status == "APPROVED")
        .all()
    )
    revised_count = 0
    for row in rows:
        spec = next(item for item in specs if item.table_code == row.table_code)
        current = RuleDefinition.model_validate(row.definition_json)
        failed_cases = [case for case in report["cases"] if row.rule_key in case["missing_rules"]]
        payload = {
            **spec_payload(spec),
            "fields": [next(field for field in spec.fields if field.name == current.field).model_dump()],
            "current_rule": current.model_dump(),
            "failed_cases": failed_cases,
        }

        def validate_revision(args):
            revised = bind_rules(spec, args.get("rules", []))
            if len(revised) != 1:
                raise RuleValidationError("A failed rule must be revised into exactly one rule")
            if revised[0].rule_hash == current.rule_hash:
                raise RuleValidationError("The revised rule is identical to the failed version")
            if revised[0].operator != current.operator:
                # 同 key 换 operator 等于把一条有原文证据的规则偷偷换成另一条：
                # 实测出现过 WABACHANDATE_REQUIRED 被改成 REGEXP，必填检查就此消失，
                # 而 rule_key 和证据页码还留着，事后无法从审计报告看出被替换过。
                raise RuleValidationError(
                    f"修正不能更换 operator（当前 {current.operator}）。"
                    f"若原文还要求 {revised[0].operator}，那是另一条规则，"
                    "请保持当前 operator 只调整 params。"
                )
            revised[0].rule_key = current.rule_key
            revised[0].rule_hash = calculate_rule_hash(revised[0], spec.source_sha256)
            return revised[0]

        try:
            revised, trace = _call_validated(
                llm,
                "assign_rules",
                "根据失败记录修正当前规则赋值。长度一律用 LENGTH_MAX（列宽是上限）；"
                "要卡「必须正好 N 位」请用 REGEXP 写死格式；只返回修正后的一条规则。",
                payload,
                validate_revision,
            )
        except WorkflowError as exc:
            append_event(
                session,
                run,
                "RULE_REBIND_GAP",
                "rule_binder",
                {"table_code": spec.table_code, "rule_key": current.rule_key, "error": str(exc)},
            )
            continue
        append_event(
            session,
            run,
            "AI_TOOL_ASSIGNED",
            "rule_binder",
            {"table_code": spec.table_code, "rebind_for": current.rule_key, **trace},
        )
        row.status = "SUPERSEDED"
        _upsert_rule(session, run, revised, "APPROVED" if revised.source == "DOC_EXPLICIT" else "PENDING")
        revised_count += 1
    return changed_count + revised_count


def retry_generic_run(session: Session, run: RunModel, settings: Settings) -> RunModel:
    report_uri = (run.result_json or {}).get("audit_report_blob")
    if not report_uri:
        redirect = (
            session.query(EventModel)
            .filter(EventModel.run_id == run.id, EventModel.kind == "GO_REDIRECT")
            .order_by(EventModel.seq.desc())
            .first()
        )
        report_uri = ((redirect.payload_json.get("failed_result") or {}).get("audit_report_blob") if redirect else None)
    if not report_uri:
        raise WorkflowError("This failure has no persisted audit report to repair")
    specs = _specs(session, run.spec_id)
    llm = RealDesignLlm(settings)
    report = json.loads(LocalBlobStore(settings.blob_dir).get(report_uri).decode("utf-8"))
    append_event(
        session,
        run,
        "GO_REDIRECT",
        "human_retry",
        {"target": "rule_binder", "reason": "Repair rules from the persisted failed audit before regenerating data"},
    )
    revised = _rebind_failed_rules(session, run, specs, llm, report)
    if not revised:
        raise WorkflowError("No failed active rules could be revised")
    pending_count = len(_pending_rules(session, run.id))
    if pending_count:
        append_event(session, run, "APPROVAL_REQUIRED", "human_gate", {"pending_count": pending_count})
        session.commit()
        return run
    session.commit()
    return continue_generic_run(session, run, settings, llm)


def _latest_payload(session: Session, run_id: str, kind: str) -> Dict[str, Any]:
    event = (
        session.query(EventModel)
        .filter(EventModel.run_id == run_id, EventModel.kind == kind)
        .order_by(EventModel.seq.desc())
        .first()
    )
    if not event:
        raise WorkflowError(f"Missing event payload: {kind}")
    return event.payload_json


def _create_run(session: Session, spec_id: str, settings: Settings) -> RunModel:
    _specs(session, spec_id)  # 校验 spec 存在且已确认；非法则抛 WorkflowError
    run = RunModel(id=str(uuid.uuid4()), spec_id=spec_id, status="NEW", current_node="created")
    session.add(run)
    session.flush()
    specification = session.get(SpecificationModel, spec_id)
    append_event(
        session,
        run,
        "RUN_CREATED",
        "table_confirmation",
        {"spec_id": spec_id, "provider": "REAL_GLM", "confirmation": specification.content_json.get("confirmation")},
    )
    return run


def _execute_run(session: Session, run: RunModel, settings: Settings, llm=None, generation_budget=None) -> RunModel:
    specs = _specs(session, run.spec_id)
    llm = llm or RealDesignLlm(settings)
    current_stage = "created"
    try:
        current_stage = "generation_planner"
        plans = _plan_stage(session, run, specs, llm, generation_budget) if generation_budget else None
        session.commit()
        current_stage = "schema_mapper"
        _schema_stage(session, run, specs, llm)
        session.commit()
        current_stage = "rule_binder"
        pending_count = _rules_stage(session, run, specs, llm, plans)
        session.commit()
        current_stage = "oracle_asset_builder"
        schema_payload = _latest_payload(session, run.id, "SCHEMA_ASSIGNED")["tables"]
        generated_rules = [
            RuleDefinition.model_validate(row.definition_json)
            for row in session.query(RuleModel).filter(RuleModel.run_id == run.id, RuleModel.status != "SUPERSEDED").all()
        ]
        _ensure_oracle_assets(session, run, specs, schema_payload, generated_rules, settings)
        session.commit()
    except Exception as exc:
        append_event(
            session,
            run,
            "RUN_FAILED",
            current_stage,
            {
                "node": current_stage,
                "result": {
                    "error": f"{type(exc).__name__}: {exc}",
                    "error_details": _error_details(exc, current_stage),
                },
            },
        )
        session.commit()
        return run
    if pending_count:
        append_event(
            session,
            run,
            "APPROVAL_REQUIRED",
            "human_gate",
            {"pending_rule_hashes": [rule.rule_hash for rule in _pending_rules(session, run.id)]},
        )
        session.commit()
        return run
    session.commit()
    return continue_generic_run(session, run, settings, llm)


def start_generic_run(session: Session, spec_id: str, settings: Settings, llm=None, generation_budget=None) -> RunModel:
    """同步入口：创建 run 并执行到第一个 gate。测试与内部调用使用。"""
    run = _create_run(session, spec_id, settings)
    return _execute_run(session, run, settings, llm, generation_budget)


def approve_generic_rules(
    session: Session,
    run: RunModel,
    approver: str,
    decisions: Dict[str, str],
    settings: Settings,
    llm=None,
) -> RunModel:
    if run.status != "WAITING_APPROVAL":
        raise WorkflowError("Run is not waiting for rule approval")
    pending = {rule.rule_hash: rule for rule in _pending_rules(session, run.id)}
    if not decisions or any(rule_hash not in pending for rule_hash in decisions):
        raise WorkflowError("Decisions must target currently pending rules")
    normalized = []
    for rule_hash, decision in decisions.items():
        if decision not in {"APPROVED", "REJECTED"}:
            raise WorkflowError("Decision must be APPROVED or REJECTED")
        pending[rule_hash].status = decision
        normalized.append({"rule_hash": rule_hash, "decision": decision})
        session.add(
            ApprovalModel(
                id=str(uuid.uuid4()),
                object_type="RULE",
                object_hash=rule_hash,
                decision=decision,
                approver=approver,
                comment="",
            )
        )
    session.flush()
    all_decided = not _pending_rules(session, run.id)
    append_event(
        session,
        run,
        "HUMAN_DECISIONS_RECORDED",
        "human_gate",
        {"approver": approver, "decisions": normalized, "all_decided": all_decided},
    )
    session.commit()
    return continue_generic_run(session, run, settings, llm) if all_decided else run


def confirm_generic_test_results(session: Session, run: RunModel, approver: str) -> RunModel:
    if run.status != "WAITING_TEST_APPROVAL":
        raise WorkflowError("Run is not waiting for test result confirmation")
    summary = (run.result_json or {}).get("audit_summary", {})
    if not summary.get("all_passed"):
        raise WorkflowError("Test results can only be confirmed after all Oracle checks pass")
    report_uri = run.result_json.get("audit_report_blob", "")
    object_hash = hashlib.sha256(f"{run.id}:{report_uri}".encode()).hexdigest()
    session.add(
        ApprovalModel(
            id=str(uuid.uuid4()),
            object_type="TEST_RESULTS",
            object_hash=object_hash,
            decision="APPROVED",
            approver=approver,
            comment="",
        )
    )
    append_event(
        session,
        run,
        "TEST_RESULTS_CONFIRMED",
        "test_confirmation",
        {"approver": approver, "object_hash": object_hash},
    )
    session.commit()
    return run


def _continue_generic_run(session: Session, run: RunModel, settings: Settings, llm=None) -> RunModel:
    specs = _specs(session, run.spec_id)
    llm = llm or RealDesignLlm(settings)
    schema_payload = _latest_payload(session, run.id, "SCHEMA_ASSIGNED")["tables"]
    schemas = {code: [item for item in items] for code, items in schema_payload.items()}
    cross = _latest_payload(session, run.id, "CROSS_RULES_ASSIGNED")
    plan_event = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id, EventModel.kind == "GENERATION_PLANNED")
        .order_by(EventModel.seq.desc())
        .first()
    )
    plans = plan_event.payload_json["tables"] if plan_event else {}
    rows = session.query(RuleModel).filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED").all()
    rules = [RuleDefinition.model_validate(row.definition_json) for row in rows]
    rules = _backfill_required_rules(session, run, specs, rules)
    rules = _backfill_length_rules(session, run, specs, rules)
    rules = _backfill_fixed_value_rules(session, run, specs, rules)
    prepared_assets = _ensure_oracle_assets(session, run, specs, schema_payload, rules, settings)
    golden_records = _data_stage(session, run, specs, llm, rules, cross.get("golden_overrides", {}), plans)
    session.commit()
    audit_cases = _audit_stage(session, run, specs, llm, rules, golden_records, plans)
    session.commit()
    attempt = run.attempt + 1
    append_event(session, run, "AUDIT_STARTED", "oracle_executor", {"attempt": attempt})
    schema_models = {code: [item for item in items] for code, items in schema_payload.items()}
    from .generic_domain import GeneratorBinding, SchemaBinding

    generator_payload = _latest_payload(session, run.id, "GENERATORS_ASSIGNED")["tables"]
    golden_generators = {
        code: [GeneratorBinding.model_validate(item) for item in values["generators"]]
        for code, values in generator_payload.items()
    }
    golden_counts = {code: int(plan.get("golden_count", 1)) for code, plan in plans.items()}
    typed_schemas = {
        code: [SchemaBinding.model_validate(item) for item in items] for code, items in schema_models.items()
    }
    code_sets, field_code_sets = load_published_code_sets(session, specs)
    validate_code_set_usage(rules, golden_generators, field_code_sets)
    report = GenericOracleExecutor(settings.oracle_sandbox_url).execute(
        run.id,
        attempt,
        specs,
        typed_schemas,
        golden_records,
        audit_cases,
        rules,
        golden_counts,
        golden_generators,
        code_sets=code_sets,
        prepared_assets=prepared_assets,
    )
    blobs = LocalBlobStore(settings.blob_dir)
    if report.get("uncovered_rules"):
        append_event(
            session,
            run,
            "RULE_COVERAGE_GAP",
            "oracle_executor",
            {
                "coverage_rate": report.get("coverage_rate"),
                "uncovered_rules": report["uncovered_rules"],
                "note": "这些规则没有任何变异用例命中过，等于没被验证；审计通过不代表它们对",
            },
        )
    ddl_uri = blobs.put(
        "\n\n".join(compile_ai_schema_mysql_ddl(spec.table_code, typed_schemas[spec.table_code]) for spec in specs).encode()
    )
    physical_tables = report["physical_tables"]
    oracle_ddl_parts = [
        compile_code_set_oracle_ddl(report["code_set_tables"][code_set["code"]]) for code_set in code_sets
    ] + [
        compile_ai_schema_oracle_ddl(report["target_tables"][spec.table_code], typed_schemas[spec.table_code])
        for spec in specs
    ]
    oracle_ddl_uri = blobs.put("\n\n".join(oracle_ddl_parts).encode())
    queries = []
    for rule in rules:
        sql, params = compile_rule_query(rule, "oracle", physical_tables)
        queries.append({"rule_key": rule.rule_key, "table_code": rule.table_code, "sql": sql, "params": params})
    sql_uri = blobs.put(canonical_json({"dialect": "oracle", "queries": queries}).encode())
    report_uri = blobs.put(canonical_json(report).encode())
    result = {
        "ddl_blob": ddl_uri,
        "oracle_ddl_blob": oracle_ddl_uri,
        "validation_sql_blob": sql_uri,
        "audit_report_blob": report_uri,
        "oracle_tables": physical_tables,
        "oracle_target_tables": report["target_tables"],
        "oracle_code_set_tables": report["code_set_tables"],
        "active_rule_hashes": sorted(rule.rule_hash for rule in rules),
        "audit_summary": {key: value for key, value in report.items() if key not in {"cases", "physical_tables", "target_tables"}},
    }
    for artifact_type, uri in [
        ("MYSQL_DDL", ddl_uri),
        ("ORACLE_DDL", oracle_ddl_uri),
        ("VALIDATION_SQL", sql_uri),
        ("AUDIT_REPORT", report_uri),
    ]:
        session.add(
            ArtifactModel(
                id=str(uuid.uuid4()),
                run_id=run.id,
                artifact_type=artifact_type,
                blob_uri=uri,
                sha256=uri.removeprefix("blob://"),
                version=attempt,
            )
        )
    session.flush()
    if not report["all_passed"] and attempt < 3:
        append_event(
            session,
            run,
            "GO_REDIRECT",
            "oracle_executor",
            {"target": "rule_binder", "failed_result": result},
        )
        revised = _rebind_failed_rules(session, run, specs, llm, report)
        if revised:
            pending_count = len(_pending_rules(session, run.id))
            if pending_count:
                append_event(session, run, "APPROVAL_REQUIRED", "human_gate", {"pending_count": pending_count})
                session.commit()
                return run
            return continue_generic_run(session, run, settings, llm)
    append_event(
        session,
        run,
        "AUDIT_COMPLETED" if report["all_passed"] else "AUDIT_REJECTED",
        "oracle_executor",
        {"result": result, "node": "oracle_executor"},
    )
    if report["all_passed"]:
        append_event(
            session,
            run,
            "TEST_CONFIRMATION_REQUIRED",
            "test_confirmation",
            {"audit_report_blob": report_uri, "active_rule_hashes": result["active_rule_hashes"]},
        )
    session.commit()
    return run


def continue_generic_run(session: Session, run: RunModel, settings: Settings, llm=None) -> RunModel:
    try:
        return _continue_generic_run(session, run, settings, llm)
    except Exception as exc:
        fallback_stage = run.current_node or "oracle_executor"
        append_event(
            session,
            run,
            "RUN_FAILED",
            fallback_stage,
            {
                "node": fallback_stage,
                "result": {
                    "error": f"{type(exc).__name__}: {exc}",
                    "error_details": _error_details(exc, fallback_stage),
                },
            },
        )
        session.commit()
        return run
