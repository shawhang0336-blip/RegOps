"""Oracle 沙箱管理、DDL 编译与 Step 2 空跑资产准备器（Layer 5: generation/sql）。

职责边界：
- `physical_table_name` / `OracleSandbox`：批次专属沙箱物理表命名、数据抽样预览与批量清理；
- `compile_ai_schema_oracle_ddl` / `compile_sandbox_oracle_ddl` / `compile_ai_schema_mysql_ddl`：建表 DDL 编译；
- `OracleAssetPreparer`：Step 2 建码表+灌启用码值+建生产同构/宽沙箱表+规则 SQL 空跑验证（`prepare_assets`）。
  Step 3 的 AB 样本真跑与审计统计由 Layer 6 `delivery/testing/sandbox_audit.py`（`GenericOracleExecutor`）承载。
"""

import hashlib
import re
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, text

from app.generation.core.binding import SANDBOX_TEXT_LIMIT
from app.generation.sql.code_set_sql import (
    code_set_value_rows,
    compile_code_set_oracle_ddl,
    sandbox_code_set_table,
)
from app.generation.sql.compiler import compile_rule_query
from app.infra.contracts import RuleDefinition, SchemaBinding, SpecDocument
from app.infra.db import quote_identifier
from app.infra.errors import WorkflowError

# Oracle 12cR2+/23 标识符硬上限 128 字节（UTF-8 字节，非字符数）；预留 7 字节给 "_TARGET" 后缀
_MAX_SANDBOX_TABLE_BYTES = 120


def run_table_prefix(run_id: str) -> str:
    run_part = run_id.replace("-", "").upper()[:12]
    return f"DQ_{run_part}_"


def physical_table_name(run_id: str, attempt: int, table_code: str) -> str:
    raw = f"{run_table_prefix(run_id)}{attempt}_{table_code}"
    if len(raw.encode("utf-8")) <= _MAX_SANDBOX_TABLE_BYTES:
        return raw
    prefix = f"{run_table_prefix(run_id)}{attempt}_"
    digest = hashlib.sha256(table_code.encode("utf-8")).hexdigest()[:8].upper()
    max_code_bytes = max(8, _MAX_SANDBOX_TABLE_BYTES - len(prefix.encode("utf-8")) - len(digest) - 1)
    clipped = _truncate_utf8_bytes(table_code, max_code_bytes)
    return f"{prefix}{clipped}_{digest}"


def _adapt_sandbox_ddl(ddl: str, connection) -> str:
    """Oracle 23 保持原样；本地离线 SQLite 测试沙箱去掉 Oracle 专属 `CHAR` 修饰词，便于真实建表联调。"""
    if connection.dialect.name == "sqlite":
        return re.sub(r"\bVARCHAR2\((\d+)\s+CHAR\)", r"VARCHAR(\1)", ddl)
    return ddl


def _drop_sandbox_table_if_exists(connection, table: str) -> None:
    """幂等清理同名沙箱残表：Oracle 的 CREATE TABLE 会隐式 COMMIT，重试前先清残表防 ORA-00955。"""
    is_oracle = connection.dialect.name.startswith("oracle")
    quoted = quote_identifier(table, "oracle" if is_oracle else "mysql")
    stmt = f"DROP TABLE {quoted} PURGE" if is_oracle else f"DROP TABLE IF EXISTS {quoted}"
    try:
        connection.execute(text(stmt))
    except Exception as exc:
        if "ORA-00942" in str(exc) or "no such table" in str(exc).lower() or "unknown table" in str(exc).lower():
            return
        raise


class OracleSandbox:
    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(database_url, pool_pre_ping=True)

    def preview(self, physical_table: str, limit: int = 20) -> List[Dict]:
        try:
            with self.engine.connect() as connection:
                is_oracle = connection.dialect.name.startswith("oracle")
                quoted = quote_identifier(physical_table, "oracle" if is_oracle else "mysql")
                sql = (
                    f"SELECT * FROM {quoted} WHERE ROWNUM <= :row_limit"
                    if is_oracle
                    else f"SELECT * FROM {quoted} LIMIT :row_limit"
                )
                result = connection.execute(text(sql), {"row_limit": limit})
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
                is_oracle = connection.dialect.name.startswith("oracle")
                all_tables = {item for item in tables if item}
                if run_ids:
                    all_tables.update(self._discover_run_tables(connection, list(run_ids)))
                for table in sorted(all_tables):
                    try:
                        quoted = quote_identifier(table, "oracle" if is_oracle else "mysql")
                        stmt = f"DROP TABLE {quoted} PURGE" if is_oracle else f"DROP TABLE IF EXISTS {quoted}"
                        connection.execute(text(stmt))
                    except Exception as exc:
                        if "ORA-00942" in str(exc) or "no such table" in str(exc).lower():
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


class OracleAssetPreparer:
    """Step 2 Oracle 沙箱资产准备器：建码表、灌码值、建表并对全部规则执行真实空跑校验。"""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def _populate_code_set_table(self, connection, table: str, code_set: Dict[str, Any]) -> None:
        _drop_sandbox_table_if_exists(connection, table)
        connection.execute(text(_adapt_sandbox_ddl(compile_code_set_oracle_ddl(table), connection)))
        statement = text(
            f"INSERT INTO {quote_identifier(table, 'oracle')} "
            '("CODE_VALUE", "LABEL_CN", "LABEL_EN", "DEFINITION", "SORT_ORDER", "ENABLED") '
            "VALUES (:code_value, :label_cn, :label_en, :definition, :sort_order, :enabled)"
        )
        batch_rows = [
            {
                "code_value": row["CODE_VALUE"],
                "label_cn": row["LABEL_CN"],
                "label_en": row["LABEL_EN"],
                "definition": row["DEFINITION"],
                "sort_order": row["SORT_ORDER"],
                "enabled": row["ENABLED"],
            }
            for row in code_set_value_rows(code_set)
        ]
        if batch_rows:
            connection.execute(statement, batch_rows)

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
                    self._populate_code_set_table(connection, code_set_tables[code_set["code"]], code_set)
                for spec in specs:
                    _drop_sandbox_table_if_exists(connection, target_tables[spec.table_code])
                    connection.execute(
                        text(_adapt_sandbox_ddl(compile_ai_schema_oracle_ddl(target_tables[spec.table_code], schemas[spec.table_code]), connection))
                    )
                    _drop_sandbox_table_if_exists(connection, physical_tables[spec.table_code])
                    connection.execute(
                        text(_adapt_sandbox_ddl(compile_sandbox_oracle_ddl(physical_tables[spec.table_code], spec), connection))
                    )
                for rule in rules or []:
                    sql = ""
                    try:
                        sql, params = compile_rule_query(rule, "oracle", physical_tables, code_set_bindings)
                        connection.execute(text(sql), params).fetchall()
                    except Exception as exc:
                        raise WorkflowError(
                            f"Oracle 空跑校验失败（表 {rule.table_code} / 字段 {rule.field} / 规则 {rule.rule_key}）: {exc}",
                            context={
                                "stage": "oracle_asset_builder",
                                "error_kind": "oracle_precheck",
                                "table_code": rule.table_code,
                                "field": rule.field,
                                "rule_key": rule.rule_key,
                                "operator": rule.operator,
                                "sql": sql,
                                "db_error": str(exc),
                            },
                        ) from exc
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
