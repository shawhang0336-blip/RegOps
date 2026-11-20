from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db import Base


class DocumentArchitectureModel(Base):
    __tablename__ = "document_architectures"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    plan_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    warnings_json: Mapped[list] = mapped_column(JSON)
    trace_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SystemSettingModel(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    value_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    secret_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SourceDocumentModel(Base):
    __tablename__ = "source_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    file_name: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(16))
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    blob_uri: Mapped[str] = mapped_column(String(255))
    spec_version: Mapped[str] = mapped_column(String(64), default="UNKNOWN")
    status: Mapped[str] = mapped_column(String(32), index=True)
    block_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RawBlockModel(Base):
    __tablename__ = "raw_blocks"
    __table_args__ = (UniqueConstraint("document_id", "ordinal", name="uq_raw_block_order"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    block_type: Mapped[str] = mapped_column(String(32))
    raw_content: Mapped[str] = mapped_column(Text)
    source_locator: Mapped[Dict[str, Any]] = mapped_column(JSON)
    content_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    raw_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TableMetadataModel(Base):
    __tablename__ = "table_metadata"
    __table_args__ = (UniqueConstraint("block_id", "current_version", name="uq_table_block_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id"), index=True)
    block_id: Mapped[str] = mapped_column(ForeignKey("raw_blocks.id"), index=True)
    table_code: Mapped[str] = mapped_column(String(96), index=True)
    table_name: Mapped[str] = mapped_column(String(255))
    table_definition: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    published_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 码表在原文中的出现顺序（来源块的 ordinal），列表按它排序才符合阅读顺序
    source_ordinal: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FieldMetadataModel(Base):
    __tablename__ = "field_metadata"
    __table_args__ = (UniqueConstraint("table_id", "field_en", name="uq_field_table_en"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    table_id: Mapped[str] = mapped_column(ForeignKey("table_metadata.id"), index=True)
    field_cn: Mapped[str] = mapped_column(String(255))
    field_en: Mapped[str] = mapped_column(String(128))
    field_definition: Mapped[str] = mapped_column(Text)
    check_text_raw: Mapped[str] = mapped_column(Text)
    # SOURCE=原文声明了类型 / INFERRED=推断或默认 / MANUAL=人工确认过 / UNKNOWN=历史数据
    type_basis: Mapped[str] = mapped_column(String(16), default="UNKNOWN", server_default="UNKNOWN")
    data_type: Mapped[str] = mapped_column(String(32))
    length: Mapped[int] = mapped_column(Integer)
    decimal_places: Mapped[int] = mapped_column(Integer, default=0)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    code_reference_text: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0)
    evidence_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    sort_order: Mapped[int] = mapped_column(Integer)
    ai_value_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MetadataRevisionModel(Base):
    __tablename__ = "metadata_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    table_id: Mapped[str] = mapped_column(ForeignKey("table_metadata.id"), index=True)
    field_id: Mapped[Optional[str]] = mapped_column(ForeignKey("field_metadata.id"), nullable=True)
    revision_kind: Mapped[str] = mapped_column(String(32))
    old_value_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    new_value_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    editor: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MetadataValidationModel(Base):
    __tablename__ = "metadata_validations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    table_id: Mapped[str] = mapped_column(ForeignKey("table_metadata.id"), index=True)
    validator_kind: Mapped[str] = mapped_column(String(32))
    round: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    issues_json: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CodeSetModel(Base):
    __tablename__ = "code_sets"
    __table_args__ = (UniqueConstraint("code", "current_version", name="uq_code_set_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(96), index=True)
    name: Mapped[str] = mapped_column(String(255))
    definition: Mapped[str] = mapped_column(Text, default="")
    source_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), index=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    published_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 码表在原文中的出现顺序（来源块的 ordinal）
    source_ordinal: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CodeValueModel(Base):
    __tablename__ = "code_values"
    __table_args__ = (UniqueConstraint("code_set_id", "value", name="uq_code_value"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code_set_id: Mapped[str] = mapped_column(ForeignKey("code_sets.id"), index=True)
    value: Mapped[str] = mapped_column(String(128))
    label_cn: Mapped[str] = mapped_column(String(255))
    label_en: Mapped[str] = mapped_column(String(255), default="")
    definition: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source_locator: Mapped[Dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CodeSetValidationModel(Base):
    __tablename__ = "code_set_validations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code_set_id: Mapped[str] = mapped_column(ForeignKey("code_sets.id"), index=True)
    validator_kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    issues_json: Mapped[list] = mapped_column(JSON)
    trace_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FieldCodeReferenceModel(Base):
    __tablename__ = "field_code_references"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    field_id: Mapped[str] = mapped_column(ForeignKey("field_metadata.id"), index=True)
    code_set_id: Mapped[str] = mapped_column(ForeignKey("code_sets.id"), index=True)
    reference_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    condition_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column(Float, default=0)


class GenerationPlanModel(Base):
    __tablename__ = "generation_plans"
    __table_args__ = (UniqueConstraint("run_id", "table_code", name="uq_generation_plan_table"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    table_code: Mapped[str] = mapped_column(String(96), index=True)
    plan_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    trace_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SchemaDefinitionModel(Base):
    __tablename__ = "schema_definitions"
    __table_args__ = (UniqueConstraint("run_id", "table_code", "version", name="uq_schema_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    table_code: Mapped[str] = mapped_column(String(96))
    version: Mapped[int] = mapped_column(Integer)
    definition_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GeneratorDefinitionModel(Base):
    __tablename__ = "generator_definitions"
    __table_args__ = (UniqueConstraint("run_id", "table_code", "version", name="uq_generator_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    table_code: Mapped[str] = mapped_column(String(96))
    version: Mapped[int] = mapped_column(Integer)
    definition_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditCaseDefinitionModel(Base):
    __tablename__ = "audit_case_definitions"
    __table_args__ = (UniqueConstraint("run_id", "table_code", "version", name="uq_audit_case_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    table_code: Mapped[str] = mapped_column(String(96))
    version: Mapped[int] = mapped_column(Integer)
    definition_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ApprovalModel(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    object_type: Mapped[str] = mapped_column(String(32), index=True)
    object_hash: Mapped[str] = mapped_column(String(64), index=True)
    decision: Mapped[str] = mapped_column(String(24))
    approver: Mapped[str] = mapped_column(String(80))
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ArtifactModel(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    artifact_type: Mapped[str] = mapped_column(String(32))
    blob_uri: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
