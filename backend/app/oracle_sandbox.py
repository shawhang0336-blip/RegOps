from typing import Dict, Iterable, List

from sqlalchemy import create_engine, text

from .audit import build_bundle_report, bundle_audit_cases
from .compiler import compile_oracle_ddl, compile_rule_query, quote_identifier
from .domain import RuleDefinition, SpecDocument


def physical_table_name(run_id: str, attempt: int, table_code: str) -> str:
    run_part = run_id.replace("-", "").upper()[:12]
    return f"DQ_{run_part}_{attempt}_{table_code}"


class OracleSandbox:
    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(database_url, pool_pre_ping=True)

    def run_audit(
        self,
        run_id: str,
        attempt: int,
        specs: List[SpecDocument],
        rules: Iterable[RuleDefinition],
    ) -> Dict:
        active_rules = list(rules)
        cases_by_table = bundle_audit_cases()
        physical_tables = {
            spec.table_code: physical_table_name(run_id, attempt, spec.table_code) for spec in specs
        }
        hits: Dict[str, Dict[str, List[str]]] = {
            code: {case.case_id: [] for case in cases} for code, cases in cases_by_table.items()
        }
        try:
            with self.engine.begin() as connection:
                for spec in specs:
                    connection.execute(text(compile_oracle_ddl(spec, physical_tables[spec.table_code])))
                    names = ["record_id"] + [field.name for field in spec.fields]
                    parameters = [f"value_{index}" for index in range(len(names))]
                    statement = text(
                        f"INSERT INTO {quote_identifier(physical_tables[spec.table_code], 'oracle')} "
                        f"({', '.join(quote_identifier(name, 'oracle') for name in names)}) "
                        f"VALUES ({', '.join(':' + name for name in parameters)})"
                    )
                    for case in cases_by_table[spec.table_code]:
                        row = dict(zip(parameters, [case.record.get(name) for name in names]))
                        connection.execute(statement, row)
                for rule in active_rules:
                    sql, params = compile_rule_query(rule, "oracle", physical_tables)
                    for row in connection.execute(text(sql), params).fetchall():
                        hits[rule.table_code][row[1]].append(row[0])
            report = build_bundle_report(specs, active_rules, cases_by_table, hits)
            report["backend"] = "ORACLE"
            report["physical_tables"] = physical_tables
            return report
        finally:
            self.engine.dispose()

    def preview(self, physical_table: str, limit: int = 20) -> List[Dict]:
        quoted = quote_identifier(physical_table, "oracle")
        try:
            with self.engine.connect() as connection:
                result = connection.execute(text(f"SELECT * FROM {quoted} WHERE ROWNUM <= :row_limit"), {"row_limit": limit})
                return [dict(row) for row in result.mappings().all()]
        finally:
            self.engine.dispose()
