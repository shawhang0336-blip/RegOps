"""阶段二子模块 1：生成预算规划（_plan_stage）与确定性表结构映射（_schema_stage）。"""

import uuid
from typing import Any, Dict, List, Optional

from app.engine.binding import compile_schema_bindings
from app.engine.rules import RuleValidationError
from app.engine.workflow.runtime import _call_validated, _emit, _fanout
from app.infra.db_tables import GenerationPlanModel, SchemaDefinitionModel
from app.infra.blob_store import LocalBlobStore
from app.infra.contracts import canonical_json
from app.infra.llm.schemas import spec_payload


def _provider_name(llm, trace: Optional[Dict[str, Any]] = None) -> str:
    if isinstance(trace, dict) and trace.get("provider"):
        return str(trace["provider"])
    settings = getattr(llm, "settings", None)
    if settings is not None and getattr(settings, "llm_provider", None):
        return str(settings.llm_provider)
    return "unknown"


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
    estimated = plan["golden_count"] + plan["mutations_per_rule"] * (
        plan["mandatory_rule_target"] + plan["expanded_rule_target"]
    )
    if estimated > budget["max_cases_per_table"]:
        raise RuleValidationError("Estimated audit cases exceed the configured budget")
    return plan


def _plan_stage(
    session,
    run,
    specs,
    llm,
    budget,
    concurrency: int = 4,
    bus=None,
    events: Optional[List[tuple]] = None,
    cancellation: Any = None,
):
    def worker(spec):
        return _call_validated(
            llm,
            "assign_generation_plan",
            "根据表复杂度自行决定必做规则数、拓展规则数、黄金数据量和每规则变异数，并说明理由；不得超出输入预算。",
            {**spec_payload(spec), "budget": budget},
            lambda args: _validate_plan(args, budget),
            bus=bus,
        )

    results = _fanout(
        session,
        run,
        "generation_planner",
        specs,
        worker,
        max_workers=concurrency,
        events=events,
        bus=bus,
        cancellation=cancellation,
    )
    plans = {spec.table_code: result for spec, (result, _) in zip(specs, results)}
    _emit(
        events,
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
    _emit(
        events,
        session,
        run,
        "BSP_BARRIER_JOIN",
        "plan_barrier",
        {"stage": "generation_planner", "table_codes": list(plans)},
    )
    return plans


def _schema_stage(
    session,
    run,
    specs,
    llm,
    concurrency: int = 4,
    bus=None,
    events: Optional[List[tuple]] = None,
    blob_store: Optional[LocalBlobStore] = None,
):
    """确定性 Schema 映射阶段：根据已确认字段元数据直接编译物理存储类型，不消耗 AI 调用。"""
    schemas = {spec.table_code: compile_schema_bindings(spec) for spec in specs}
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
    tables_payload = {code: [item.model_dump() for item in items] for code, items in schemas.items()}
    event_payload: Dict[str, Any] = {
        "strategy": "DETERMINISTIC_SCHEMA_COMPILER",
        "tables": tables_payload,
    }
    if blob_store is not None:
        event_payload["tables_blob"] = blob_store.put(canonical_json(tables_payload).encode("utf-8"))
    _emit(
        events,
        session,
        run,
        "SCHEMA_ASSIGNED",
        "schema_barrier",
        event_payload,
    )
    _emit(
        events,
        session,
        run,
        "BSP_BARRIER_JOIN",
        "schema_barrier",
        {"stage": "schema_mapper", "table_codes": list(schemas)},
    )
    return tables_payload
