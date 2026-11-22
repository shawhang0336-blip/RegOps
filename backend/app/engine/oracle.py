"""Oracle 沙箱管理与规则真实验证执行器（阶段二核心执行组件）。

职责边界：
- `physical_table_name` / `OracleSandbox`：批次专属沙箱物理表命名、数据抽样预览与批量清理；
- `compile_ai_schema_oracle_ddl` / `compile_ai_schema_mysql_ddl`：根据字段 Schema 绑定编译建表 DDL；
- `GenericOracleExecutor`：在 Oracle 沙箱中建表、灌入黄金样本与变异样本、真跑校验 SQL 并输出审计报告。
"""

import copy
import hashlib
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, text

from app.engine.code_set_sql import compile_code_set_oracle_ddl, sandbox_code_set_table
from app.engine.binding import materialize_golden_record
from app.engine.compiler import compile_rule_query
from app.infra.contracts import AuditCaseBinding, GeneratorBinding, RuleDefinition, SchemaBinding, SpecDocument
from app.infra.db import quote_identifier

# Oracle 12cR2+/23 标识符硬上限 128 字节；预留 7 字节给 "_TARGET" 后缀
_MAX_SANDBOX_TABLE_LEN = 120


def run_table_prefix(run_id: str) -> str:
    run_part = run_id.replace("-", "").upper()[:12]
    return f"DQ_{run_part}_"


def physical_table_name(run_id: str, attempt: int, table_code: str) -> str:
    raw = f"{run_table_prefix(run_id)}{attempt}_{table_code}"
    if len(raw) <= _MAX_SANDBOX_TABLE_LEN:
        return raw
    prefix = f"{run_table_prefix(run_id)}{attempt}_"
    digest = hashlib.sha256(table_code.encode("utf-8")).hexdigest()[:8].upper()
    keep = max(8, _MAX_SANDBOX_TABLE_LEN - len(prefix) - len(digest) - 1)
    return f"{prefix}{table_code[:keep]}_{digest}"


def _drop_sandbox_table_if_exists(connection, table: str) -> None:
    """幂等清理同名沙箱残表：Oracle 的 CREATE TABLE 会隐式 COMMIT，重试前先清残表防 ORA-00955。"""
    quoted = quote_identifier(table, "oracle")
    is_oracle = connection.dialect.name.startswith("oracle")
    stmt = f"DROP TABLE {quoted} PURGE" if is_oracle else f"DROP TABLE IF EXISTS {quoted}"
    try:
        connection.execute(text(stmt))
    except Exception as exc:
        if "ORA-00942" in str(exc) or "no such table" in str(exc).lower():
            return
        raise


class OracleSandbox:
    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(database_url, pool_pre_ping=True)

    def preview(self, physical_table: str, limit: int = 20) -> List[Dict]:
        quoted = quote_identifier(physical_table, "oracle")
        try:
            with self.engine.connect() as connection:
                result = connection.execute(
                    text(f"SELECT * FROM {quoted} WHERE ROWNUM <= :row_limit"),
                    {"row_limit": limit},
                )
                return [
                    {k: (v.read() if hasattr(v, "read") else v) for k, v in row.items()}
                    for row in result.mappings().all()
                ]
        finally:
            self.engine.dispose()

    def _discover_run_tables(self, connection, run_ids: List[str]) -> List[str]:
        discovered: List[str] = []
        for run_id in run_ids:
            if not run_id:
                continue
            prefix = run_table_prefix(run_id).replace("_", r"\_") + "%"
            try:
                rows = connection.execute(
                    text("SELECT TABLE_NAME FROM USER_TABLES WHERE TABLE_NAME LIKE :prefix ESCAPE '\\'"),
                    {"prefix": prefix},
                ).fetchall()
                discovered.extend(str(row[0]) for row in rows if row and row[0])
            except Exception:
                # 非 Oracle 测试方言或无 USER_TABLES 视图时回退到事件中收集的表清单
                continue
        return discovered

    def drop_tables(self, tables: List[str], run_ids: Optional[List[str]] = None) -> List[str]:
        """清理批次专属沙箱表；单表失败不影响其他表，也不触碰生产表。"""
        errors: List[str] = []
        try:
            with self.engine.begin() as connection:
                all_tables = {item for item in tables if item}
                if run_ids:
                    all_tables.update(self._discover_run_tables(connection, list(run_ids)))
                for table in sorted(all_tables):
                    try:
                        connection.execute(text(f"DROP TABLE {quote_identifier(table, 'oracle')} PURGE"))
                    except Exception as exc:
                        if "ORA-00942" in str(exc):
                            continue
                        errors.append(f"{table}: {type(exc).__name__}")
        finally:
            self.engine.dispose()
        return errors


def _fit_text(value: Any, length: int) -> Any:
    text_value = str(value)
    if length and len(text_value) > length:
        text_value = text_value[:length]
    # Oracle AL32UTF8 下 VARCHAR2(<=4000 CHAR) 仍有 4000 字节上限；>4000 在 DDL 中会升格为 CLOB
    if length <= SANDBOX_TEXT_LIMIT and len(text_value.encode("utf-8")) > SANDBOX_TEXT_LIMIT:
        text_value = _truncate_utf8_bytes(text_value, SANDBOX_TEXT_LIMIT)
    return text_value


def _fit_number(value: Any, length: int, decimals: int) -> Any:
    raw = value.replace(",", "").strip() if isinstance(value, str) else value
    try:
        number = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return value
    if not number.is_finite():
        return value
    precision = max(1, min(int(length or 38), 38))
    scale = max(0, min(int(decimals or 0), precision))
    # Python Decimal 默认精度仅 28 位，无法准确表达 Oracle NUMBER(38, s) 的截断与上限（否则 quantize 抛异常或 limit 进位引发 ORA-01438）
    with localcontext() as ctx:
        ctx.prec = max(42, precision + 4)
        quantum = Decimal(1).scaleb(-scale)
        number = number.quantize(quantum, rounding=ROUND_DOWN)
        integer_digits = max(precision - scale, 0)
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
        spec_length = field.length or 0
        schema_length = (schema_lengths or {}).get(field.name) or 0
        if spec_length and schema_length:
            length = min(spec_length, schema_length)
        else:
            length = schema_length or spec_length
        if (field.data_type or "").upper() == "NUMBER":
            fitted[field.name] = _fit_number(value, length or 38, field.decimal_places or 0)
        else:
            fitted[field.name] = _fit_text(value, length)
    return fitted


# 沙箱表（变异用例落地的表）字符列是 VARCHAR2(4000)，只有这一个物理上限；
# 数值列是裸 NUMBER（38 位），保证数值比较语义与生产一致
SANDBOX_TEXT_LIMIT = 4000


def _truncate_utf8_bytes(text_value: str, max_bytes: int) -> str:
    """按 UTF-8 字节上限安全截断（Oracle AL32UTF8 下 VARCHAR2(4000 CHAR) 仍有 4000 字节硬上限，防 ORA-12899）。"""
    encoded = text_value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text_value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def fit_sandbox_values(spec: SpecDocument, record: Dict[str, Any]) -> Dict[str, Any]:
    """变异用例进沙箱宽表：只做 4000 字节上限保护，不按元数据声明长度裁剪。

    长度类规则（LENGTH / LENGTH_MAX）要的就是「超长值」，裁掉等于把规则测成死规则。
    数值列顺手去千分位逗号：校验阶段允许 "1,000.00" 这种写法（见 _as_number），
    但 Oracle NUMBER 列不认逗号，直接插会 ORA-01722。
    """
    numeric_fields = {
        field.name for field in spec.fields if (field.data_type or "").upper() == "NUMBER"
    }
    fitted = dict(record)
    for field in spec.fields:
        if field.name not in fitted:
            continue
        value = fitted[field.name]
        if not isinstance(value, str):
            continue
        if field.name in numeric_fields and "," in value:
            cleaned = value.replace(",", "").strip()
            try:
                if Decimal(cleaned).is_finite():
                    fitted[field.name] = cleaned
                    continue
            except (InvalidOperation, ValueError):
                pass  # 非数字保留原样：绑定阶段已拦截，真漏过来就让 Oracle 大声报错
        if len(value) > SANDBOX_TEXT_LIMIT or len(value.encode("utf-8")) > SANDBOX_TEXT_LIMIT:
            fitted[field.name] = _truncate_utf8_bytes(value[:SANDBOX_TEXT_LIMIT], SANDBOX_TEXT_LIMIT)
    return fitted


def compile_ai_schema_oracle_ddl(
    table: str,
    schema: List[SchemaBinding],
) -> str:
    columns = ['"record_id" VARCHAR2(128 CHAR) PRIMARY KEY']
    for field in schema:
        if field.storage_type == "NUMBER":
            precision = max(1, min(int(field.length or 38), 38))
            scale = max(0, min(int(field.decimal_places or 0), precision))
            data_type = f"NUMBER({precision},{scale})"
        else:
            char_len = max(1, int(field.length or 64))
            data_type = "CLOB" if char_len > 4000 else f"VARCHAR2({char_len} CHAR)"
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
    columns = ['"record_id" VARCHAR2(128 CHAR) PRIMARY KEY']
    for field in spec.fields:
        if (field.data_type or "").upper() == "NUMBER":
            columns.append(f"{quote_identifier(field.name, 'oracle')} NUMBER NULL")
        else:
            columns.append(f"{quote_identifier(field.name, 'oracle')} VARCHAR2(4000 CHAR) NULL")
    return f"CREATE TABLE {quote_identifier(table, 'oracle')} (\n  " + ",\n  ".join(columns) + "\n)"


def compile_ai_schema_mysql_ddl(table: str, schema: List[SchemaBinding]) -> str:
    # MySQL utf8mb4 单行所有内联列总字节上限 65535（超限报 Error 1118: Row size too large），
    # 单列 VARCHAR > 16383 直接升格 TEXT；宽表总字节逼近上限时按最宽文本列依次升格 TEXT。
    _MYSQL_ROW_INLINE_BUDGET = 60000
    col_specs: List[Dict[str, Any]] = []
    inline_bytes = 64 * 4 + 2  # `record_id` VARCHAR(64)
    for field in schema:
        if field.storage_type == "NUMBER":
            precision = max(1, min(int(field.length or 18), 65))
            scale = max(0, min(int(field.decimal_places or 0), min(30, precision)))
            col_specs.append({"field": field.field, "type": f"DECIMAL({precision},{scale})", "text_len": 0})
            inline_bytes += (precision // 9 + 1) * 4 + 2
        else:
            char_len = max(1, int(field.length or 64))
            if char_len > 16383:
                col_specs.append({"field": field.field, "type": "TEXT", "text_len": 0})
                inline_bytes += 20
            else:
                col_specs.append({"field": field.field, "type": f"VARCHAR({char_len})", "text_len": char_len})
                inline_bytes += char_len * 4 + 2
    if inline_bytes > _MYSQL_ROW_INLINE_BUDGET:
        for idx in sorted(
            (i for i, item in enumerate(col_specs) if item["text_len"] > 0),
            key=lambda i: col_specs[i]["text_len"],
            reverse=True,
        ):
            if inline_bytes <= _MYSQL_ROW_INLINE_BUDGET:
                break
            inline_bytes -= col_specs[idx]["text_len"] * 4 + 2
            inline_bytes += 20
            col_specs[idx]["type"] = "TEXT"
            col_specs[idx]["text_len"] = 0
    columns = ["`record_id` VARCHAR(64) NOT NULL PRIMARY KEY"]
    for item in col_specs:
        columns.append(f"{quote_identifier(item['field'])} {item['type']} NULL")
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
                    _drop_sandbox_table_if_exists(connection, table)
                    connection.execute(text(compile_code_set_oracle_ddl(table)))
                    statement = text(
                        f"INSERT INTO {quote_identifier(table, 'oracle')} "
                        '("CODE_VALUE", "LABEL_CN", "LABEL_EN", "DEFINITION", "SORT_ORDER", "ENABLED") '
                        "VALUES (:code_value, :label_cn, :label_en, :definition, :sort_order, :enabled)"
                    )
                    batch_rows = [
                        {
                            "code_value": value["value"],
                            "label_cn": value["label_cn"],
                            "label_en": value.get("label_en") or "",
                            "definition": value.get("definition") or "",
                            "sort_order": value.get("sort_order", 0),
                            "enabled": 1 if value.get("enabled", True) else 0,
                        }
                        for value in code_set["values"]
                    ]
                    if batch_rows:
                        connection.execute(statement, batch_rows)
                for spec in specs:
                    _drop_sandbox_table_if_exists(connection, target_tables[spec.table_code])
                    connection.execute(text(compile_ai_schema_oracle_ddl(target_tables[spec.table_code], schemas[spec.table_code])))
                    _drop_sandbox_table_if_exists(connection, physical_tables[spec.table_code])
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
        from app.engine.mutations import make_distinct_key_value

        for spec in specs:
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
            for row_idx, case in enumerate(audit_cases[spec.table_code]):
                is_golden = "GOLDEN" in case.case_id.upper()
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
                            record[primary_kf] = materialize_golden_record(spec, [gen], row_idx + 1)[primary_kf]
                        else:
                            record[primary_kf] = make_distinct_key_value(
                                record.get(primary_kf), spec_fields.get(primary_kf), row_idx + 1
                            )
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
                    _drop_sandbox_table_if_exists(connection, table)
                    connection.execute(text(compile_code_set_oracle_ddl(table)))
                    statement = text(
                        f"INSERT INTO {quote_identifier(table, 'oracle')} "
                        '("CODE_VALUE", "LABEL_CN", "LABEL_EN", "DEFINITION", "SORT_ORDER", "ENABLED") '
                        "VALUES (:code_value, :label_cn, :label_en, :definition, :sort_order, :enabled)"
                    )
                    cs_rows = [
                        {
                            "code_value": value["value"],
                            "label_cn": value["label_cn"],
                            "label_en": value.get("label_en") or "",
                            "definition": value.get("definition") or "",
                            "sort_order": value.get("sort_order", 0),
                            "enabled": 1 if value.get("enabled", True) else 0,
                        }
                        for value in code_set["values"]
                    ]
                    if cs_rows:
                        connection.execute(statement, cs_rows)
                for spec in specs:
                    table = physical_tables[spec.table_code]
                    target_table = target_tables[spec.table_code]
                    names = ["record_id"] + [field.name for field in spec.fields]
                    parameters = [f"value_{index}" for index in range(len(names))]
                    if prepared_assets:
                        # 幂等清理：若上次同 attempt 执行因瞬时网络抖动中断重试，先清旧行防 ORA-00001 主键冲突
                        connection.execute(text(f"DELETE FROM {quote_identifier(target_table, 'oracle')}"))
                    else:
                        _drop_sandbox_table_if_exists(connection, target_table)
                        connection.execute(text(compile_ai_schema_oracle_ddl(target_table, schemas[spec.table_code])))
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
                    if prepared_assets:
                        connection.execute(text(f"DELETE FROM {quote_identifier(table, 'oracle')}"))
                    else:
                        _drop_sandbox_table_if_exists(connection, table)
                        connection.execute(text(compile_sandbox_oracle_ddl(table, spec)))
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
