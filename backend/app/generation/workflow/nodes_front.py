"""Step 2 前半段节点体装配（Layer 5: generation/workflow/nodes_front）。

对应前端 `02 /generation`（生成表结构、码表和规则）：
- `generation_planner`（确定性规划）→ `schema_mapper`（确定性 DDL 映射）
- `rule_binder` → 真 BSP `Send` `rule_table_worker` → `rule_join`（单表/跨表规则绑定）
- `oracle_asset_builder`（Oracle 建表+灌码值+规则空跑预检）→ `human_gate`（人工确权中断门）
"""

from typing import Any, Dict, List

from app.catalog.code_sets.bindings import load_published_code_sets
from app.generation.core.rule_backfill import derive_deterministic_rules as _policy_derive_deterministic_rules
from app.generation.workflow.runtime import _emit, _pending_rules, _upsert_rule
from app.generation.workflow.stage_plan_schema import _plan_stage, _schema_stage
from app.generation.workflow.stage_rules import (
    _ensure_oracle_assets,
    _rebind_failed_rules,
    bind_table_rules_worker,
    join_table_rules_barrier,
    prepare_deterministic_rule_index,
)
from app.infra.blob_store import LocalBlobStore
from app.infra.contracts import RuleDefinition
from app.infra.db_tables import RuleModel
from app.infra.errors import WorkflowError
from app.kernel.interrupt import Interrupt
from app.kernel.run import RunContext
from app.kernel.scheduler import Outcome, Send


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
