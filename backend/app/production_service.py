import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Dict, Tuple

from sqlalchemy.orm import Session

from .asset_models import ArtifactModel
from .blobs import LocalBlobStore
from .domain import RuleDefinition, canonical_json
from .generic_domain import SchemaBinding
from .generic_workflow import _latest_payload, _specs
from .models import RuleModel, RunModel
from .production import compile_production_mysql_ddl, compile_production_oracle_ddl, compile_production_rule_query
from .workflow import WorkflowError


def build_production_bundle(session: Session, run: RunModel, blob_dir: str) -> Tuple[str, bytes]:
    if run.status != "COMPLETED" or not run.result_json or not run.result_json.get("audit_summary", {}).get("all_passed"):
        raise WorkflowError("Production export requires a completed, fully passed Oracle audit")
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
    oracle_ddl = "\n\n".join(compile_production_oracle_ddl(spec.table_code, schemas[spec.table_code]) + ";" for spec in specs)
    mysql_ddl = "\n\n".join(compile_production_mysql_ddl(spec.table_code, schemas[spec.table_code]) + ";" for spec in specs)
    oracle_rules = []
    for rule in sorted(rules, key=lambda item: (item.table_code, item.rule_key)):
        oracle_rules.append(
            f"-- {rule.rule_key} | {rule.source} | {rule.rule_hash}\n"
            f"-- {rule.rationale}\n"
            f"{compile_production_rule_query(rule, 'oracle')};"
        )
    manifest = {
        "bundle_type": "PRODUCTION_DEPLOYMENT",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run.id,
        "audit": run.result_json["audit_summary"],
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
        "contains_test_data": False,
        "uses_run_specific_tables": False,
        "contains_synthetic_record_id": False,
        "deployment_order": [
            "Backup target schemas",
            "Review manifest.json and rules.json",
            "Run oracle/01_create_tables.sql or mysql/01_create_tables.sql as appropriate",
            "Run oracle/02_validation_rules.sql in report mode",
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("oracle/01_create_tables.sql", oracle_ddl + "\n")
        archive.writestr("oracle/02_validation_rules.sql", "\n\n".join(oracle_rules) + "\n")
        archive.writestr("mysql/01_create_tables.sql", mysql_ddl + "\n")
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
            version=run.attempt,
        )
    )
    session.commit()
    return uri, content
