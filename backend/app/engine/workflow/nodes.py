"""ProdAgent 节点体装配（Workflow Nodes）。

每个节点严格遵守 `body(input, ctx: RunContext) -> Outcome` 契约：
- 只负责本阶段逻辑，绝不直接调用下游节点；
- 所有跨节点产物写入 `Outcome.shared`（经 Channel Reducer 合并），所有事实事件写入 `Outcome.events`；
- 遇到人工确权/测试确认通过中断事件挂起；遇到 Oracle 审计缺失命中通过 `Outcome.goto` 交回 Scheduler 调度自愈。
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
from app.infra.domain import RuleDefinition, canonical_json
from app.engine.oracle import GenericOracleExecutor, compile_ai_schema_mysql_ddl, compile_ai_schema_oracle_ddl
from app.engine.workflow.runtime import _latest_payload, _pending_rules
from app.engine.workflow.stages import (
    _audit_stage,
    _backfill_fixed_value_rules,
    _backfill_length_rules,
    _backfill_required_rules,
    _data_stage,
    _ensure_oracle_assets,
    _plan_stage,
    _rebind_failed_rules,
    _rules_stage,
    _schema_stage,
)
from app.infra.asset_models import ArtifactModel
from app.infra.errors import WorkflowError
from app.infra.models import RuleModel
from app.kernel.blob_store import LocalBlobStore
from app.kernel.channel import RunContext
from app.kernel.scheduler import Outcome
from app.policy.regops_plan import (
    interrupt_payload,
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
    """前半段节点 body：只做本节点的事，事件与产物统一放入 Outcome 交由 Scheduler 屏障落账。"""

    def body_generation_planner(_input, ctx: RunContext) -> Outcome:
        events: List[tuple] = []
        # 无预算时不走规划阶段（保持原有语义：生成计划是可选阶段）
        plans = (
            _plan_stage(
                ctx.session, ctx.run, specs, ctx.llm, ctx.budget, ctx.concurrency,
                bus=ctx.bus, events=events,
            )
            if ctx.get("use_plan")
            else None
        )
        return Outcome(output=plans, shared={"plans": plans or {}}, events=events)

    def body_schema_mapper(_input, ctx: RunContext) -> Outcome:
        events: List[tuple] = []
        blobs = LocalBlobStore(ctx.settings.blob_dir)
        schema_payload = _schema_stage(
            ctx.session, ctx.run, specs, ctx.llm, ctx.concurrency,
            bus=ctx.bus, events=events, blob_store=blobs,
        )
        return Outcome(shared={"schema_payload": schema_payload}, events=events)

    def body_rule_binder(_input, ctx: RunContext) -> Outcome:
        events: List[tuple] = []
        self_heal_report = ctx.shared.pop("self_heal_report", None)
        if self_heal_report is not None:
            # 自愈或人工重试回边：只靶向重绑缺失命中的失败规则，绝不全表重新提取
            origin = ctx.get("approval_origin") or "self_heal"
            revised = _rebind_failed_rules(
                ctx.session, ctx.run, specs, ctx.llm, self_heal_report,
                bus=ctx.bus, events=events,
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
            )

        plans = ctx.get("plans")
        pending_count, golden_overrides = _rules_stage(
            ctx.session, ctx.run, specs, ctx.llm, plans,
            concurrency=ctx.concurrency, bus=ctx.bus, events=events,
        )
        # pending 只写入 Channel；人工门事件在 asset builder 之后才落，
        # 保证即使有待确认规则，Oracle 资产也先建好（与既有语义一致）
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
            .filter(RuleModel.run_id == ctx.run.id, RuleModel.status != "SUPERSEDED").all()
        ]
        assets = _ensure_oracle_assets(
            ctx.session, ctx.run, specs, schema_payload, generated_rules, ctx.settings,
            events=events, attempt=ctx.state.attempt + 1,
        )
        pending_count = len(_pending_rules(ctx.session, ctx.run.id))
        origin = ctx.get("approval_origin") or "rule_binder"
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
            events.append((
                "APPROVAL_REQUIRED",
                "human_gate",
                interrupt_payload("APPROVAL_REQUIRED", question, pending_meta, ctx.identity.as_payload()),
            ))
        when = "pending_rules" if pending_count else "no_pending_rules"
        return Outcome(
            shared={"rules": generated_rules, "prepared_assets": assets, "pending_count": pending_count},
            events=events,
            when=when,
        )

    return {
        "generation_planner": body_generation_planner,
        "schema_mapper": body_schema_mapper,
        "rule_binder": body_rule_binder,
        "oracle_asset_builder": body_oracle_asset_builder,
    }


def _back_bodies(specs, settings):
    """后半段节点 body：产物与事件全部通过 Outcome 返回，节点之间不直接调用下游。"""

    def body_data_generator_binder(_input, ctx: RunContext) -> Outcome:
        events: List[tuple] = []
        blobs = LocalBlobStore(ctx.settings.blob_dir)
        schema_payload = ctx.get("schema_payload")
        golden_overrides = ctx.get("golden_overrides") or {}
        plans = ctx.get("plans") or {}
        heal_data_report = ctx.shared.pop("heal_data_report", None)
        rows = ctx.session.query(RuleModel).filter(RuleModel.run_id == ctx.run.id, RuleModel.status == "APPROVED").all()
        rules = [RuleDefinition.model_validate(row.definition_json) for row in rows]
        rules = _backfill_required_rules(ctx.session, ctx.run, specs, rules, events=events)
        rules = _backfill_length_rules(ctx.session, ctx.run, specs, rules, events=events)
        rules = _backfill_fixed_value_rules(ctx.session, ctx.run, specs, rules, events=events)
        prepared_assets = _ensure_oracle_assets(
            ctx.session, ctx.run, specs, schema_payload, rules, ctx.settings,
            events=events, attempt=ctx.state.attempt + 1,
        )
        golden_records, generator_payload = _data_stage(
            ctx.session, ctx.run, specs, ctx.llm, rules,
            golden_overrides, plans, concurrency=ctx.concurrency,
            bus=ctx.bus, events=events, blob_store=blobs,
            heal_report=heal_data_report,
        )
        return Outcome(
            shared={
                "rules": rules,
                "prepared_assets": prepared_assets,
                "golden_records": golden_records,
                "generator_payload": generator_payload,
            },
            events=events,
        )

    def body_audit_case_binder(_input, ctx: RunContext) -> Outcome:
        events: List[tuple] = []
        blobs = LocalBlobStore(ctx.settings.blob_dir)
        plans = ctx.get("plans") or {}
        heal_cases_report = ctx.shared.pop("heal_cases_report", None)
        audit_cases = _audit_stage(
            ctx.session, ctx.run, specs, ctx.llm, ctx.get("rules"),
            ctx.get("golden_records"), plans, concurrency=ctx.concurrency,
            bus=ctx.bus, events=events, blob_store=blobs,
            heal_report=heal_cases_report,
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
            run.id, attempt, specs, typed_schemas, golden_records, audit_cases, rules,
            golden_counts, golden_generators,
            code_sets=code_sets, prepared_assets=prepared_assets, code_set_bindings=code_set_bindings,
        )

        events = [("AUDIT_STARTED", "oracle_executor", {"attempt": attempt})]
        if report.get("uncovered_rules"):
            events.append(("RULE_COVERAGE_GAP", "oracle_executor", {
                "coverage_rate": report.get("coverage_rate"),
                "uncovered_rules": report["uncovered_rules"],
                "note": "这些规则没有任何变异用例命中过，等于没被验证；审计通过不代表它们对",
            }))

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
        for artifact_type, uri in [
            ("MYSQL_DDL", ddl_uri), ("ORACLE_DDL", oracle_ddl_uri),
            ("VALIDATION_SQL", sql_uri), ("AUDIT_REPORT", report_uri),
        ]:
            ctx.session.add(ArtifactModel(
                id=str(uuid.uuid4()), run_id=run.id, artifact_type=artifact_type,
                blob_uri=uri, sha256=uri.removeprefix("blob://"), version=attempt,
            ))
        ctx.session.flush()

        shared = {"report": report, "result": result, "attempt": attempt, "first_attempt": first_attempt}
        cases = report.get("cases", [])
        golden_fp_cases = [
            case for case in cases
            if case.get("case_kind") == "GOLDEN" and case.get("unexpected_rules")
        ]
        missing_rule_keys = sorted(
            {rule_key for case in cases for rule_key in case.get("missing_rules", [])}
        )
        if not report["all_passed"] and attempt < first_attempt + max_attempts - 1:
            # 确定性归因分流（Root-Cause Router）：
            # 1) 黄金样本触发误报：优先回 data_generator_binder 修正测试数据，绝不削弱规则；
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
            # 2) 变异样本漏报：若全部漏报规则均属机械派生确定性规则（规则本身无误），先回 audit_case_binder 重建反例；
            #    否则回 rule_binder 执行靶向 _rebind_failed_rules。
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

        events.append((
            "AUDIT_COMPLETED" if report["all_passed"] else "AUDIT_REJECTED",
            "oracle_executor", {"result": result, "node": "oracle_executor"},
        ))
        if report["all_passed"]:
            events.append(("TEST_CONFIRMATION_REQUIRED", "test_confirmation", interrupt_payload(
                "TEST_CONFIRMATION_REQUIRED",
                "Oracle 沙箱验证已全部通过，请确认测试结果后导出生产包。",
                {
                    "audit_report_blob": report_uri,
                    "active_rule_hashes": result["active_rule_hashes"],
                    "attempt": attempt,
                },
                ctx.identity.as_payload(),
            )))
            return Outcome(shared=shared, events=events, when="audit_passed")
        return Outcome(shared=shared, events=events, when="audit_rejected")

    return {
        "data_generator_binder": body_data_generator_binder,
        "audit_case_binder": body_audit_case_binder,
        "oracle_executor": body_oracle_executor,
    }
