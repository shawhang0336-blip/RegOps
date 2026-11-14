import copy
from typing import Any, Dict, List

from sqlalchemy import create_engine, text

from .code_set_oracle import compile_code_set_oracle_ddl, sandbox_code_set_table
from .compiler import compile_rule_query, quote_identifier
from .domain import RuleDefinition, SpecDocument
from .generic_domain import AuditCaseBinding, GeneratorBinding, SchemaBinding, materialize_golden_record
from .oracle_sandbox import physical_table_name


def compile_ai_schema_oracle_ddl(
    table: str,
    schema: List[SchemaBinding],
) -> str:
    columns = ['"record_id" VARCHAR2(64 CHAR) PRIMARY KEY']
    for field in schema:
        if field.storage_type == "NUMBER":
            data_type = f"NUMBER({field.length},{field.decimal_places})"
        else:
            data_type = f"VARCHAR2({field.length} CHAR)"
        columns.append(f"{quote_identifier(field.field, 'oracle')} {data_type} NULL")
    return f"CREATE TABLE {quote_identifier(table, 'oracle')} (\n  " + ",\n  ".join(columns) + "\n)"


def compile_sandbox_oracle_ddl(table: str, spec: SpecDocument) -> str:
    columns = ['"record_id" VARCHAR2(64 CHAR) PRIMARY KEY']
    columns.extend(f"{quote_identifier(field.name, 'oracle')} VARCHAR2(4000 CHAR) NULL" for field in spec.fields)
    return f"CREATE TABLE {quote_identifier(table, 'oracle')} (\n  " + ",\n  ".join(columns) + "\n)"


def compile_ai_schema_mysql_ddl(table: str, schema: List[SchemaBinding]) -> str:
    columns = ["`record_id` VARCHAR(64) NOT NULL PRIMARY KEY"]
    for field in schema:
        data_type = (
            f"DECIMAL({field.length},{field.decimal_places})"
            if field.storage_type == "NUMBER"
            else f"VARCHAR({field.length})"
        )
        columns.append(f"{quote_identifier(field.field)} {data_type} NULL")
    return f"CREATE TABLE IF NOT EXISTS {quote_identifier(table)} (\n  " + ",\n  ".join(columns) + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"


class GenericOracleExecutor:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def prepare_assets(
        self,
        run_id: str,
        attempt: int,
        specs: List[SpecDocument],
        schemas: Dict[str, List[SchemaBinding]],
        code_sets: List[Dict[str, Any]] = None,
        rules: List[RuleDefinition] = None,
    ) -> Dict[str, Any]:
        physical_tables = {
            spec.table_code: physical_table_name(run_id, attempt, spec.table_code) for spec in specs
        }
        target_tables = {code: f"{table}_TARGET" for code, table in physical_tables.items()}
        selected_code_sets = code_sets or []
        code_set_tables = {
            item["code"]: sandbox_code_set_table(run_id, attempt, item["code"]) for item in selected_code_sets
        }
        engine = create_engine(self.database_url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                for code_set in selected_code_sets:
                    table = code_set_tables[code_set["code"]]
                    connection.execute(text(compile_code_set_oracle_ddl(table)))
                    statement = text(
                        f"INSERT INTO {quote_identifier(table, 'oracle')} "
                        '("CODE_VALUE", "LABEL_CN", "LABEL_EN", "DEFINITION", "SORT_ORDER", "ENABLED") '
                        "VALUES (:code_value, :label_cn, :label_en, :definition, :sort_order, :enabled)"
                    )
                    for value in code_set["values"]:
                        connection.execute(
                            statement,
                            {
                                "code_value": value["value"],
                                "label_cn": value["label_cn"],
                                "label_en": value.get("label_en") or "",
                                "definition": value.get("definition") or "",
                                "sort_order": value.get("sort_order", 0),
                                "enabled": 1 if value.get("enabled", True) else 0,
                            },
                        )
                for spec in specs:
                    connection.execute(text(compile_ai_schema_oracle_ddl(target_tables[spec.table_code], schemas[spec.table_code])))
                    connection.execute(text(compile_sandbox_oracle_ddl(physical_tables[spec.table_code], spec)))
                for rule in rules or []:
                    sql, params = compile_rule_query(rule, "oracle", physical_tables)
                    connection.execute(text(sql), params).fetchall()
        finally:
            engine.dispose()
        return {
            "attempt": attempt,
            "physical_tables": physical_tables,
            "target_tables": target_tables,
            "code_set_tables": code_set_tables,
            "code_set_versions": {item["code"]: item["version"] for item in selected_code_sets},
            "code_set_record_counts": {item["code"]: len(item["values"]) for item in selected_code_sets},
            "empty_run": {"all_passed": True, "rule_count": len(rules or []), "statement_count": len(rules or [])},
        }

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
    ) -> Dict[str, Any]:
        physical_tables = {
            spec.table_code: physical_table_name(run_id, attempt, spec.table_code) for spec in specs
        }
        target_tables = {code: f"{table}_TARGET" for code, table in physical_tables.items()}
        selected_code_sets = code_sets or []
        code_set_tables = {
            item["code"]: sandbox_code_set_table(run_id, attempt, item["code"]) for item in selected_code_sets
        }
        if prepared_assets:
            physical_tables = prepared_assets["physical_tables"]
            target_tables = prepared_assets["target_tables"]
            code_set_tables = prepared_assets["code_set_tables"]
        records: Dict[str, List[Dict[str, Any]]] = {}
        for spec in specs:
            records[spec.table_code] = []
            for case in audit_cases[spec.table_code]:
                record = copy.deepcopy(golden_records[spec.table_code])
                record.update(case.overrides)
                record["record_id"] = case.case_id
                records[spec.table_code].append(record)
        hits = {
            spec.table_code: {case.case_id: [] for case in audit_cases[spec.table_code]} for spec in specs
        }
        engine = create_engine(self.database_url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                for code_set in ([] if prepared_assets else selected_code_sets):
                    table = code_set_tables[code_set["code"]]
                    connection.execute(text(compile_code_set_oracle_ddl(table)))
                    statement = text(
                        f"INSERT INTO {quote_identifier(table, 'oracle')} "
                        '("CODE_VALUE", "LABEL_CN", "LABEL_EN", "DEFINITION", "SORT_ORDER", "ENABLED") '
                        "VALUES (:code_value, :label_cn, :label_en, :definition, :sort_order, :enabled)"
                    )
                    for value in code_set["values"]:
                        connection.execute(
                            statement,
                            {
                                "code_value": value["value"],
                                "label_cn": value["label_cn"],
                                "label_en": value.get("label_en") or "",
                                "definition": value.get("definition") or "",
                                "sort_order": value.get("sort_order", 0),
                                "enabled": 1 if value.get("enabled", True) else 0,
                            },
                        )
                for spec in specs:
                    table = physical_tables[spec.table_code]
                    names = ["record_id"] + [field.name for field in spec.fields]
                    parameters = [f"value_{index}" for index in range(len(names))]
                    if not prepared_assets:
                        connection.execute(text(compile_ai_schema_oracle_ddl(target_tables[spec.table_code], schemas[spec.table_code])))
                    target_insert = text(
                        f"INSERT INTO {quote_identifier(target_tables[spec.table_code], 'oracle')} "
                        f"({', '.join(quote_identifier(name, 'oracle') for name in names)}) "
                        f"VALUES ({', '.join(':' + name for name in parameters)})"
                    )
                    golden_total = (golden_counts or {}).get(spec.table_code, 1)
                    for index in range(golden_total):
                        generated = (
                            materialize_golden_record(spec, golden_generators[spec.table_code], index)
                            if golden_generators and spec.table_code in golden_generators
                            else golden_records[spec.table_code]
                        )
                        golden = {
                            **golden_records[spec.table_code],
                            **generated,
                            "record_id": f"GOLDEN_{index + 1}",
                        }
                        connection.execute(target_insert, dict(zip(parameters, [golden.get(name) for name in names])))
                    if not prepared_assets:
                        connection.execute(text(compile_sandbox_oracle_ddl(table, spec)))
                    statement = text(
                        f"INSERT INTO {quote_identifier(table, 'oracle')} "
                        f"({', '.join(quote_identifier(name, 'oracle') for name in names)}) "
                        f"VALUES ({', '.join(':' + name for name in parameters)})"
                    )
                    for record in records[spec.table_code]:
                        connection.execute(statement, dict(zip(parameters, [record.get(name) for name in names])))
                for rule in rules:
                    sql, params = compile_rule_query(rule, "oracle", physical_tables)
                    for row in connection.execute(text(sql), params).fetchall():
                        hits[rule.table_code][row[1]].append(row[0])
        finally:
            engine.dispose()
        results = []
        false_positives = 0
        false_negatives = 0
        for spec in specs:
            for case in audit_cases[spec.table_code]:
                expected = set(case.expected_rule_keys)
                actual = set(hits[spec.table_code][case.case_id])
                missing = sorted(expected - actual)
                unexpected = sorted(actual - expected)
                false_negatives += len(missing)
                false_positives += len(unexpected)
                results.append(
                    {
                        "table_code": spec.table_code,
                        "case_id": case.case_id,
                        "expected_rules": sorted(expected),
                        "actual_rules": sorted(actual),
                        "missing_rules": missing,
                        "unexpected_rules": unexpected,
                        "passed": not missing and not unexpected,
                    }
                )
        return {
            "all_passed": false_positives == 0 and false_negatives == 0,
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
