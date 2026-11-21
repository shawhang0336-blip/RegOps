from typing import Any, Dict, Optional, Tuple

from app.infra.domain import RuleDefinition, SpecDocument
from app.infra.sql import quote_identifier

_BRACKET_LITERAL_ESCAPES = set(r".+/()_*?,;:@#$!~=%")


def normalize_oracle_regexp(pattern: str) -> str:
    r"""将正则字符类 [...] 规范化为 Oracle POSIX REGEXP_LIKE 兼容写法（同时保持 Python re 语义完全等价）。

    Oracle POSIX 字符类内反斜杠不作转义：
    - `\-` 会被解析为从 `\` 到后续字符的非法范围（ORA-12728），需将 `-` 移至字符类末尾；
    - `\d`/`\s`/`\w` 在 [...] 内会被当成字面反斜杠和字母，需展开为 `0-9`/` `/`A-Za-z0-9_`；
    - `\.` / `\+` 等多余转义在 [...] 内会多匹配反斜杠本身，需去反斜杠。
    """
    if not pattern or "[" not in pattern:
        return pattern
    out = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\" and i + 1 < n:
            out.append(pattern[i : i + 2])
            i += 2
            continue
        if ch != "[":
            out.append(ch)
            i += 1
            continue
        # 进入字符类 [...]
        j = i + 1
        negated = False
        if j < n and pattern[j] == "^":
            negated = True
            j += 1
        leading_rbracket = False
        if j < n and pattern[j] == "]":
            leading_rbracket = True
            j += 1
        body = []
        trailing_hyphen = False
        closed = False
        while j < n:
            c = pattern[j]
            # 保留 POSIX 具名字符类 [:...:] 原样不拆
            if c == "[" and j + 1 < n and pattern[j + 1] in (":", ".", "="):
                delim = pattern[j + 1]
                end = pattern.find(f"{delim}]", j + 2)
                if end != -1:
                    body.append(pattern[j : end + 2])
                    j = end + 2
                    continue
            if c == "]":
                closed = True
                j += 1
                break
            if c == "\\" and j + 1 < n:
                nxt = pattern[j + 1]
                if nxt == "-":
                    trailing_hyphen = True
                    j += 2
                    continue
                if nxt == "d":
                    body.append("0-9")
                    j += 2
                    continue
                if nxt == "w":
                    body.append("A-Za-z0-9_")
                    j += 2
                    continue
                if nxt == "s":
                    body.append(" ")
                    j += 2
                    continue
                if nxt in _BRACKET_LITERAL_ESCAPES:
                    body.append(nxt)
                    j += 2
                    continue
                body.append(pattern[j : j + 2])
                j += 2
                continue
            body.append(c)
            j += 1
        if not closed:
            out.append(pattern[i:])
            break
        rebuilt = "["
        if negated:
            rebuilt += "^"
        if leading_rbracket:
            rebuilt += "]"
        rebuilt += "".join(body)
        if trailing_hyphen:
            rebuilt += "-"
        rebuilt += "]"
        out.append(rebuilt)
        i = j
    return "".join(out)


def _blank_fold(field: str, dialect: str) -> str:
    """把制表/换行折成空格再 TRIM。

    只 TRIM 空格会把纯 "\\t" 当成有值：Oracle 的 TRIM 默认只去空格，
    于是「必填」在遇到空白填充的脏数据时漏判。必填的语义是「去掉空白后仍有内容」。
    """
    chars = ("CHR(9)", "CHR(10)", "CHR(13)") if dialect == "oracle" else ("CHAR(9)", "CHAR(10)", "CHAR(13)")
    folded = field
    for char in chars:
        folded = f"REPLACE({folded}, {char}, ' ')"
    return f"TRIM({folded})"


def missing_text(field: str, dialect: str) -> str:
    folded = _blank_fold(field, dialect)
    if dialect == "oracle":
        # Oracle 里空串即 NULL，折叠后 TRIM 为 '' 会变成 NULL
        return f"({field} IS NULL OR {folded} IS NULL)"
    return f"({field} IS NULL OR {folded} = '')"


def compile_where(
    rule: RuleDefinition,
    dialect: str = "mysql",
    physical_tables: Optional[Dict[str, str]] = None,
    code_set_bindings: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
) -> Tuple[str, Dict[str, Any]]:
    field = f"t.{quote_identifier(rule.field, dialect)}"
    params: Dict[str, Any] = {}
    if rule.operator == "REQUIRED":
        return missing_text(field, dialect), params
    if rule.operator in {"ENUM", "NOT_IN"}:
        values = list(rule.params["values"])
        # 规则取值与启用码值完全一致时，编译为码表子查询：
        # 值域随码表发布自动同步，且避开 Oracle IN 列表 1000 项上限（ORA-01795）。
        binding = (code_set_bindings or {}).get((rule.table_code, rule.field))
        if rule.operator == "ENUM" and binding:
            rule_values = {str(item) for item in values}
            code_values = {str(item) for item in binding["values"]}
            if rule_values and rule_values == code_values:
                code_table = quote_identifier(binding["table"], dialect)
                code_value_col = quote_identifier("CODE_VALUE", dialect)
                enabled_col = quote_identifier("ENABLED", dialect)
                return (
                    f"({field} IS NOT NULL AND NOT EXISTS "
                    f"(SELECT 1 FROM {code_table} c "
                    f"WHERE c.{code_value_col} = {field} AND c.{enabled_col} = 1))",
                    params,
                )
        operator = "NOT IN" if rule.operator == "ENUM" else "IN"
        null_guard = f"{field} IS NOT NULL AND " if rule.operator == "ENUM" else ""
        # Oracle IN 列表硬上限 1000 项：超出时按 1000 切片，用 AND/OR 连接
        chunk_size = 1000
        if dialect == "oracle" and len(values) > chunk_size:
            chunks = [values[index : index + chunk_size] for index in range(0, len(values), chunk_size)]
            parts = []
            for chunk_index, chunk in enumerate(chunks):
                names = []
                for value_index, value in enumerate(chunk):
                    key = f"value_{chunk_index}_{value_index}"
                    params[key] = value
                    names.append(f":{key}")
                parts.append(f"{field} {operator} ({', '.join(names)})")
            joiner = " AND " if rule.operator == "ENUM" else " OR "
            return f"({null_guard}{joiner.join(parts)})", params
        names = []
        for index, value in enumerate(values):
            key = f"value_{index}"
            params[key] = value
            names.append(f":{key}")
        return f"({null_guard}{field} {operator} ({', '.join(names)}))", params
    if rule.operator == "COND_REQUIRED":
        condition_field = f"t.{quote_identifier(rule.params['if_field'], dialect)}"
        raw_values = list(rule.params["if_values"])
        has_blank = any(value is None or str(value) == "" for value in raw_values)
        non_blank_values = [value for value in raw_values if value is not None and str(value) != ""]
        condition_values = []
        for index, value in enumerate(non_blank_values):
            key = f"if_value_{index}"
            params[key] = value
            condition_values.append(f":{key}")
        is_eq = rule.params["if_operator"] == "EQ"
        # Oracle 中空串 '' 等价于 NULL，col <> '' 或 col NOT IN ('', NULL) 恒为 UNKNOWN（永远不命中）。
        # 含空值哨兵时改为显式 NOT missing_text / missing_text 谓词，兼容 VARCHAR2 与 NUMBER 列。
        if not condition_values:
            condition = missing_text(condition_field, dialect) if is_eq else f"NOT {missing_text(condition_field, dialect)}"
            return f"({condition} AND {missing_text(field, dialect)})", params
        if len(condition_values) == 1:
            cmp_expr = f"{condition_field} {'=' if is_eq else '<>'} {condition_values[0]}"
        else:
            cmp_expr = f"{condition_field} {'IN' if is_eq else 'NOT IN'} ({', '.join(condition_values)})"
        if has_blank:
            if is_eq:
                cond_expr = f"({missing_text(condition_field, dialect)} OR {cmp_expr})"
            else:
                cond_expr = f"NOT {missing_text(condition_field, dialect)} AND {cmp_expr}"
        else:
            cond_expr = f"{condition_field} IS NOT NULL AND {cmp_expr}"
        return f"({cond_expr} AND {missing_text(field, dialect)})", params
    if rule.operator in {"LENGTH", "LENGTH_MAX"}:
        params["expected_length"] = int(rule.params["length"])
        comparison = "<>" if rule.operator == "LENGTH" else ">"
        return f"({field} IS NOT NULL AND LENGTH({field}) {comparison} :expected_length)", params
    if rule.operator == "REGEXP":
        raw_pattern = rule.params["pattern"]
        if dialect == "oracle":
            params["expected_pattern"] = normalize_oracle_regexp(raw_pattern)
            return f"({field} IS NOT NULL AND NOT REGEXP_LIKE({field}, :expected_pattern))", params
        params["expected_pattern"] = raw_pattern
        return f"({field} IS NOT NULL AND {field} NOT REGEXP :expected_pattern)", params
    if rule.operator == "FOREIGN_KEY":
        reference_code = rule.params["references_table"]
        reference_table = (physical_tables or {}).get(reference_code, reference_code)
        reference_field = quote_identifier(rule.params["references_field"], dialect)
        return (
            f"({field} IS NOT NULL AND NOT EXISTS "
            f"(SELECT 1 FROM {quote_identifier(reference_table, dialect)} r "
            f"WHERE r.{reference_field} = {field}))",
            params,
        )
    if rule.operator == "NUM_RANGE":
        # 违规 = 越出 [min, max]；inclusive=False 时边界本身也算违规
        bounds = []
        min_value = rule.params.get("min")
        max_value = rule.params.get("max")
        if min_value is not None:
            params["range_min"] = float(min_value)
            bounds.append(f"{field} {'<' if rule.params.get('min_inclusive', True) else '<='} :range_min")
        if max_value is not None:
            params["range_max"] = float(max_value)
            bounds.append(f"{field} {'>' if rule.params.get('max_inclusive', True) else '>='} :range_max")
        if not bounds:
            raise ValueError("NUM_RANGE requires at least one of min/max")
        return f"({field} IS NOT NULL AND ({' OR '.join(bounds)}))", params
    if rule.operator == "FIELD_COMPARE":
        # 规则断言 field OP right_field 必须成立；违规 = 两列都有值但比较不成立
        right_field = f"t.{quote_identifier(rule.params['right_field'], dialect)}"
        inverse = {"LT": ">=", "LE": ">", "GT": "<=", "GE": "<", "EQ": "<>", "NE": "="}
        comparison = inverse[rule.params["operator"]]
        return f"({field} IS NOT NULL AND {right_field} IS NOT NULL AND {field} {comparison} {right_field})", params
    raise ValueError(f"Unsupported operator: {rule.operator}")


def compile_rule_query(
    rule: RuleDefinition,
    dialect: str = "mysql",
    physical_tables: Optional[Dict[str, str]] = None,
    code_set_bindings: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
) -> Tuple[str, Dict[str, Any]]:
    where, params = compile_where(rule, dialect, physical_tables, code_set_bindings)
    params["compiled_rule_key"] = rule.rule_key
    table = (physical_tables or {}).get(rule.table_code, rule.table_code)
    table_alias = "t" if dialect == "oracle" else "AS t"
    sql = (
        f"SELECT :compiled_rule_key AS rule_key, t.{quote_identifier('record_id', dialect)} AS record_id "
        f"FROM {quote_identifier(table, dialect)} {table_alias} WHERE {where}"
    )
    return sql, params


def compile_mysql_ddl(spec: SpecDocument) -> str:
    columns = ["`record_id` VARCHAR(64) NOT NULL PRIMARY KEY"]
    for field in spec.fields:
        if field.data_type == "NUMBER":
            data_type = f"DECIMAL({field.length},{field.decimal_places or 0})"
        else:
            data_type = f"VARCHAR({field.length})"
        columns.append(f"{quote_identifier(field.name)} {data_type} NULL")
    body = ",\n  ".join(columns)
    return (
        f"CREATE TABLE IF NOT EXISTS {quote_identifier(spec.table_code)} (\n  {body}\n) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
    )
