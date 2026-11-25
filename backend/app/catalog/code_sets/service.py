"""码表抽取、校验、摘要查询与删除服务（Catalog Code Set Service）。

职责边界：
- 摘要视图：`code_set_summary` / `code_set_summaries`；
- 单块码表抽取与落库：`extract_code_sets_for_block` / `_upsert_code_set_draft`；
- 质量与 AI 复核记录：`_record_code_set_validation`；
- 码表删除：`remove_code_set`；
- 同时聚合导出 `parser.py` 与 `linking.py` 的常用接口，作为码表子域统一服务入口。
（注：跨子域的表+码表双轨并发抽取协同已提升至顶层 `app.catalog.extraction`。）
"""

import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog.code_sets.linking import (
    _compact_code_set_name,
    _fill_code_values,
    _find_matching_code_set,
    _reference_name_variants,
    _set_has_values,
    _stamp_field_ordinal,
    _values_overlap,
    auto_link_all_references,
    document_code_set_ids,
    document_code_set_ordinals,
    ensure_field_reference,
    ensure_table_references,
    generation_blockers,
    refresh_reference_status,
)
from app.catalog.code_sets.parser import (
    CIRCLED_INDEX,
    CIRCLED_NUM,
    CODE_NAME_SYNONYMS,
    ENUM_NOISE_RE,
    FIXED_VALUE_HINT_RE,
    CodeSetDraft,
    CodeValueDraft,
    _has_error_issues,
    _parse_bare_semicolon_list,
    _parse_enum_list,
    _parse_enum_pair,
    _split_enum_fragments,
    is_conditional_phrase,
    is_rule_reference,
    normalize_code_set_name,
    parse_fixed_value,
    parse_inline_enum,
    validate_code_set_draft,
)
from app.infra.errors import ProviderAuthOrBillingError
from app.infra.llm import RealDesignLlm
from app.kernel.run import BudgetExceededError, RunCancelledError
from app.infra.llm.guard import call_llm_validated
from app.infra.db_tables import (
    CodeSetModel,
    CodeSetValidationModel,
    CodeValueModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    RawBlockModel,
    TableMetadataModel,
)
from app.infra.config import Settings
from app.infra.db import SessionLocal
from app.ingestion.parsers.pdf_catalog import extract_code_pairs_from_section


def code_set_summary(session: Session, row: CodeSetModel, metrics: Optional[Tuple[int, int, int]] = None) -> Dict[str, Any]:
    if metrics is None:
        value_count = session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id).count()
        enabled_count = session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id, CodeValueModel.enabled.is_(True)).count()
        unresolved = session.query(FieldCodeReferenceModel).filter(
            FieldCodeReferenceModel.code_set_id == row.id,
            FieldCodeReferenceModel.status == "UNRESOLVED",
        ).count()
    else:
        value_count, enabled_count, unresolved = metrics
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "definition": row.definition,
        "source_type": row.source_type,
        "status": row.status,
        "current_version": row.current_version,
        "published_version": row.published_version,
        "value_count": value_count,
        # 和 evaluate_code_set 对齐：占位判断要看「一条码值都没有」，
        # 而不是「没有启用的」。同名不同义是最难查的一类 bug。
        "total_value_count": value_count,
        "enabled_value_count": enabled_count,
        "unresolved_reference_count": unresolved,
    }


def code_set_summaries(session: Session, rows: List[CodeSetModel]) -> List[Dict[str, Any]]:
    """列表页批量统计码值数和未决引用，按 500 条分片避免 Oracle ORA-01795（IN 列表超 1000 项）。"""
    if not rows:
        return []
    ids = [row.id for row in rows]
    value_metrics: Dict[str, Tuple[int, int]] = {}
    unresolved_metrics: Dict[str, int] = {}
    for idx in range(0, len(ids), 500):
        chunk = ids[idx : idx + 500]
        value_rows = (
            session.query(
                CodeValueModel.code_set_id,
                func.count(CodeValueModel.id),
                func.sum(case((CodeValueModel.enabled.is_(True), 1), else_=0)),
            )
            .filter(CodeValueModel.code_set_id.in_(chunk))
            .group_by(CodeValueModel.code_set_id)
            .all()
        )
        for code_set_id, total, enabled in value_rows:
            value_metrics[code_set_id] = (int(total or 0), int(enabled or 0))
        unresolved_rows = (
            session.query(FieldCodeReferenceModel.code_set_id, func.count(FieldCodeReferenceModel.id))
            .filter(
                FieldCodeReferenceModel.code_set_id.in_(chunk),
                FieldCodeReferenceModel.status == "UNRESOLVED",
            )
            .group_by(FieldCodeReferenceModel.code_set_id)
            .all()
        )
        for code_set_id, total in unresolved_rows:
            unresolved_metrics[code_set_id] = int(total or 0)
    return [
        code_set_summary(
            session,
            row,
            (
                value_metrics.get(row.id, (0, 0))[0],
                value_metrics.get(row.id, (0, 0))[1],
                unresolved_metrics.get(row.id, 0),
            ),
        )
        for row in rows
    ]


def _clean_value_drafts(raw_values: List[Dict[str, Any]]) -> List[CodeValueDraft]:
    cleaned = []
    for v in raw_values:
        val = str(v.get("value") or "").strip()
        lbl = str(v.get("label_cn") or "").strip()
        if not val or not lbl:
            continue
        cleaned.append(
            CodeValueDraft(
                value=val,
                label_cn=lbl,
                label_en=str(v.get("label_en") or "").strip(),
                definition=str(v.get("definition") or "").strip(),
            )
        )
    return cleaned


def _upsert_code_set_draft(
    session: Session,
    draft: CodeSetDraft,
    locator: Dict[str, Any],
) -> Tuple[CodeSetModel, bool]:
    """幂等写入或补充码表草稿：使用数据库原生 SAVEPOINT 兜底并发唯一键竞争，彻底避免进程内锁与 DB 写锁形成 AB-BA 死锁。"""
    safe_code = draft.code.upper()[:96]
    safe_name = draft.name[:255]
    existing = (
        session.query(CodeSetModel)
        .filter(CodeSetModel.code == safe_code)
        .order_by(CodeSetModel.current_version.desc())
        .first()
    )
    if not existing:
        existing = session.query(CodeSetModel).filter(CodeSetModel.name == safe_name).first()
    changed = False
    if existing:
        row = existing
        if draft.definition and (not row.definition or row.source_type == "DOCUMENT_REFERENCE") and row.definition != draft.definition:
            row.definition = draft.definition
            changed = True
        if row.source_type == "DOCUMENT_REFERENCE" and row.source_type != draft.source_type:
            row.source_type = draft.source_type
            changed = True
    else:
        has_errors = any(issue["severity"] == "ERROR" for issue in validate_code_set_draft(draft))
        row = CodeSetModel(
            id=str(uuid.uuid4()),
            code=safe_code,
            name=safe_name,
            definition=draft.definition,
            source_type=draft.source_type,
            status="NEEDS_REVIEW" if has_errors else ("AI_SELF_VALIDATED" if draft.values else "DRAFT"),
            current_version=1,
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            existing = (
                session.query(CodeSetModel)
                .filter(CodeSetModel.code == safe_code)
                .order_by(CodeSetModel.current_version.desc())
                .first()
            )
            if not existing:
                raise
            row = existing

    # 记下码表在原文里的位置，左侧列表才能按出现顺序排（×1000 给同块内留出插入空间）
    if row.source_ordinal is None and locator.get("block_id"):
        source_block = session.get(RawBlockModel, locator["block_id"])
        if source_block is not None:
            row.source_ordinal = source_block.ordinal * 1000
    # 按大小写不敏感索引已有码值，防止 MySQL 8 utf8mb4_0900_ai_ci 唯一键 uq_code_value 撞车
    existing_vals = {
        (item.value or "").strip().upper(): item
        for item in session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id).all()
    }
    sort_order = len(existing_vals)
    for item in draft.values:
        code_val = item.value.strip()[:128]
        if not code_val:
            continue
        code_val_key = code_val.upper()
        if code_val_key in existing_vals:
            cur = existing_vals[code_val_key]
            if item.label_cn and not cur.label_cn:
                cur.label_cn = item.label_cn[:255]
                changed = True
            if item.label_en and not cur.label_en:
                cur.label_en = item.label_en[:255]
                changed = True
            if item.definition and not cur.definition:
                cur.definition = item.definition
                changed = True
        else:
            new_val = CodeValueModel(
                id=str(uuid.uuid4()),
                code_set_id=row.id,
                value=code_val,
                label_cn=item.label_cn.strip()[:255],
                label_en=(item.label_en or "").strip()[:255],
                definition=(item.definition or "").strip(),
                sort_order=sort_order,
                enabled=True,
                source_locator=locator,
            )
            try:
                with session.begin_nested():
                    session.add(new_val)
                    session.flush()
                existing_vals[code_val_key] = new_val
                sort_order += 1
                changed = True
            except IntegrityError:
                # 并发写入同一码值时跳过重复插入，回读已有记录
                existing_val = (
                    session.query(CodeValueModel)
                    .filter(CodeValueModel.code_set_id == row.id, func.upper(CodeValueModel.value) == code_val_key)
                    .first()
                )
                if existing_val is not None:
                    existing_vals[code_val_key] = existing_val

    if not existing:
        return row, True
    has_errors = any(issue["severity"] == "ERROR" for issue in validate_code_set_draft(draft))
    target_status = "NEEDS_REVIEW" if has_errors else ("AI_SELF_VALIDATED" if existing_vals else "DRAFT")
    if row.status != "PUBLISHED" and row.status != target_status:
        row.status = target_status
        changed = True
    if changed:
        row.current_version += 1
    return row, changed


def enforce_code_set_review_evidence(
    issues: List[Dict[str, Any]],
    draft: CodeSetDraft,
    raw_content: str,
) -> List[Dict[str, Any]]:
    """码表 AI 复核防幻觉护栏：校验 AI 报出的 ERROR，杜绝虚构码值或截断误判把干净码表整表标红。"""
    known_values = {(item.value or "").strip().upper() for item in draft.values}
    compact_source = re.sub(r"\s+", "", raw_content or "").upper()
    truncated = len(raw_content or "") > 6000
    result: List[Dict[str, Any]] = []
    for issue in issues:
        item = dict(issue)
        code = str(item.get("code") or "AI_REVIEW")
        severity = str(item.get("severity") or "WARNING").upper()
        val = str(item.get("value") or "").strip()
        msg = str(item.get("message") or "")
        reason = ""
        if severity == "ERROR":
            is_missing_issue = any(kw in code.upper() for kw in ("MISSING", "OMIT", "INCOMPLETE"))
            if val and not is_missing_issue:
                val_up = val.upper()
                if val_up not in known_values and val_up not in compact_source:
                    reason = "所引码值既不在当前码表也不在来源原文中"
            elif truncated and is_missing_issue:
                reason = "来源原文超过 6000 字已截断，不能仅凭前段判定尾部遗漏"
        if reason:
            severity = "WARNING"
            code = f"UNVERIFIED_{code}"[:64]
            msg = f"{msg}（{reason}，已降级为提示）"
        item["severity"] = severity
        item["code"] = code
        item["message"] = msg
        result.append(item)
    return result


def _record_code_set_validation(
    session: Session,
    row: CodeSetModel,
    draft: CodeSetDraft,
    block: RawBlockModel,
    settings: Settings,
    content_changed: bool,
    review_with_ai: Any = "adaptive",
    extra_program_issues: Optional[List[Dict[str, Any]]] = None,
) -> None:
    program_issues = validate_code_set_draft(draft) + list(extra_program_issues or [])
    session.add(
        CodeSetValidationModel(
            id=str(uuid.uuid4()),
            code_set_id=row.id,
            validator_kind="PROGRAM",
            status="NEEDS_REVIEW" if _has_error_issues(program_issues) else ("WARNING" if program_issues else "PASSED"),
            issues_json=program_issues,
            trace_json={"value_count": len(draft.values)},
        )
    )
    if row.status == "PUBLISHED":
        return
    if review_with_ai in {False, "never"}:
        row.status = "NEEDS_REVIEW" if _has_error_issues(program_issues) else "PROGRAM_VALIDATED"
        return

    # adaptive 模式：对齐 metadata_service，仅对 AI 抽取或程序检查有告警的码表做 AI 复核；
    # 规整表格提取且无告警（或超大码表会被 6000 字 raw_context 截断）走确定性交叉校验即可。
    if review_with_ai == "adaptive":
        needs_ai_review = (draft.source_type == "AI_EXTRACTED" or bool(program_issues)) and len(draft.values) <= 120
        if not needs_ai_review:
            session.add(
                CodeSetValidationModel(
                    id=str(uuid.uuid4()),
                    code_set_id=row.id,
                    validator_kind="AI_REVIEW",
                    status="PASSED",
                    issues_json=[],
                    trace_json={"strategy": "DETERMINISTIC_CROSS_CHECK", "value_count": len(draft.values)},
                )
            )
            row.status = "NEEDS_REVIEW" if _has_error_issues(program_issues) else "AI_SELF_VALIDATED"
            return

    llm = None
    try:
        llm = RealDesignLlm(settings)
    except Exception:
        pass

    existing_review = session.query(CodeSetValidationModel).filter(
        CodeSetValidationModel.code_set_id == row.id,
        CodeSetValidationModel.validator_kind == "AI_REVIEW",
    ).first()
    if llm is None:
        row.status = "NEEDS_REVIEW" if _has_error_issues(program_issues) else "PROGRAM_VALIDATED"
        return
    if not content_changed and existing_review:
        return

    try:
        args, trace = call_llm_validated(
            llm,
            "review_code_set",
            "独立复核码值表。仅输出码值遗漏、重复、含义错配、无证据码值或定义不一致的问题；无问题返回空数组。",
            {
                "source_locator": block.source_locator,
                "code_set": draft.model_dump(),
                "raw_context": block.raw_content[:6000],
            },
            timeout=35,
            backoff=(1.0,),
            transient_attempts=2,
            validation_attempts=2,
        )
        issues = args.get("issues", [])
    except (BudgetExceededError, RunCancelledError):
        raise
    except Exception as exc:
        trace = {"error": f"{type(exc).__name__}: {exc}"}
        issues = [{
            "severity": "WARNING",
            "code": "AI_REVIEW_UNAVAILABLE",
            "value": "",
            "message": f"AI 复核不可用：{trace['error']}",
        }]
    issues = enforce_code_set_review_evidence(issues, draft, block.raw_content)
    session.add(
        CodeSetValidationModel(
            id=str(uuid.uuid4()),
            code_set_id=row.id,
            validator_kind="AI_REVIEW",
            status="NEEDS_REVIEW" if _has_error_issues(issues) else "PASSED",
            issues_json=issues,
            trace_json=trace,
        )
    )
    # AI 复核不可用时 issues 里塞的是 WARNING 级的 AI_REVIEW_UNAVAILABLE——
    # 那是服务没跑成，不是数据有问题。整表标红成「需要核对」会让 100 张
    # 本来没问题的码表挂在那里没人该看。只有 ERROR 级的真问题才拦。
    row.status = "NEEDS_REVIEW" if _has_error_issues(program_issues) or _has_error_issues(issues) else "AI_SELF_VALIDATED"


def preload_existing_code_sets_state(session: Session) -> Dict[str, Dict[str, Any]]:
    """预加载已有码表的校验状态与码值集合，供无 DB Worker 判断是否可跳过重复 AI 复核。"""
    rows = session.query(CodeSetModel).all()
    if not rows:
        return {}
    reviewed_ids = {
        cs_id
        for (cs_id,) in session.query(CodeSetValidationModel.code_set_id)
        .filter(CodeSetValidationModel.validator_kind == "AI_REVIEW")
        .all()
    }
    vals_by_cs: Dict[str, set] = {}
    for cs_id, val in session.query(CodeValueModel.code_set_id, CodeValueModel.value).all():
        vals_by_cs.setdefault(cs_id, set()).add((val or "").strip().upper())

    state: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        state[r.code.upper()] = {
            "id": r.id,
            "name": r.name,
            "definition": r.definition or "",
            "source_type": r.source_type,
            "has_ai_review": r.id in reviewed_ids,
            "values": vals_by_cs.get(r.id, set()),
        }
    return state


def _compute_single_code_set_review(
    draft: CodeSetDraft,
    raw_content: str,
    source_locator: Dict[str, Any],
    settings: Settings,
    *,
    review_with_ai: Any = "adaptive",
    extra_program_issues: Optional[List[Dict[str, Any]]] = None,
    existing_state: Optional[Dict[str, Dict[str, Any]]] = None,
    llm: Any = None,
    bus: Any = None,
) -> Dict[str, Any]:
    program_issues = validate_code_set_draft(draft) + list(extra_program_issues or [])
    if review_with_ai in {False, "never"}:
        return {
            "program_issues": program_issues,
            "review_kind": "NONE",
            "ai_issues": [],
            "ai_trace": None,
        }
    if review_with_ai == "adaptive":
        needs_ai_review = (draft.source_type == "AI_EXTRACTED" or bool(program_issues)) and len(draft.values) <= 120
        if not needs_ai_review:
            return {
                "program_issues": program_issues,
                "review_kind": "DETERMINISTIC",
                "ai_issues": [],
                "ai_trace": {"strategy": "DETERMINISTIC_CROSS_CHECK", "value_count": len(draft.values)},
            }

    active_llm = llm
    if active_llm is None:
        try:
            active_llm = RealDesignLlm(settings)
        except Exception:
            active_llm = None
    if active_llm is None:
        return {
            "program_issues": program_issues,
            "review_kind": "NO_LLM",
            "ai_issues": [],
            "ai_trace": None,
        }

    safe_code = draft.code.upper()[:96]
    prev = (existing_state or {}).get(safe_code)
    if prev and prev.get("has_ai_review"):
        incoming_keys = {(item.value or "").strip().upper() for item in draft.values if item.value.strip()}
        if incoming_keys <= prev.get("values", set()) and not (
            draft.definition and (not prev.get("definition") or prev.get("source_type") == "DOCUMENT_REFERENCE") and prev.get("definition") != draft.definition
        ):
            return {
                "program_issues": program_issues,
                "review_kind": "SKIPPED_UNCHANGED",
                "ai_issues": [],
                "ai_trace": None,
            }

    try:
        args, trace = call_llm_validated(
            active_llm,
            "review_code_set",
            "独立复核码值表。仅输出码值遗漏、重复、含义错配、无证据码值或定义不一致的问题；无问题返回空数组。",
            {
                "source_locator": source_locator,
                "code_set": draft.model_dump(),
                "raw_context": raw_content[:6000],
            },
            timeout=35,
            backoff=(1.0,),
            bus=bus,
            transient_attempts=2,
            validation_attempts=2,
        )
        issues = args.get("issues", [])
    except (BudgetExceededError, RunCancelledError, ProviderAuthOrBillingError):
        raise
    except Exception as exc:
        trace = {"error": f"{type(exc).__name__}: {exc}"}
        issues = [{
            "severity": "WARNING",
            "code": "AI_REVIEW_UNAVAILABLE",
            "value": "",
            "message": f"AI 复核不可用：{trace['error']}",
        }]
    issues = enforce_code_set_review_evidence(issues, draft, raw_content)
    return {
        "program_issues": program_issues,
        "review_kind": "AI",
        "ai_issues": issues,
        "ai_trace": trace,
    }


def compute_code_sets_for_block_snapshot(
    block_snap: Dict[str, Any],
    settings: Settings,
    *,
    review_with_ai: Any = "adaptive",
    existing_state: Optional[Dict[str, Dict[str, Any]]] = None,
    llm: Any = None,
    bus: Any = None,
) -> List[Dict[str, Any]]:
    """纯计算/HTTP 码表提取 Worker：零 SQLAlchemy 访问，返回待单写落库的码表草案清单。"""
    block_id = str(block_snap["id"])
    ordinal = int(block_snap.get("ordinal") or 0)
    block_type = str(block_snap.get("block_type") or "")
    raw_content = str(block_snap.get("raw_content") or "")
    source_locator = dict(block_snap.get("source_locator") or {})
    content = dict(block_snap.get("content_json") or {})

    results: List[Dict[str, Any]] = []

    cs_spec = content.get("code_set_spec")
    if cs_spec and cs_spec.get("values"):
        valid_vals = _clean_value_drafts(cs_spec["values"])
        if valid_vals:
            draft = CodeSetDraft(
                code=cs_spec["code"],
                name=cs_spec["name"],
                definition=cs_spec.get("definition", ""),
                source_type=cs_spec.get("source_type", "DOCUMENT_TABLE"),
                values=valid_vals,
            )
            locator = {**source_locator, "block_id": block_id}
            review = _compute_single_code_set_review(
                draft,
                raw_content,
                source_locator,
                settings,
                review_with_ai=review_with_ai,
                existing_state=existing_state,
                llm=llm,
                bus=bus,
            )
            results.append({"draft": draft, "locator": locator, **review})

    inline_list = content.get("inline_code_sets") or []
    for inline_cs in inline_list:
        valid_vals = _clean_value_drafts(inline_cs.get("values") or [])
        if not valid_vals:
            continue
        draft = CodeSetDraft(
            code=inline_cs["code"],
            name=inline_cs["name"],
            definition=inline_cs.get("definition", ""),
            source_type="INLINE_ENUM",
            values=valid_vals,
        )
        locator = {
            **source_locator,
            "block_id": block_id,
            "field_en": inline_cs.get("field_en", ""),
            "page": inline_cs.get("page", ""),
        }
        review = _compute_single_code_set_review(
            draft,
            raw_content,
            source_locator,
            settings,
            review_with_ai=review_with_ai,
            existing_state=existing_state,
            llm=llm,
            bus=bus,
        )
        results.append({"draft": draft, "locator": locator, **review})

    if not results and (block_type in {"CODE_SET", "CODE_SET_CANDIDATE"} or content.get("candidate_role") == "CODE_SET"):
        title = content.get("table_name") or source_locator.get("title") or source_locator.get("section") or f"码值表_{ordinal + 1}"
        code_id = content.get("table_code") or re.sub(r"[^A-Z0-9_]", "_", f"CS_{block_id[:8]}".upper())
        pairs = extract_code_pairs_from_section(raw_content, str(source_locator.get("section", "")), title)
        valid_vals = _clean_value_drafts(pairs)
        extra_warnings: List[Dict[str, Any]] = []
        if not valid_vals:
            if len(raw_content) > 6000:
                extra_warnings.append(
                    {
                        "severity": "WARNING",
                        "code": "RAW_CONTENT_TRUNCATED",
                        "value": "",
                        "message": f"原文长 {len(raw_content)} 字，AI 兜底抽取仅读取前 6000 字，请核对尾部码值是否完整",
                    }
                )
            try:
                active_llm = llm if llm is not None else RealDesignLlm(settings)
                args, _ = call_llm_validated(
                    active_llm,
                    "assign_code_set",
                    "从输入块中提取码值表代码、中文名、定义及全部码值项（value、label_cn、label_en、definition），不得虚构。",
                    {"source_locator": source_locator, "raw_content": raw_content[:6000]},
                    timeout=35,
                    backoff=(1.0,),
                    bus=bus,
                    transient_attempts=2,
                    validation_attempts=2,
                )
                valid_vals = _clean_value_drafts(args.get("values", []))
                draft = CodeSetDraft(
                    code=args.get("code") or code_id,
                    name=args.get("name") or title,
                    definition=args.get("definition", ""),
                    source_type="AI_EXTRACTED",
                    values=valid_vals,
                )
            except (BudgetExceededError, RunCancelledError, ProviderAuthOrBillingError):
                raise
            except Exception:
                draft = CodeSetDraft(code=code_id, name=title, definition="待补充码值项", source_type="DOCUMENT_TABLE", values=[])
        else:
            draft = CodeSetDraft(
                code=code_id,
                name=title,
                definition=f"从文档块 {title} 提取",
                source_type="DOCUMENT_TABLE",
                values=valid_vals,
            )
        if draft.values:
            locator = {**source_locator, "block_id": block_id}
            review = _compute_single_code_set_review(
                draft,
                raw_content,
                source_locator,
                settings,
                review_with_ai=review_with_ai,
                extra_program_issues=extra_warnings,
                existing_state=existing_state,
                llm=llm,
                bus=bus,
            )
            results.append({"draft": draft, "locator": locator, **review})

    return results


def persist_computed_code_sets(
    session: Session,
    computed_items: List[Dict[str, Any]],
    *,
    auto_link: bool = False,
) -> List[str]:
    """在主协调线程单写落库已计算的码表草案及校验记录。"""
    created_ids: List[str] = []
    for item in computed_items:
        draft: CodeSetDraft = item["draft"]
        locator = item["locator"]
        program_issues = item["program_issues"]
        review_kind = item["review_kind"]
        row, changed = _upsert_code_set_draft(session, draft, locator)
        session.add(
            CodeSetValidationModel(
                id=str(uuid.uuid4()),
                code_set_id=row.id,
                validator_kind="PROGRAM",
                status="NEEDS_REVIEW" if _has_error_issues(program_issues) else ("WARNING" if program_issues else "PASSED"),
                issues_json=program_issues,
                trace_json={"value_count": len(draft.values)},
            )
        )
        if row.status != "PUBLISHED":
            if review_kind == "NONE":
                row.status = "NEEDS_REVIEW" if _has_error_issues(program_issues) else "PROGRAM_VALIDATED"
            elif review_kind == "DETERMINISTIC":
                session.add(
                    CodeSetValidationModel(
                        id=str(uuid.uuid4()),
                        code_set_id=row.id,
                        validator_kind="AI_REVIEW",
                        status="PASSED",
                        issues_json=[],
                        trace_json=item["ai_trace"],
                    )
                )
                row.status = "NEEDS_REVIEW" if _has_error_issues(program_issues) else "AI_SELF_VALIDATED"
            elif review_kind == "NO_LLM":
                row.status = "NEEDS_REVIEW" if _has_error_issues(program_issues) else "PROGRAM_VALIDATED"
            elif review_kind == "SKIPPED_UNCHANGED":
                existing_review = session.query(CodeSetValidationModel).filter(
                    CodeSetValidationModel.code_set_id == row.id,
                    CodeSetValidationModel.validator_kind == "AI_REVIEW",
                ).first()
                if changed or not existing_review:
                    row.status = "NEEDS_REVIEW" if _has_error_issues(program_issues) else "AI_SELF_VALIDATED"
            elif review_kind == "AI":
                ai_issues = item["ai_issues"]
                existing_review = session.query(CodeSetValidationModel).filter(
                    CodeSetValidationModel.code_set_id == row.id,
                    CodeSetValidationModel.validator_kind == "AI_REVIEW",
                ).first()
                if changed or not existing_review:
                    session.add(
                        CodeSetValidationModel(
                            id=str(uuid.uuid4()),
                            code_set_id=row.id,
                            validator_kind="AI_REVIEW",
                            status="NEEDS_REVIEW" if _has_error_issues(ai_issues) else "PASSED",
                            issues_json=ai_issues,
                            trace_json=item["ai_trace"],
                        )
                    )
                    row.status = "NEEDS_REVIEW" if _has_error_issues(program_issues) or _has_error_issues(ai_issues) else "AI_SELF_VALIDATED"
        created_ids.append(row.id)
    if auto_link:
        auto_link_all_references(session)
    session.flush()
    return created_ids


def extract_code_sets_for_block(block_id: str, settings: Settings, auto_link: bool = True, review_with_ai: Any = "adaptive") -> List[str]:
    """Extract standalone CodeSets or inline field enum CodeSets from a RawBlockModel (Track B)."""
    with SessionLocal() as session:
        block = session.get(RawBlockModel, block_id)
        if not block:
            raise ValueError("Raw block not found")
        existing_state = preload_existing_code_sets_state(session)
        snap = {
            "id": block.id,
            "document_id": block.document_id,
            "ordinal": int(block.ordinal or 0),
            "block_type": block.block_type,
            "raw_content": block.raw_content or "",
            "raw_hash": block.raw_hash or "",
            "source_locator": dict(block.source_locator or {}),
            "content_json": dict(block.content_json or {}),
        }
        computed_items = compute_code_sets_for_block_snapshot(
            snap,
            settings,
            review_with_ai=review_with_ai,
            existing_state=existing_state,
        )
        created_ids = persist_computed_code_sets(session, computed_items, auto_link=auto_link)
        session.commit()
        return created_ids


def remove_code_set(session: Session, row) -> Dict[str, Any]:
    """删掉一张码表及其码值。

    被字段引用着的不能直接删——那会让字段的码值引用悬空，
    后面 readiness 报 UNRESOLVED 时已经晚了。要先解除引用。
    """
    from app.infra.db_tables import CodeValueModel, FieldCodeReferenceModel

    reference_count = (
        session.query(FieldCodeReferenceModel)
        .filter(FieldCodeReferenceModel.code_set_id == row.id)
        .count()
    )
    if reference_count:
        raise ValueError(
            f"码表「{row.code}」被 {reference_count} 个字段引用着，"
            "删掉会让这些字段的码值引用悬空。先处理引用再删。"
        )
    value_count = session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id).count()
    # 校验记录挂在 code_set_id 外键上，不先删会在 MySQL 8 报 1451 / Oracle 23 报 ORA-02292
    session.query(CodeSetValidationModel).filter(CodeSetValidationModel.code_set_id == row.id).delete(
        synchronize_session=False
    )
    session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id).delete(synchronize_session=False)
    code = row.code
    code_set_id = row.id
    session.delete(row)
    session.flush()
    return {"deleted": code_set_id, "code": code, "value_count": value_count}
