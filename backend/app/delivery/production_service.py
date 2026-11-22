"""Step 3.3：生产部署 ZIP 包组装与完整性门禁服务（Layer 6: delivery）。

严格守卫生产交付出口：
1. 校验 Run 已 COMPLETED、Oracle AB 审计全通过、覆盖完整、无 PENDING 规则；
2. 校验当前数据库 APPROVED 规则集哈希与审计通关快照 `active_rule_hashes` 严格一致，拦截事后篡改；
3. 校验已发布码表版本未漂移；
4. 同时输出 Oracle 23（01~05）与 MySQL 8（01~04）全套生产脚本、注释防换行注入、`manifest.json` 逐文件 SHA-256 校验和；
5. 按 `(run_id, 'PRODUCTION_BUNDLE', version)` 幂等落库，重复下载不写冗余产物行。
"""

import hashlib
import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from sqlalchemy.orm import Session

from app.catalog.code_sets.bindings import (
    code_set_compile_bindings,
    load_published_code_sets,
)
from app.delivery.production import (
    compile_production_mysql_ddl,
    compile_production_oracle_ddl,
    compile_production_rule_query,
)
from app.generation.core.binding import SchemaBinding
from app.generation.sql.code_set_sql import (
    compile_code_set_mysql_ddl,
    compile_code_set_mysql_upsert,
    compile_code_set_oracle_ddl,
    compile_code_set_oracle_insert,
    compile_code_set_oracle_merge,
    stable_code_set_table,
)
from app.generation.workflow import _latest_payload, _specs
from app.infra.blob_store import LocalBlobStore
from app.infra.contracts import RuleDefinition, canonical_json
from app.infra.db_tables import ArtifactModel, RuleModel, RunModel
from app.infra.errors import WorkflowError
from app.policy.regops_channels import resolve_event_tables
from app.policy.regops_plan import project

_NO_CODE_SETS_COMMENT = "-- No published code sets referenced by this run.\n"


def _sql_comment(text: Any) -> str:
    """清洗 SQL 单行注释中的换行符，防止注释逸出变成非法 SQL 语句。"""
    return " ".join(str(text or "").splitlines()).strip()


def _join_sql_blocks(blocks: List[str], empty_comment: str = _NO_CODE_SETS_COMMENT) -> str:
    if not blocks:
        return empty_comment
    return "\n\n".join(blocks) + "\n"


def build_production_bundle(session: Session, run: RunModel, blob_dir: str) -> Tuple[str, bytes]:
    state = project(session, run)
    if state.status != "COMPLETED" or not state.result_json or not state.result_json.get("audit_summary", {}).get("all_passed"):
        raise WorkflowError("Production export requires a completed, fully passed Oracle audit")
    if state.result_json.get("audit_summary", {}).get("coverage_complete") is False:
        raise WorkflowError("Production export is blocked by uncovered validation rules")
    pending = session.query(RuleModel).filter(RuleModel.run_id == run.id, RuleModel.status == "PENDING").count()
    if pending:
        raise WorkflowError("Production export is blocked by unapproved rules")

    rows = session.query(RuleModel).filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED").all()
    rules = [RuleDefinition.model_validate(row.definition_json) for row in rows]
    if not rules:
        raise WorkflowError("Production export requires at least one approved validation rule")
    audited_hashes = state.result_json.get("active_rule_hashes")
    if isinstance(audited_hashes, list):
        current_hashes = sorted(rule.rule_hash for rule in rules)
        if current_hashes != sorted(audited_hashes):
            raise WorkflowError(
                "Approved rules changed after the audited run; rerun the Oracle audit before production export"
            )

    blobs = LocalBlobStore(blob_dir)
    specs = _specs(session, run.spec_id)
    schema_payload = resolve_event_tables(_latest_payload(session, run.id, "SCHEMA_ASSIGNED"), blobs)
    schemas = {
        code: [SchemaBinding.model_validate(item) for item in items]
        for code, items in schema_payload.items()
    }
    expected_versions = state.result_json["audit_summary"].get("code_set_versions", {})
    code_sets, field_code_sets = load_published_code_sets(session, specs, expected_versions=expected_versions)
    if code_sets and "code_set_versions" not in state.result_json["audit_summary"]:
        raise WorkflowError("This run predates code set auditing; rerun the Oracle audit before production export")

    code_set_tables = {item["code"]: stable_code_set_table(item["code"]) for item in code_sets}
    rule_bindings = code_set_compile_bindings(field_code_sets, code_set_tables)

    oracle_code_set_ddl: List[str] = []
    oracle_code_set_insert: List[str] = []
    oracle_code_set_merge: List[str] = []
    mysql_code_set_ddl: List[str] = []
    mysql_code_set_upsert: List[str] = []
    for code_set in code_sets:
        table = code_set_tables[code_set["code"]]
        comment = (
            f"-- {_sql_comment(code_set['code'])} | {_sql_comment(code_set['name'])} "
            f"| audited version {code_set['version']}\n"
        )
        oracle_code_set_ddl.append(comment + compile_code_set_oracle_ddl(table) + ";")
        insert_sql = compile_code_set_oracle_insert(code_set, table)
        merge_sql = compile_code_set_oracle_merge(code_set, table)
        oracle_code_set_insert.append(comment + insert_sql + (";" if insert_sql else ""))
        oracle_code_set_merge.append(comment + merge_sql + (";" if merge_sql else ""))

        mysql_code_set_ddl.append(comment + compile_code_set_mysql_ddl(table) + ";")
        upsert_sql = compile_code_set_mysql_upsert(code_set, table)
        mysql_code_set_upsert.append(comment + upsert_sql + (";" if upsert_sql else ""))

    oracle_ddl = "\n\n".join(compile_production_oracle_ddl(spec.table_code, schemas[spec.table_code]) + ";" for spec in specs) + "\n"
    mysql_ddl = "\n\n".join(compile_production_mysql_ddl(spec.table_code, schemas[spec.table_code]) + ";" for spec in specs) + "\n"

    oracle_rules: List[str] = []
    mysql_rules: List[str] = []
    for rule in sorted(rules, key=lambda item: (item.table_code, item.rule_key)):
        header = (
            f"-- {_sql_comment(rule.rule_key)} | {_sql_comment(rule.source)} | {_sql_comment(rule.rule_hash)}\n"
            f"-- {_sql_comment(rule.rationale)}\n"
        )
        oracle_rules.append(header + compile_production_rule_query(rule, "oracle", code_set_bindings=rule_bindings) + ";")
        mysql_rules.append(header + compile_production_rule_query(rule, "mysql", code_set_bindings=rule_bindings) + ";")

    bundle_files: Dict[str, str] = {
        "oracle/01_report_tables.sql": oracle_ddl,
        "oracle/02_code_set_tables.sql": _join_sql_blocks(oracle_code_set_ddl),
        "oracle/03_code_values_insert.sql": _join_sql_blocks(oracle_code_set_insert),
        "oracle/04_code_values_merge.sql": _join_sql_blocks(oracle_code_set_merge),
        "oracle/05_validation_rules.sql": _join_sql_blocks(oracle_rules, "-- No approved rules.\n"),
        "mysql/01_report_tables.sql": mysql_ddl,
        "mysql/02_code_set_tables.sql": _join_sql_blocks(mysql_code_set_ddl),
        "mysql/03_code_values_upsert.sql": _join_sql_blocks(mysql_code_set_upsert),
        "mysql/04_validation_rules.sql": _join_sql_blocks(mysql_rules, "-- No approved rules.\n"),
        "rules.json": canonical_json([rule.model_dump() for rule in rules]),
    }
    file_checksums = {
        path: hashlib.sha256(body.encode("utf-8")).hexdigest() for path, body in sorted(bundle_files.items())
    }

    manifest = {
        "bundle_type": "PRODUCTION_DEPLOYMENT",
        "dialects": ["oracle", "mysql"],
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
        "file_checksums": file_checksums,
        "contains_test_data": False,
        "uses_run_specific_tables": False,
        "contains_synthetic_record_id": False,
        "deployment_order": [
            "Backup target schemas",
            "Review manifest.json, file_checksums, and rules.json",
            "Oracle 23: run oracle/01_report_tables.sql -> oracle/02_code_set_tables.sql -> oracle/03_code_values_insert.sql (initial) or oracle/04_code_values_merge.sql (repeatable) -> oracle/05_validation_rules.sql",
            "MySQL 8: run mysql/01_report_tables.sql -> mysql/02_code_set_tables.sql -> mysql/03_code_values_upsert.sql -> mysql/04_validation_rules.sql",
        ],
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, body in bundle_files.items():
            archive.writestr(path, body)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    content = buffer.getvalue()
    uri = blobs.put(content)

    # 按 (run_id, 'PRODUCTION_BUNDLE', version) 幂等更新，防止重复点击下载堆积冗余产物行
    existing_artifact = (
        session.query(ArtifactModel)
        .filter(
            ArtifactModel.run_id == run.id,
            ArtifactModel.artifact_type == "PRODUCTION_BUNDLE",
            ArtifactModel.version == state.attempt,
        )
        .first()
    )
    if existing_artifact is not None:
        existing_artifact.blob_uri = uri
        existing_artifact.sha256 = uri.removeprefix("blob://")
    else:
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
