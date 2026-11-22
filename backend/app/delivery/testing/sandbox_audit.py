"""Step 3.2：Oracle 沙箱 AB 正反例真跑与审计报告执行器（Layer 6: delivery/testing）。

继承 Layer 5 `OracleAssetPreparer`（空跑建表与码值预建），专注于第三阶段测试执行：
- 灌入黄金合规正例 A（支持 `golden_count > 1` 多条黄金同步入宽沙箱表真跑校验）；
- 灌入定向变异反例 B（含 `UNIQUE_KEY` 隔离分配独立主键防污染黄金基线）；
- 执行全量校验 SQL，统计误报（FP）、漏报（FN）、共触发（co-triggered）与规则覆盖率。
"""

import copy
from typing import Any, Dict, List

from sqlalchemy import create_engine, text

from app.delivery.testing.mutations import make_distinct_key_value
from app.generation.core.binding import materialize_golden_record
from app.generation.sql.code_set_sql import sandbox_code_set_table
from app.generation.sql.compiler import compile_rule_query
from app.generation.sql.oracle import (
    OracleAssetPreparer,
    _adapt_sandbox_ddl,
    _drop_sandbox_table_if_exists,
    compile_ai_schema_oracle_ddl,
    compile_sandbox_oracle_ddl,
    fit_record_values,
    fit_sandbox_values,
    physical_table_name,
)
from app.infra.contracts import AuditCaseBinding, GeneratorBinding, RuleDefinition, SchemaBinding, SpecDocument
from app.infra.db import quote_identifier


class GenericOracleExecutor(OracleAssetPreparer):
    """Oracle AB 沙箱真跑执行器：复用 Step 2 预建资产并执行全量黄金/变异审计。"""

    def execute(
        self,
        run_id: str,
        attempt: int,
        specs: List[SpecDocument],
        schemas: Dict[str, List[SchemaBinding]],
        golden_records: Dict[str, Dict[str, Any]],
        audit_cases: Dict[str, List[AuditCaseBinding]],
        rules: List[RuleDefinition],
        golden_counts: Dict[str, int] = None,
        golden_generators: Dict[str, List[GeneratorBinding]] = None,
        code_sets: List[Dict[str, Any]] = None,
        prepared_assets: Dict[str, Any] = None,
        code_set_bindings: Dict[Any, Any] = None,
    ) -> Dict[str, Any]:
        physical_tables = {
            spec.table_code: physical_table_name(run_id, attempt, spec.table_code) for spec in specs
        }
        target_tables = {code: f"{table}_TARGET" for code, table in physical_tables.items()}
        selected_code_sets = code_sets or []
        code_set_tables = {
            item["code"]: sandbox_code_set_table(run_id, attempt, item["code"]) for item in selected_code_sets
        }
        prepared_code_set_codes = set()
        prepared_spec_codes = set()
        if prepared_assets:
            prep_phys = prepared_assets.get("physical_tables") or {}
            prep_tgt = prepared_assets.get("target_tables") or {}
            prep_cs = prepared_assets.get("code_set_tables") or {}
            for code, tbl in prep_phys.items():
                physical_tables[code] = tbl
                if code in prep_tgt:
                    target_tables[code] = prep_tgt[code]
                    prepared_spec_codes.add(code)
            for cs_code, tbl in prep_cs.items():
                code_set_tables[cs_code] = tbl
                prepared_code_set_codes.add(cs_code)

        # 当 golden_count > 1 时，将第 2..N 条黄金样本也纳入沙箱真实校验（不仅写入 _TARGET，防止多条黄金间 UNIQUE_KEY/交叉失配漏检）
        effective_cases: Dict[str, List[AuditCaseBinding]] = {}
        records: Dict[str, List[Dict[str, Any]]] = {}

        for spec in specs:
            table_cases = list(audit_cases[spec.table_code])
            existing_ids = {c.case_id for c in table_cases}
            existing_golden = sum(1 for c in table_cases if "GOLDEN" in c.case_id.upper())
            golden_total = max(1, int((golden_counts or {}).get(spec.table_code, 1)))
            extra_golden_records: Dict[str, Dict[str, Any]] = {}
            if golden_total > existing_golden and golden_generators and spec.table_code in golden_generators:
                for g_idx in range(1, golden_total):
                    extra_id = f"GOLDEN_BASELINE_{g_idx + 1}"
                    if extra_id in existing_ids:
                        continue
                    existing_ids.add(extra_id)
                    gen_rec = materialize_golden_record(spec, golden_generators[spec.table_code], g_idx)
                    extra_golden_records[extra_id] = {**golden_records[spec.table_code], **gen_rec}
                    table_cases.append(AuditCaseBinding(case_id=extra_id, overrides={}, expected_rule_keys=[]))
            effective_cases[spec.table_code] = table_cases

            records[spec.table_code] = []
            spec_fields = {f.name: f for f in spec.fields}
            unique_rules = [
                r for r in rules if r.table_code == spec.table_code and r.operator == "UNIQUE_KEY"
            ]
            gen_by_field = (
                {g.field: g for g in golden_generators[spec.table_code]}
                if golden_generators and spec.table_code in golden_generators
                else {}
            )
            for row_idx, case in enumerate(table_cases):
                is_golden = "GOLDEN" in case.case_id.upper()
                if case.case_id in extra_golden_records:
                    record = copy.deepcopy(extra_golden_records[case.case_id])
                else:
                    record = copy.deepcopy(golden_records[spec.table_code])
                # 表含 UNIQUE_KEY 规则时：非黄金且未显式覆盖唯一键的普通变异行自动分配独立主键，
                # 避免与其他单字段变异行继承同一黄金主键而污染 GOLDEN_BASELINE（DUP 变异行显式覆写不受影响）。
                if unique_rules and not is_golden:
                    for u_rule in unique_rules:
                        key_fields = list(dict.fromkeys((u_rule.params or {}).get("key_fields") or [u_rule.field]))
                        if any(kf in case.overrides for kf in key_fields):
                            continue
                        primary_kf = key_fields[0]
                        gen = gen_by_field.get(primary_kf)
                        if gen is not None and gen.generator == "SEQUENCE":
                            record[primary_kf] = materialize_golden_record(spec, [gen], row_idx + golden_total)[primary_kf]
                        else:
                            record[primary_kf] = make_distinct_key_value(
                                record.get(primary_kf), spec_fields.get(primary_kf), row_idx + golden_total
                            )
                record.update(case.overrides)
                record["record_id"] = case.case_id
                records[spec.table_code].append(fit_sandbox_values(spec, record))
        hits = {
            spec.table_code: {case.case_id: [] for case in effective_cases[spec.table_code]} for spec in specs
        }
        engine = create_engine(self.database_url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                for code_set in selected_code_sets:
                    if code_set["code"] in prepared_code_set_codes:
                        continue
                    self._populate_code_set_table(connection, code_set_tables[code_set["code"]], code_set)
                for spec in specs:
                    table = physical_tables[spec.table_code]
                    target_table = target_tables[spec.table_code]
                    names = ["record_id"] + [field.name for field in spec.fields]
                    parameters = [f"value_{index}" for index in range(len(names))]
                    if spec.table_code in prepared_spec_codes:
                        # 幂等清理：若上次同 attempt 执行因瞬时网络抖动中断重试，先清旧行防 ORA-00001 主键冲突
                        connection.execute(text(f"DELETE FROM {quote_identifier(target_table, 'oracle')}"))
                    else:
                        _drop_sandbox_table_if_exists(connection, target_table)
                        connection.execute(
                            text(_adapt_sandbox_ddl(compile_ai_schema_oracle_ddl(target_table, schemas[spec.table_code]), connection))
                        )
                    target_insert = text(
                        f"INSERT INTO {quote_identifier(target_table, 'oracle')} "
                        f"({', '.join(quote_identifier(name, 'oracle') for name in names)}) "
                        f"VALUES ({', '.join(':' + name for name in parameters)})"
                    )
                    golden_total = (golden_counts or {}).get(spec.table_code, 1)
                    golden_rows = []
                    for index in range(golden_total):
                        generated = (
                            materialize_golden_record(spec, golden_generators[spec.table_code], index)
                            if golden_generators and spec.table_code in golden_generators
                            else golden_records[spec.table_code]
                        )
                        golden = fit_record_values(
                            spec,
                            {
                                item.field: item.length or (38 if item.storage_type == "NUMBER" else 64)
                                for item in schemas[spec.table_code]
                            },
                            {
                                **golden_records[spec.table_code],
                                **generated,
                                "record_id": f"GOLDEN_{index + 1}",
                            },
                        )
                        golden_rows.append(dict(zip(parameters, [golden.get(name) for name in names])))
                    if golden_rows:
                        connection.execute(target_insert, golden_rows)
                    if spec.table_code in prepared_spec_codes:
                        connection.execute(text(f"DELETE FROM {quote_identifier(table, 'oracle')}"))
                    else:
                        _drop_sandbox_table_if_exists(connection, table)
                        connection.execute(text(_adapt_sandbox_ddl(compile_sandbox_oracle_ddl(table, spec), connection)))
                    statement = text(
                        f"INSERT INTO {quote_identifier(table, 'oracle')} "
                        f"({', '.join(quote_identifier(name, 'oracle') for name in names)}) "
                        f"VALUES ({', '.join(':' + name for name in parameters)})"
                    )
                    audit_rows = [
                        dict(zip(parameters, [record.get(name) for name in names]))
                        for record in records[spec.table_code]
                    ]
                    if audit_rows:
                        connection.execute(statement, audit_rows)
                for rule in rules:
                    sql, params = compile_rule_query(rule, "oracle", physical_tables, code_set_bindings)
                    for row in connection.execute(text(sql), params).fetchall():
                        hits[rule.table_code][row[1]].append(row[0])
        finally:
            engine.dispose()
        results = []
        false_positives = 0
        false_negatives = 0
        co_triggered_total = 0
        hit_rules = set()
        for spec in specs:
            for case in effective_cases[spec.table_code]:
                is_golden = "GOLDEN" in case.case_id.upper()
                # 黄金用例是「零命中」基线：AI 写的期望一律无效，命中任何规则都是误报
                expected = set() if is_golden else set(case.expected_rule_keys)
                actual = set(hits[spec.table_code][case.case_id])
                hit_rules.update(actual)
                missing = sorted(expected - actual)
                extra = sorted(actual - expected)
                # 非黄金用例是「定向变异」：同一个变异值同时踩中多条规则（例如置空同时违反
                # 必填和枚举）是必然的，不算缺陷。AI 手写的期望全集从来写不全，用它判失败
                # 只会把好规则判成坏规则。
                unexpected = extra if is_golden else []
                co_triggered = [] if is_golden else extra
                false_negatives += len(missing)
                false_positives += len(unexpected)
                co_triggered_total += len(co_triggered)
                results.append(
                    {
                        "table_code": spec.table_code,
                        "case_id": case.case_id,
                        "case_kind": "GOLDEN" if is_golden else "MUTATION",
                        "expected_rules": sorted(expected),
                        "actual_rules": sorted(actual),
                        "missing_rules": missing,
                        "unexpected_rules": unexpected,
                        "co_triggered_rules": co_triggered,
                        "passed": not missing and not unexpected,
                    }
                )
        all_rule_keys = {rule.rule_key for rule in rules}
        uncovered = sorted(all_rule_keys - hit_rules)
        coverage_rate = (
            round((len(all_rule_keys) - len(uncovered)) / len(all_rule_keys), 4) if all_rule_keys else 1.0
        )
        return {
            "all_passed": false_positives == 0 and false_negatives == 0,
            "coverage_complete": not uncovered,
            "coverage_rate": coverage_rate,
            "uncovered_rules": uncovered,
            "co_triggered_count": co_triggered_total,
            "backend": "ORACLE",
            "table_count": len(specs),
            "case_count": len(results),
            "active_rule_count": len(rules),
            "false_positive_count": false_positives,
            "false_negative_count": false_negatives,
            "record_counts": {code: len(items) for code, items in records.items()},
            "physical_tables": physical_tables,
            "target_tables": target_tables,
            "code_set_tables": code_set_tables,
            "code_set_versions": {item["code"]: item["version"] for item in selected_code_sets},
            "code_set_record_counts": {item["code"]: len(item["values"]) for item in selected_code_sets},
            "cases": results,
            "coverage_statement": "结果仅对本次透明展示的 AI 审计参数负责，不代表监管语义完备。",
        }
