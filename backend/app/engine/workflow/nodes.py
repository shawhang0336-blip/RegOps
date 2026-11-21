"""ProdAgent 节点体装配（Workflow Nodes）。

每个节点严格遵守 `body(input, ctx: RunContext) -> Outcome` 契约：
- 只负责本阶段逻辑，绝不直接调用下游节点；
- 多表规则绑定、黄金造数、变异用例三大阶段均采用**真 BSP `Send` 动态扇出 → 单表并行 Worker（零 DB 写操作）→ 主线程 Join 屏障汇聚**，
  每张表独立落盘增量检查点（`TABLE_RULES_BOUND` / `TABLE_GENERATORS_ASSIGNED` / `TABLE_AUDIT_CASES_ASSIGNED`），
  单表失败中断后再次续跑自动复用已完成表，绝不重跑已付费成果；
- 遇到人工确权/测试确认通过一等 `Interrupt` 挂起；遇到 Oracle 审计缺失命中通过 `Outcome.goto` 交回 Scheduler 调度自愈。
"""

import uuid
from typing import Any, Dict, List

from app.assets.code_sets.oracle import (
    code_set_compile_bindings,
    compile_code_set_oracle_ddl,
    load_published_code_sets,
    sandbox_code_set_table,
    validate_code_set_usage,
)
from app.engine.binding import GeneratorBinding, SchemaBinding
from app.engine.compiler import compile_rule_query
from app.engine.oracle import GenericOracleExecutor, compile_ai_schema_mysql_ddl, compile_ai_schema_oracle_ddl
from app.engine.rule_backfill import derive_deterministic_rules as _policy_derive_deterministic_rules
from app.engine.workflow.runtime import _emit, _latest_payload, _pending_rules, _upsert_rule
from app.engine.workflow.stage_audit import bind_table_audit_worker, join_table_audit_barrier
from app.engine.workflow.stage_data import (
    _backfill_fixed_value_rules,
    _backfill_length_rules,
    _backfill_required_rules,
    bind_table_data_worker,
    join_table_data_barrier,
)
from app.engine.workflow.stage_plan_schema import _plan_stage, _schema_stage
from app.engine.workflow.stage_rules import (
    _ensure_oracle_assets,
    _rebind_failed_rules,
    bind_table_rules_worker,
    join_table_rules_barrier,
    prepare_deterministic_rule_index,
)
from app.infra.asset_models import ArtifactModel
from app.infra.blob_store import LocalBlobStore
from app.infra.domain import RuleDefinition, canonical_json
from app.infra.errors import WorkflowError
from app.infra.models import RuleModel
from app.kernel.interrupt import Interrupt
from app.kernel.run import RunContext
from app.kernel.scheduler import Outcome, Send
from app.policy.regops_plan import (
    self_heal_cases_target,
    self_heal_data_target,
    self_heal_target,
)

_DETERMINISTIC_RULE_SUFFIXES = ("_REQUIRED", "_LENGTH_MAX", "_FIXED_VALUE_ENUM", "_CODE_SET_ENUM")


def _is_mechanically_derived_rule(rule_key: str, rule_map: Dict[str, RuleDefinition]) -> bool:
    rule = rule_map.get(rule_key)
    if rule and "非 AI 生成" in (rule.rationale or ""):
        return True
    return any(rule_key.endswith(suffix) for suffix in _DETERMINISTIC_RULE_SUFFIXES)


def _front_bodies(specs, llm, budget, concurrency, settings):
    """前半段节点 body：规划 → 表结构 → 真 BSP Send 规则扇出/汇聚 → Oracle 资产预建 & 人工确权门。"""
    spec_by_code = {spec.table_code: spec for spec in specs}

    def body_generation_planner(_input, ctx: RunContext) -> Outcome:
        events: List[tuple] = []
        plans = (
            _plan_stage(
                ctx.session,
                ctx.run,
                specs,
                ctx.llm,
                ctx.budget,
                ctx.concurrency,
                bus=ctx.bus,
                events=events,
                cancellation=ctx.cancellation,
            )
            if ctx.get("use_plan")
            else None
        )
        return Outcome(output=plans, shared={"plans": plans or {}}, events=events)

    def body_schema_mapper(_input, ctx: RunContext) -> Outcome:
        events: List[tuple] = []
        blobs = LocalBlobStore(ctx.settings.blob_dir)
        schema_payload = _schema_stage(
            ctx.session,
            ctx.run,
            specs,
            ctx.llm,
            ctx.concurrency,
            bus=ctx.bus,
            events=events,
            blob_store=blobs,
        )
        return Outcome(shared={"schema_payload": schema_payload}, events=events)

    def body_rule_binder(_input, ctx: RunContext) -> Outcome:
        events: List[tuple] = []
        self_heal_report = ctx.shared.pop("self_heal_report", None)
        if self_heal_report is not None:
            # 自愈或人工重试回边：只靶向重绑缺失命中的失败规则，绝不全表重新提取
            origin = ctx.get("approval_origin") or "self_heal"
            revised = _rebind_failed_rules(
                ctx.session,
                ctx.run,
                specs,
                ctx.llm,
                self_heal_report,
                bus=ctx.bus,
                events=events,
            )
            if origin == "human_retry" and not revised:
                raise WorkflowError("No failed active rules could be revised")
            bound = ctx.session.query(RuleModel).filter(RuleModel.run_id == ctx.run.id).all()
            pending_count = len(_pending_rules(ctx.session, ctx.run.id))
            events.append(
                (
                    "RULES_BOUND",
                    "rule_barrier",
                    {
                        "pending_count": pending_count,
                        "origin": origin,
                        "revised_count": revised,
                        "rule_set": [
                            {
                                "rule_key": rule.rule_key,
                                "rule_hash": rule.rule_hash,
                                "source": rule.source,
                                "table_code": rule.table_code,
                            }
                            for rule in sorted(bound, key=lambda item: (item.table_code, item.rule_key))
                        ],
                    },
                )
            )
            return Outcome(
                shared={"pending_count": pending_count, "approval_origin": origin},
                events=events,
                when="direct",
            )

        _, field_code_sets = load_published_code_sets(ctx.session, specs)
        deterministic_rules = _policy_derive_deterministic_rules(
            ctx.session,
            ctx.run,
            specs,
            _upsert_rule,
            _emit,
            field_code_sets=field_code_sets,
            events=events,
        )
        det_index = prepare_deterministic_rule_index(deterministic_rules)
        done_tables = ctx.get("table_rules_done") or {}
        missing_specs = [spec for spec in specs if spec.table_code not in done_tables]
        wave_ctx = {
            "field_code_sets": field_code_sets,
            "det_index": det_index,
            "outer_workers": max(1, min(ctx.concurrency, len(missing_specs) or 1)),
        }
        if not missing_specs:
            return Outcome(
                shared={"rule_wave_ctx": wave_ctx, "approval_origin": "rule_binder"},
                events=events,
                when="skip_fanout",
            )
        events.append(
            (
                "BSP_FANOUT",
                "rule_binder",
                {
                    "stage": "rule_binder",
                    "table_codes": [spec.table_code for spec in missing_specs],
                    "reused_tables": sorted(done_tables),
                },
            )
        )
        return Outcome(
            shared={"rule_wave_ctx": wave_ctx, "approval_origin": "rule_binder"},
            events=events,
            sends=[Send("rule_table_worker", spec.table_code, label=spec.table_code) for spec in missing_specs],
            when="fanout",
        )

    def body_rule_table_worker(table_code: str, ctx: RunContext) -> Outcome:
        """纯单表规则绑定并发 Worker（零 DB 写操作；结果由 Scheduler 屏障落账并增量归约）。"""
        spec = spec_by_code[table_code]
        wave_ctx = ctx.get("rule_wave_ctx") or {}
        rules, trace = bind_table_rules_worker(
            spec,
            llm=ctx.llm,
            plans=ctx.get("plans"),
            field_code_sets=wave_ctx.get("field_code_sets") or {},
            det_index=wave_ctx.get("det_index") or prepare_deterministic_rule_index([]),
            concurrency=ctx.concurrency,
            outer_workers=int(wave_ctx.get("outer_workers") or 1),
            bus=ctx.bus,
        )
        serialized_rules = [rule.model_dump() for rule in rules]
        gaps = trace.get("gaps") or []
        return Outcome(
            shared={
                "table_rules_done": {
                    table_code: {"rules": serialized_rules, "gaps": gaps},
                }
            },
            events=[
                ("AI_TOOL_ASSIGNED", "rule_binder", {"table_code": table_code, **trace}),
                (
                    "TABLE_RULES_BOUND",
                    "rule_table_worker",
                    {
                        "table_code": table_code,
                        "rule_count": len(serialized_rules),
                        "rules": serialized_rules,
                        "gaps": gaps,
                    },
                ),
            ],
        )

    def body_rule_join(_input, ctx: RunContext) -> Outcome:
        events: List[tuple] = []
        table_results = ctx.get("table_rules_done") or {}
        pending_count, golden_overrides = join_table_rules_barrier(
            ctx.session,
            ctx.run,
            specs,
            ctx.llm,
            table_results,
            bus=ctx.bus,
            events=events,
        )
        return Outcome(
            shared={
                "pending_count": pending_count,
                "golden_overrides": golden_overrides,
                "approval_origin": "rule_binder",
            },
            events=events,
        )

    def body_oracle_asset_builder(_input, ctx: RunContext) -> Outcome:
        events: List[tuple] = []
        schema_payload = ctx.get("schema_payload")
        generated_rules = [
            RuleDefinition.model_validate(row.definition_json)
            for row in ctx.session.query(RuleModel)
            .filter(RuleModel.run_id == ctx.run.id, RuleModel.status != "SUPERSEDED")
            .all()
        ]
        assets = _ensure_oracle_assets(
            ctx.session,
            ctx.run,
            specs,
            schema_payload,
            generated_rules,
            ctx.settings,
            events=events,
            attempt=ctx.state.attempt + 1,
        )
        if ctx.budget_guard is not None and hasattr(ctx.budget_guard, "snapshot"):
            budget_snap = ctx.budget_guard.snapshot()
            for kind, _ev_node, ev_payload in reversed(events):
                if kind == "ORACLE_ASSETS_PREPARED" and isinstance(ev_payload, dict):
                    if isinstance(ev_payload.get("result"), dict):
                        ev_payload["result"]["budget_usage"] = budget_snap
                    break
        pending_count = len(_pending_rules(ctx.session, ctx.run.id))
        origin = ctx.get("approval_origin") or "rule_binder"
        interrupt_cmd = None
        if pending_count:
            if origin == "self_heal":
                question = f"Oracle 自愈修正后有 {pending_count} 条规则需要人工确认，请批准或拒绝后重新验证。"
            elif origin == "human_retry":
                question = f"重试修复后有 {pending_count} 条规则需要人工确认，请批准或拒绝后继续生成。"
            else:
                question = f"AI 推断出 {pending_count} 条规则需要人工确认，请批准或拒绝后继续生成。"
            pending_meta: Dict[str, Any] = {
                "pending_count": pending_count,
                "pending_rule_hashes": [rule.rule_hash for rule in _pending_rules(ctx.session, ctx.run.id)],
                "origin": origin,
            }
            if origin == "self_heal":
                pending_meta["attempt"] = ctx.state.attempt
            interrupt_cmd = Interrupt(
                kind="APPROVAL_REQUIRED",
                node="human_gate",
                question=question,
                pending_input=pending_meta,
                resume_node="human_gate",
                identity=ctx.identity.as_payload(),
            )
        when = "pending_rules" if pending_count else "no_pending_rules"
        return Outcome(
            shared={"rules": generated_rules, "prepared_assets": assets, "pending_count": pending_count},
            events=events,
            when=when,
            interrupt=interrupt_cmd,
        )

    return {
        "generation_planner": body_generation_planner,
        "schema_mapper": body_schema_mapper,
        "rule_binder": body_rule_binder,
        "rule_table_worker": body_rule_table_worker,
        "rule_join": body_rule_join,
        "oracle_asset_builder": body_oracle_asset_builder,
    }


def _back_bodies(specs, settings):
    """后半段节点 body：真 BSP Send 造数扇出/汇聚 → 真 BSP Send 变异用例扇出/汇聚 → Oracle 沙箱审计与确定性归因自愈。"""
    spec_by_code = {spec.table_code: spec for spec in specs}

    def body_data_generator_binder(_input, ctx: RunContext) -> Outcome:
        events: List[tuple] = []
        schema_payload = ctx.get("schema_payload")
        heal_data_report = ctx.shared.pop("heal_data_report", None)
        rows = ctx.session.query(RuleModel).filter(RuleModel.run_id == ctx.run.id, RuleModel.status == "APPROVED").all()
        rules = [RuleDefinition.model_validate(row.definition_json) for row in rows]
        rules = _backfill_required_rules(ctx.session, ctx.run, specs, rules, events=events)
        rules = _backfill_length_rules(ctx.session, ctx.run, specs, rules, events=events)
        rules = _backfill_fixed_value_rules(ctx.session, ctx.run, specs, rules, events=events)
        prepared_assets = _ensure_oracle_assets(
            ctx.session,
            ctx.run,
            specs,
            schema_payload,
            rules,
            ctx.settings,
            events=events,
            attempt=ctx.state.attempt + 1,
        )
        _, field_code_sets = load_published_code_sets(ctx.session, specs)

        done_tables = dict(ctx.get("table_generators_done") or {})
        if heal_data_report is not None:
            affected = {
                c.get("table_code")
                for c in heal_data_report.get("cases", [])
                if c.get("case_kind") == "GOLDEN" and c.get("unexpected_rules") and c.get("table_code")
            }
            for tcode in affected or {s.table_code for s in specs}:
                done_tables.pop(tcode, None)
                if isinstance(ctx.shared.get("table_generators_done"), dict):
                    ctx.shared["table_generators_done"].pop(tcode, None)
                if isinstance(ctx.shared.get("table_audit_cases_done"), dict):
                    ctx.shared["table_audit_cases_done"].pop(tcode, None)

        missing_specs = [spec for spec in specs if spec.table_code not in done_tables]
        wave_ctx = {
            "field_code_sets": field_code_sets,
            "heal_report": heal_data_report,
        }
        shared_base = {
            "rules": rules,
            "prepared_assets": prepared_assets,
            "data_wave_ctx": wave_ctx,
        }
        if not missing_specs:
            return Outcome(shared=shared_base, events=events, when="skip_fanout")

        events.append(
            (
                "BSP_FANOUT",
                "data_generator_binder",
                {
                    "stage": "data_generator_binder",
                    "table_codes": [spec.table_code for spec in missing_specs],
                    "reused_tables": sorted(done_tables),
                },
            )
        )
        return Outcome(
            shared=shared_base,
            events=events,
            sends=[Send("data_table_worker", spec.table_code, label=spec.table_code) for spec in missing_specs],
            when="fanout",
        )

    def body_data_table_worker(table_code: str, ctx: RunContext) -> Outcome:
        """纯单表黄金造数并发 Worker（零 DB 写操作；支持单表级增量续跑）。"""
        spec = spec_by_code[table_code]
        wave_ctx = ctx.get("data_wave_ctx") or {}
        generators, trace = bind_table_data_worker(
            spec,
            llm=ctx.llm,
            active_rules=ctx.get("rules") or [],
            golden_overrides=ctx.get("golden_overrides") or {},
            field_code_sets=wave_ctx.get("field_code_sets") or {},
            plans=ctx.get("plans") or {},
            bus=ctx.bus,
            heal_report=wave_ctx.get("heal_report"),
        )
        serialized = [item.model_dump() for item in generators]
        return Outcome(
            shared={"table_generators_done": {table_code: serialized}},
            events=[
                ("AI_TOOL_ASSIGNED", "data_generator_binder", {"table_code": table_code, **trace}),
                (
                    "TABLE_GENERATORS_ASSIGNED",
                    "data_table_worker",
                    {"table_code": table_code, "generators": serialized},
                ),
            ],
        )

    def body_data_join(_input, ctx: RunContext) -> Outcome:
        events: List[tuple] = []
        blobs = LocalBlobStore(ctx.settings.blob_dir)
        golden_records, generator_payload = join_table_data_barrier(
            ctx.session,
            ctx.run,
            specs,
            ctx.get("rules") or [],
            ctx.get("golden_overrides") or {},
            ctx.get("table_generators_done") or {},
            plans=ctx.get("plans") or {},
            events=events,
            blob_store=blobs,
        )
        return Outcome(
            shared={"golden_records": golden_records, "generator_payload": generator_payload},
            events=events,
        )

    def body_audit_case_binder(_input, ctx: RunContext) -> Outcome:
        events: List[tuple] = []
        heal_cases_report = ctx.shared.pop("heal_cases_report", None)
        _, field_code_sets = load_published_code_sets(ctx.session, specs)

        done_tables = dict(ctx.get("table_audit_cases_done") or {})
        if heal_cases_report is not None:
            affected = {
                c.get("table_code")
                for c in heal_cases_report.get("cases", [])
                if c.get("missing_rules") and c.get("table_code")
            }
            for tcode in affected or {s.table_code for s in specs}:
                done_tables.pop(tcode, None)
                if isinstance(ctx.shared.get("table_audit_cases_done"), dict):
                    ctx.shared["table_audit_cases_done"].pop(tcode, None)

        missing_specs = [spec for spec in specs if spec.table_code not in done_tables]
        wave_ctx = {
            "field_code_sets": field_code_sets,
            "heal_report": heal_cases_report,
            "outer_workers": max(1, min(ctx.concurrency, len(missing_specs) or 1)),
        }
        if not missing_specs:
            return Outcome(shared={"audit_wave_ctx": wave_ctx}, events=events, when="skip_fanout")

        events.append(
            (
                "BSP_FANOUT",
                "audit_case_binder",
                {
                    "stage": "audit_case_binder",
                    "table_codes": [spec.table_code for spec in missing_specs],
                    "reused_tables": sorted(done_tables),
                },
            )
        )
        return Outcome(
            shared={"audit_wave_ctx": wave_ctx},
            events=events,
            sends=[Send("audit_table_worker", spec.table_code, label=spec.table_code) for spec in missing_specs],
            when="fanout",
        )

    def body_audit_table_worker(table_code: str, ctx: RunContext) -> Outcome:
        """纯单表审计变异用例并发 Worker（零 DB 写操作；支持单表级增量续跑）。"""
        spec = spec_by_code[table_code]
        wave_ctx = ctx.get("audit_wave_ctx") or {}
        golden_records = ctx.get("golden_records") or {}
        case_items, trace = bind_table_audit_worker(
            spec,
            llm=ctx.llm,
            active_rules=ctx.get("rules") or [],
            golden_record=golden_records[table_code],
            field_code_sets=wave_ctx.get("field_code_sets") or {},
            plans=ctx.get("plans") or {},
            concurrency=ctx.concurrency,
            outer_workers=int(wave_ctx.get("outer_workers") or 1),
            bus=ctx.bus,
            heal_report=wave_ctx.get("heal_report"),
        )
        serialized_cases = [item.model_dump() for item in case_items]
        gaps = trace.get("gaps") or []
        return Outcome(
            shared={
                "table_audit_cases_done": {
                    table_code: {"cases": serialized_cases, "gaps": gaps},
                }
            },
            events=[
                ("AI_TOOL_ASSIGNED", "audit_case_binder", {"table_code": table_code, **trace}),
                (
                    "TABLE_AUDIT_CASES_ASSIGNED",
                    "audit_table_worker",
                    {
                        "table_code": table_code,
                        "case_count": len(serialized_cases),
                        "cases": serialized_cases,
                        "gaps": gaps,
                    },
                ),
            ],
        )

    def body_audit_join(_input, ctx: RunContext) -> Outcome:
        events: List[tuple] = []
        blobs = LocalBlobStore(ctx.settings.blob_dir)
        audit_cases = join_table_audit_barrier(
            ctx.session,
            ctx.run,
            specs,
            ctx.get("table_audit_cases_done") or {},
            events=events,
            blob_store=blobs,
        )
        return Outcome(shared={"audit_cases": audit_cases}, events=events)

    def body_oracle_executor(_input, ctx: RunContext) -> Outcome:
        run = ctx.run
        schema_payload = ctx.get("schema_payload")
        plans = ctx.get("plans") or {}
        rules = ctx.get("rules")
        golden_records = ctx.get("golden_records")
        audit_cases = ctx.get("audit_cases")
        prepared_assets = ctx.get("prepared_assets")
        attempt = ctx.state.attempt + 1
        first_attempt = ctx.attempt_base or attempt
        max_attempts = int((ctx.budget or {}).get("max_retries", 3))

        generator_payload = ctx.get("generator_payload") or _latest_payload(ctx.session, run.id, "GENERATORS_ASSIGNED")["tables"]
        golden_generators = {
            code: [GeneratorBinding.model_validate(item) for item in values["generators"]]
            for code, values in generator_payload.items()
        }
        golden_counts = {code: int(plan.get("golden_count", 1)) for code, plan in plans.items()}
        typed_schemas = {
            code: [SchemaBinding.model_validate(item) for item in items]
            for code, items in schema_payload.items()
        }
        code_sets, field_code_sets = load_published_code_sets(ctx.session, specs)
        validate_code_set_usage(rules, golden_generators, field_code_sets)
        code_set_bindings = code_set_compile_bindings(
            field_code_sets,
            {item["code"]: sandbox_code_set_table(run.id, attempt, item["code"]) for item in code_sets},
        )
        report = GenericOracleExecutor(ctx.settings.oracle_sandbox_url).execute(
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
            code_set_bindings=code_set_bindings,
        )

        events = [("AUDIT_STARTED", "oracle_executor", {"attempt": attempt})]
        if report.get("uncovered_rules"):
            events.append(
                (
                    "RULE_COVERAGE_GAP",
                    "oracle_executor",
                    {
                        "coverage_rate": report.get("coverage_rate"),
                        "uncovered_rules": report["uncovered_rules"],
                        "note": "这些规则没有任何变异用例命中过，等于没被验证；审计通过不代表它们对",
                    },
                )
            )

        blobs = LocalBlobStore(ctx.settings.blob_dir)
        ddl_uri = blobs.put(
            "\n\n".join(compile_ai_schema_mysql_ddl(spec.table_code, typed_schemas[spec.table_code]) for spec in specs).encode()
        )
        physical_tables = report["physical_tables"]
        oracle_ddl_parts = [
            compile_code_set_oracle_ddl(report["code_set_tables"][cs["code"]]) for cs in code_sets
        ] + [
            compile_ai_schema_oracle_ddl(report["target_tables"][spec.table_code], typed_schemas[spec.table_code])
            for spec in specs
        ]
        oracle_ddl_uri = blobs.put("\n\n".join(oracle_ddl_parts).encode())
        queries = []
        for rule in rules:
            sql, params = compile_rule_query(rule, "oracle", physical_tables, code_set_bindings)
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
            "audit_summary": {k: v for k, v in report.items() if k not in {"cases", "physical_tables", "target_tables"}},
        }
        if ctx.budget_guard is not None and hasattr(ctx.budget_guard, "snapshot"):
            result["budget_usage"] = ctx.budget_guard.snapshot()
        for artifact_type, uri in [
            ("MYSQL_DDL", ddl_uri),
            ("ORACLE_DDL", oracle_ddl_uri),
            ("VALIDATION_SQL", sql_uri),
            ("AUDIT_REPORT", report_uri),
        ]:
            ctx.session.add(
                ArtifactModel(
                    id=str(uuid.uuid4()),
                    run_id=run.id,
                    artifact_type=artifact_type,
                    blob_uri=uri,
                    sha256=uri.removeprefix("blob://"),
                    version=attempt,
                )
            )
        ctx.session.flush()

        shared = {"report": report, "result": result, "attempt": attempt, "first_attempt": first_attempt}
        cases = report.get("cases", [])
        golden_fp_cases = [
            case for case in cases if case.get("case_kind") == "GOLDEN" and case.get("unexpected_rules")
        ]
        missing_rule_keys = sorted(
            {rule_key for case in cases for rule_key in case.get("missing_rules", [])}
        )
        if not report["all_passed"] and attempt < first_attempt + max_attempts - 1:
            if golden_fp_cases and not missing_rule_keys:
                return Outcome(
                    shared={
                        **shared,
                        "heal_data_report": report,
                        "prepared_assets": None,
                    },
                    events=events,
                    goto=self_heal_data_target(),
                    when="audit_failed_heal_data",
                    control={
                        "failed_result": result,
                        "reason": "golden false positive -> heal test data without weakening rules",
                        "origin": "self_heal_data",
                        "attempt": attempt,
                    },
                )
            if missing_rule_keys:
                rule_map = {rule.rule_key: rule for rule in (rules or [])}
                all_mechanical = all(_is_mechanically_derived_rule(k, rule_map) for k in missing_rule_keys)
                if all_mechanical:
                    return Outcome(
                        shared={
                            **shared,
                            "heal_cases_report": report,
                            "prepared_assets": None,
                        },
                        events=events,
                        goto=self_heal_cases_target(),
                        when="audit_failed_heal_cases",
                        control={
                            "failed_result": result,
                            "reason": "deterministic rule missed -> regenerate targeted audit mutations",
                            "origin": "self_heal_cases",
                            "attempt": attempt,
                        },
                    )
                return Outcome(
                    shared={
                        **shared,
                        "self_heal_report": report,
                        "approval_origin": "self_heal",
                        "prepared_assets": None,
                    },
                    events=events,
                    goto=self_heal_target(),
                    when="audit_failed_self_heal",
                    control={
                        "failed_result": result,
                        "reason": "scheduler self-heal",
                        "origin": "self_heal",
                        "attempt": attempt,
                    },
                )

        events.append(
            (
                "AUDIT_COMPLETED" if report["all_passed"] else "AUDIT_REJECTED",
                "oracle_executor",
                {"result": result, "node": "oracle_executor"},
            )
        )
        if report["all_passed"]:
            return Outcome(
                shared=shared,
                events=events,
                when="audit_passed",
                interrupt=Interrupt(
                    kind="TEST_CONFIRMATION_REQUIRED",
                    node="test_confirmation",
                    question="Oracle 沙箱验证已全部通过，请确认测试结果后导出生产包。",
                    pending_input={
                        "audit_report_blob": report_uri,
                        "active_rule_hashes": result["active_rule_hashes"],
                        "attempt": attempt,
                    },
                    resume_node="test_confirmation",
                    identity=ctx.identity.as_payload(),
                ),
            )
        return Outcome(shared=shared, events=events, when="audit_rejected")

    return {
        "data_generator_binder": body_data_generator_binder,
        "data_table_worker": body_data_table_worker,
        "data_join": body_data_join,
        "audit_case_binder": body_audit_case_binder,
        "audit_table_worker": body_audit_table_worker,
        "audit_join": body_audit_join,
        "oracle_executor": body_oracle_executor,
    }
