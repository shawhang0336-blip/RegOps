import copy
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, text

from .code_set_oracle import compile_code_set_oracle_ddl, sandbox_code_set_table
from .compiler import compile_rule_query, quote_identifier
from .domain import RuleDefinition, SpecDocument
from .generic_domain import AuditCaseBinding, GeneratorBinding, SchemaBinding, materialize_golden_record
from .oracle_sandbox import physical_table_name


def _fit_text(value: Any, length: int) -> Any:
    text_value = str(value)
    if length and len(text_value) > length:
        return text_value[:length]
    return text_value


def _fit_number(value: Any, length: int, decimals: int) -> Any:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return value
    scale = max(int(decimals or 0), 0)
    quantum = Decimal(1).scaleb(-scale)
    number = number.quantize(quantum, rounding=ROUND_DOWN)
    integer_digits = max(int(length or 38) - scale, 1)
    limit = Decimal(10) ** integer_digits - quantum
    if number > limit:
        number = limit
    if number < -limit:
        number = -limit
    return number


def fit_record_values(
    spec: SpecDocument,
    schema_lengths: Optional[Dict[str, int]],
    record: Dict[str, Any],
) -> Dict[str, Any]:
    """把记录值裁到字段长度/精度以内。

    阶段 1 给了字段长度，阶段 2 让 AI 设计变异用例时不一定遵守：实测出现过
    REMARK 给 257 字符、字段是 VARCHAR(256)，插库直接 ORA-12899，整次审计失败。
    入库前按元数据裁一刀，宁可截断也不让整批挂掉。

    注意：这个函数只用于「进生产同构表（严格列宽）」的黄金记录。
    审计用例的变异值进的是 VARCHAR2(4000) 的沙箱宽表，绝不能裁——
    一旦裁到声明长度，LENGTH / LENGTH_MAX 规则就永远没有超长值可测，
    整类长度规则会集体变成永不触发的死规则（实测 8/14 个失败就是这么来的）。
    """
    fitted = dict(record)
    for field in spec.fields:
        if field.name not in fitted:
            continue
        value = fitted[field.name]
        if value is None:
            continue
        schema_length = (schema_lengths or {}).get(field.name)
        length = min(field.length or 0, schema_length) if schema_length else (field.length or 0)
        if (field.data_type or "").upper() == "NUMBER":
            fitted[field.name] = _fit_number(value, length or 38, field.decimal_places or 0)
        else:
            fitted[field.name] = _fit_text(value, length)
    return fitted


# 沙箱表（变异用例落地的表）字符列是 VARCHAR2(4000)，只有这一个物理上限；
# 数值列是裸 NUMBER（38 位），保证数值比较语义与生产一致
SANDBOX_TEXT_LIMIT = 4000


def fit_sandbox_values(spec: SpecDocument, record: Dict[str, Any]) -> Dict[str, Any]:
    """变异用例进沙箱宽表：只做 4000 上限保护，不按元数据声明长度裁剪。

    长度类规则（LENGTH / LENGTH_MAX）要的就是「超长值」，裁掉等于把规则测成死规则。
    """
    fitted = dict(record)
    for field in spec.fields:
        if field.name not in fitted:
            continue
        value = fitted[field.name]
        if not isinstance(value, str):
            continue
        if len(value) > SANDBOX_TEXT_LIMIT:
            fitted[field.name] = value[:SANDBOX_TEXT_LIMIT]
    return fitted


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
    """沙箱表：字符字段 VARCHAR2(4000) 放开长度，数值字段用原生 NUMBER。

    数值字段必须建成裸 NUMBER（不带精度，支持 38 位任意小数）：
    VARCHAR2 上 FIELD_COMPARE/NUM_RANGE 会退化成字符串字典序比较
    （'90' > '100' 在字典序下成立），黄金样本必误报、数值变异必漏报。
    代价：非数字变异值会在插入时被 Oracle 拒绝——这类用例在绑定阶段
    （bind_audit_cases）就会被拦下，轮到入库的都是数值形态。
    """
    columns = ['"record_id" VARCHAR2(64 CHAR) PRIMARY KEY']
    for field in spec.fields:
        if (field.data_type or "").upper() == "NUMBER":
            columns.append(f"{quote_identifier(field.name, 'oracle')} NUMBER NULL")
        else:
            columns.append(f"{quote_identifier(field.name, 'oracle')} VARCHAR2(4000 CHAR) NULL")
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
                    sql, params = compile_rule_query(rule, "oracle", physical_tables, code_set_bindings)
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
                records[spec.table_code].append(fit_sandbox_values(spec, record))
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
                        golden = fit_record_values(
                            spec,
                            {item.field: item.length for item in schemas[spec.table_code]},
                            {
                                **golden_records[spec.table_code],
                                **generated,
                                "record_id": f"GOLDEN_{index + 1}",
                            },
                        )
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
            for case in audit_cases[spec.table_code]:
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
        uncovered = sorted({rule.rule_key for rule in rules} - hit_rules)
        coverage_rate = (
            round((len(rules) - len(uncovered)) / len(rules), 4) if rules else 1.0
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
