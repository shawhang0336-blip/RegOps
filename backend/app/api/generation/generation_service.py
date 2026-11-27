"""发起规则生成前的两遍预检、自动定稿发布与元数据快照冻结（供 `api/generation/generation.py` 调用）。"""

import hashlib
import uuid
from typing import Any, Dict, List, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.catalog.code_sets.linking import generation_blockers
from app.catalog.code_sets.readiness import auto_publish_referenced_code_sets
from app.catalog.tables.readiness import evaluate_table, split_preflight_issues
from app.catalog.tables.service import metadata_to_spec, publish_table
from app.delivery.workflow import _create_run
from app.infra.config import get_budget
from app.infra.contracts import canonical_json
from app.infra.db_tables import RunModel, SpecPackageModel, SpecificationModel, TableMetadataModel


def prepare_generation_run(
    session: Session,
    *,
    table_ids: List[str],
    approver: str,
    accept_warnings: bool,
    settings,
) -> Tuple[RunModel, Dict[str, int], Dict[str, List[Dict[str, Any]]], List[str]]:
    """两遍预检：去重并拦截 MERGED → 自动定稿并发布关联码表 → 校验就绪度 → 冻结元数据快照并创建 Run。"""
    unique_ids = list(dict.fromkeys(tid for tid in table_ids if tid))
    if not unique_ids:
        raise HTTPException(status_code=422, detail="Select at least one published table")
    tables = [session.get(TableMetadataModel, table_id) for table_id in unique_ids]
    if any(not table for table in tables):
        raise HTTPException(status_code=404, detail="Metadata table not found")

    specs = []
    pending_warnings: List[Dict[str, Any]] = []
    auto_published: Dict[str, List[Dict[str, Any]]] = {}
    auto_finalized: List[str] = []

    for table in tables:
        if table.status == "MERGED":
            raise HTTPException(
                status_code=409,
                detail={
                    "stage": "metadata",
                    "table_code": table.table_code,
                    "message": f"{table.table_code} 已被合并（MERGED），请改选合并后的目标表。",
                },
            )
        if table.status == "NEEDS_REVIEW":
            raise HTTPException(
                status_code=409,
                detail={
                    "stage": "metadata",
                    "table_code": table.table_code,
                    "message": (
                        f"{table.table_code} 还有违反原文的字段问题没处理完，"
                        "先到元数据页把标红的问题解决掉再选它。"
                    ),
                },
            )
        if not (table.status == "PUBLISHED" and table.published_version == table.current_version):
            try:
                finalized = publish_table(session, table, approver, table.current_version)
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"stage": "ddl", "table_code": table.table_code, "message": str(exc)},
                ) from exc
            auto_finalized.append(table.table_code)
            if finalized.get("published"):
                auto_published[table.table_code] = finalized["published"]
        try:
            specs.append(metadata_to_spec(session, table))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=f"{table.table_code}: {exc}") from exc
        auto = auto_publish_referenced_code_sets(session, table)
        if auto["published"]:
            auto_published[table.table_code] = auto["published"]

    if auto_published or auto_finalized:
        session.commit()

    for table in tables:
        readiness = evaluate_table(session, table)
        if not readiness["ddl"]["ready"]:
            blocker = readiness["ddl"]["blockers"][0]
            raise HTTPException(
                status_code=409,
                detail={
                    "table_code": table.table_code,
                    "stage": "ddl",
                    "blockers": readiness["ddl"]["blockers"],
                    "message": blocker["message"],
                },
            )
        issues = split_preflight_issues(readiness)
        hard_blockers = issues["hard"]
        if hard_blockers:
            raise HTTPException(
                status_code=409,
                detail={
                    "stage": "code_sets",
                    "table_code": table.table_code,
                    "blockers": hard_blockers,
                    "message": (
                        "码值未就绪，阶段 2 无法装配："
                        + hard_blockers[0]["message"]
                        + "。请先在码表页补齐缺失码值后再选这张表（能自动发布的已经替你发了）。"
                    ),
                },
            )
        soft_warnings = issues["soft"]
        if soft_warnings:
            pending_warnings.append({"table_code": table.table_code, "blockers": soft_warnings})
        table_blockers = generation_blockers(session, table)
        if table_blockers:
            raise HTTPException(status_code=409, detail={"table_code": table.table_code, "blockers": table_blockers})

    if pending_warnings and not accept_warnings:
        raise HTTPException(
            status_code=409,
            detail={
                "stage": "preflight",
                "requires_ack": True,
                "message": "有表的规则或码值未就绪，继续推进会导致规则缺失或阶段 2 直接报错",
                "tables": pending_warnings,
            },
        )

    pkg_ids = list(dict.fromkeys(table.package_id for table in tables if table.package_id))
    primary_pkg = session.get(SpecPackageModel, pkg_ids[0]) if pkg_ids else None
    snapshot_material = [
        {"table_id": table.id, "version": table.published_version, "spec_sha256": spec.source_sha256}
        for table, spec in zip(tables, specs)
    ]
    digest = hashlib.sha256(canonical_json(snapshot_material).encode()).hexdigest()
    snapshot = SpecificationModel(
        id=str(uuid.uuid4()),
        version="METADATA_PUBLISHED",
        table_code="METADATA_SNAPSHOT",
        source_name="published-metadata",
        source_sha256=digest,
        content_json={
            "selection_confirmed": True,
            "package_id": pkg_ids[0] if len(pkg_ids) == 1 else (pkg_ids[0] if pkg_ids else None),
            "package_ids": pkg_ids,
            "package_name": primary_pkg.name if primary_pkg else None,
            "regulator": primary_pkg.regulator if primary_pkg else None,
            "table_name": f"已发布元数据 · {len(specs)} 张表",
            "confirmation": {"approver": approver, "tables": snapshot_material},
            "tables": [spec.model_dump() for spec in specs],
        },
    )
    session.add(snapshot)
    session.flush()
    run = _create_run(session, snapshot.id, settings, operator=approver.strip())
    budget = get_budget(session)
    session.commit()
    return run, budget, auto_published, auto_finalized
