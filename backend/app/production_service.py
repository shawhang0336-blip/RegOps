import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Dict, Tuple

from sqlalchemy.orm import Session

from .asset_models import ArtifactModel
from .blobs import LocalBlobStore
from .code_set_oracle import code_set_compile_bindings, compile_code_set_oracle_ddl, compile_code_set_oracle_insert, compile_code_set_oracle_merge, load_published_code_sets, stable_code_set_table
from .domain import RuleDefinition, canonical_json
from .events import project
from .generic_domain import SchemaBinding
from .generic_workflow import _latest_payload, _specs
from .models import RuleModel, RunModel
from .production import compile_production_mysql_ddl, compile_production_oracle_ddl, compile_production_rule_query
from .errors import WorkflowError


def build_production_bundle(session: Session, run: RunModel, blob_dir: str) -> Tuple[str, bytes]:
    state = project(session, run)
    if state.status != "COMPLETED" or not state.result_json or not state.result_json.get("audit_summary", {}).get("all_passed"):
        raise WorkflowError("Production export requires a completed, fully passed Oracle audit")
    if state.result_json.get("audit_summary", {}).get("coverage_complete") is False:
        raise WorkflowError("Production export is blocked by uncovered validation rules")
    pending = session.query(RuleModel).filter(RuleModel.run_id == run.id, RuleModel.status == "PENDING").count()
    if pending:
        raise WorkflowError("Production export is blocked by unapproved rules")
    specs = _specs(session, run.spec_id)
    schema_payload = _latest_payload(session, run.id, "SCHEMA_ASSIGNED")["tables"]
    schemas = {
        code: [SchemaBinding.model_validate(item) for item in items]
        for code, items in schema_payload.items()
    }
    rows = session.query(RuleModel).filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED").all()
    rules = [RuleDefinition.model_validate(row.definition_json) for row in rows]
    expected_versions = state.result_json["audit_summary"].get("code_set_versions", {})
    code_sets, field_code_sets = load_published_code_sets(session, specs, expected_versions=expected_versions)
    if code_sets and "code_set_versions" not in state.result_json["audit_summary"]:
        raise WorkflowError("This run predates code set auditing; rerun the Oracle audit before production export")
    code_set_tables = {item["code"]: stable_code_set_table(item["code"]) for item in code_sets}
    rule_bindings = code_set_compile_bindings(field_code_sets, code_set_tables)
    oracle_code_set_ddl = []
    oracle_code_set_insert = []
    oracle_code_set_merge = []
    for code_set in code_sets:
        table = code_set_tables[code_set["code"]]
        comment = f"-- {code_set['code']} | {code_set['name']} | audited version {code_set['version']}\n"
        oracle_code_set_ddl.append(comment + compile_code_set_oracle_ddl(table) + ";")
        insert_sql = compile_code_set_oracle_insert(code_set, table)
        merge_sql = compile_code_set_oracle_merge(code_set, table)
        oracle_code_set_insert.append(comment + insert_sql + (";" if insert_sql else ""))
        oracle_code_set_merge.append(comment + merge_sql + (";" if merge_sql else ""))
    oracle_ddl = "\n\n".join(compile_production_oracle_ddl(spec.table_code, schemas[spec.table_code]) + ";" for spec in specs)
    mysql_ddl = "\n\n".join(compile_production_mysql_ddl(spec.table_code, schemas[spec.table_code]) + ";" for spec in specs)
    oracle_rules = []
    for rule in sorted(rules, key=lambda item: (item.table_code, item.rule_key)):
        oracle_rules.append(
            f"-- {rule.rule_key} | {rule.source} | {rule.rule_hash}\n"
            f"-- {rule.rationale}\n"
            f"{compile_production_rule_query(rule, 'oracle', code_set_bindings=rule_bindings)};"
        )
    manifest = {
        "bundle_type": "PRODUCTION_DEPLOYMENT",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run.id,
        "audit": state.result_json["audit_summary"],
        "tables": [
            {
                "table_code": spec.table_code,
                "table_name": spec.table_name,
                "source_sha256": spec.source_sha256,
                "source_name": spec.source_name,
                "spec_version": spec.version,
            }
            for spec in specs
        ],
        "rule_count": len(rules),
        "code_sets": [
            {
                "code": item["code"],
                "name": item["name"],
                "version": item["version"],
                "table": code_set_tables[item["code"]],
                "value_count": len(item["values"]),
            }
            for item in code_sets
        ],
        "contains_test_data": False,
        "uses_run_specific_tables": False,
        "contains_synthetic_record_id": False,
        "deployment_order": [
            "Backup target schemas",
            "Review manifest.json and rules.json",
            "Run oracle/01_report_tables.sql or mysql/01_report_tables.sql as appropriate",
            "Run oracle/02_code_set_tables.sql",
            "Run oracle/03_code_values_insert.sql for initial deployment, or oracle/04_code_values_merge.sql for repeatable updates",
            "Run oracle/05_validation_rules.sql in report mode",
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("oracle/01_report_tables.sql", oracle_ddl + "\n")
        archive.writestr("oracle/02_code_set_tables.sql", "\n\n".join(oracle_code_set_ddl) + "\n")
        archive.writestr("oracle/03_code_values_insert.sql", "\n\n".join(oracle_code_set_insert) + "\n")
        archive.writestr("oracle/04_code_values_merge.sql", "\n\n".join(oracle_code_set_merge) + "\n")
        archive.writestr("oracle/05_validation_rules.sql", "\n\n".join(oracle_rules) + "\n")
        archive.writestr("mysql/01_report_tables.sql", mysql_ddl + "\n")
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("rules.json", canonical_json([rule.model_dump() for rule in rules]))
    content = buffer.getvalue()
    blobs = LocalBlobStore(blob_dir)
    uri = blobs.put(content)
    session.add(
        ArtifactModel(
            id=str(uuid.uuid4()),
            run_id=run.id,
            artifact_type="PRODUCTION_BUNDLE",
            blob_uri=uri,
            sha256=uri.removeprefix("blob://"),
            version=state.attempt,
        )
    )
    session.commit()
    return uri, content
