"""Add SpecPackage multi-file container, document roles, and structured rule evidences.

Revision ID: 0007_spec_packages_and_rule_evidence
Revises: 0006_mysql_column_widths
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import LONGTEXT

revision = "0007_spec_packages_and_rule_evidence"
down_revision = "0006_mysql_column_widths"
branch_labels = None
depends_on = None

_LONG_TEXT = sa.Text().with_variant(LONGTEXT(), "mysql")


def _col_names(inspector, table_name: str) -> set:
    return {c["name"] for c in inspector.get_columns(table_name)}


def _index_names(inspector, table_name: str) -> set:
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def _unique_names(inspector, table_name: str) -> set:
    return {uc["name"] for uc in inspector.get_unique_constraints(table_name) if uc.get("name")}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    dialect = bind.dialect.name

    if "spec_packages" not in tables:
        op.create_table(
            "spec_packages",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("regulator", sa.String(32), nullable=False, server_default="SAFE"),
            sa.Column("spec_version", sa.String(64), nullable=False, server_default="UNKNOWN"),
            sa.Column("parent_package_id", sa.String(36), sa.ForeignKey("spec_packages.id"), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_spec_packages_regulator", "spec_packages", ["regulator"])
        op.create_index("ix_spec_packages_status", "spec_packages", ["status"])

    insp = sa.inspect(bind)
    if "source_documents" in tables:
        cols = _col_names(insp, "source_documents")
        if "package_id" not in cols:
            op.add_column("source_documents", sa.Column("package_id", sa.String(36), nullable=True))
        if "doc_role" not in cols:
            op.add_column(
                "source_documents",
                sa.Column("doc_role", sa.String(32), nullable=False, server_default="AUTO"),
            )
        idxs = _index_names(insp, "source_documents")
        if "ix_source_documents_package_id" not in idxs:
            op.create_index("ix_source_documents_package_id", "source_documents", ["package_id"])

        ucs = _unique_names(insp, "source_documents")
        if dialect == "sqlite":
            with op.batch_alter_table("source_documents") as batch_op:
                if "ix_source_documents_sha256" in idxs:
                    try:
                        batch_op.drop_index("ix_source_documents_sha256")
                    except Exception:
                        pass
                batch_op.create_index("ix_source_documents_sha256", ["sha256"], unique=False)
                if "uq_doc_package_sha256" not in ucs:
                    batch_op.create_unique_constraint("uq_doc_package_sha256", ["package_id", "sha256"])
        else:
            if "ix_source_documents_sha256" in idxs:
                idx_obj = next((i for i in insp.get_indexes("source_documents") if i["name"] == "ix_source_documents_sha256"), None)
                if idx_obj and idx_obj.get("unique"):
                    op.drop_index("ix_source_documents_sha256", table_name="source_documents")
                    op.create_index("ix_source_documents_sha256", "source_documents", ["sha256"], unique=False)
            if "uq_doc_package_sha256" not in ucs:
                op.create_unique_constraint("uq_doc_package_sha256", "source_documents", ["package_id", "sha256"])

    insp = sa.inspect(bind)
    if "table_metadata" in tables:
        cols = _col_names(insp, "table_metadata")
        if "package_id" not in cols:
            op.add_column("table_metadata", sa.Column("package_id", sa.String(36), nullable=True))
        if "ix_table_metadata_package_id" not in _index_names(insp, "table_metadata"):
            op.create_index("ix_table_metadata_package_id", "table_metadata", ["package_id"])

    insp = sa.inspect(bind)
    if "code_sets" in tables:
        cols = _col_names(insp, "code_sets")
        if "package_id" not in cols:
            op.add_column("code_sets", sa.Column("package_id", sa.String(36), nullable=True))
        if "ix_code_sets_package_id" not in _index_names(insp, "code_sets"):
            op.create_index("ix_code_sets_package_id", "code_sets", ["package_id"])
        ucs = _unique_names(insp, "code_sets")
        if dialect == "sqlite":
            with op.batch_alter_table("code_sets") as batch_op:
                if "uq_code_set_version" in ucs:
                    batch_op.drop_constraint("uq_code_set_version", type_="unique")
                if "uq_code_set_pkg_version" not in ucs:
                    batch_op.create_unique_constraint("uq_code_set_pkg_version", ["package_id", "code", "current_version"])
        else:
            if "uq_code_set_version" in ucs:
                op.drop_constraint("uq_code_set_version", "code_sets", type_="unique")
            if "uq_code_set_pkg_version" not in ucs:
                op.create_unique_constraint("uq_code_set_pkg_version", "code_sets", ["package_id", "code", "current_version"])

    if "table_rule_evidences" not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
            "table_rule_evidences",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("package_id", sa.String(36), sa.ForeignKey("spec_packages.id"), nullable=True),
            sa.Column("document_id", sa.String(36), sa.ForeignKey("source_documents.id"), nullable=False),
            sa.Column("block_id", sa.String(36), sa.ForeignKey("raw_blocks.id"), nullable=False),
            sa.Column("table_code", sa.String(96), nullable=False),
            sa.Column("table_name", sa.String(255), nullable=False, server_default=""),
            sa.Column("rule_code", sa.String(96), nullable=False, server_default=""),
            sa.Column("rule_name", sa.String(255), nullable=False, server_default=""),
            sa.Column("severity", sa.String(16), nullable=False, server_default="HARD"),
            sa.Column("scope", sa.String(24), nullable=False, server_default="SINGLE_TABLE"),
            sa.Column("target_fields_json", sa.JSON(), nullable=False),
            sa.Column("rule_text", _LONG_TEXT, nullable=False),
            sa.Column("reference_sql", _LONG_TEXT, nullable=False, server_default=""),
            sa.Column("source_locator", sa.JSON(), nullable=False),
            sa.Column("rule_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("document_id", "block_id", "rule_hash", name="uq_rule_evidence_block_hash"),
        )
        op.create_index("ix_table_rule_evidences_package_id", "table_rule_evidences", ["package_id"])
        op.create_index("ix_table_rule_evidences_document_id", "table_rule_evidences", ["document_id"])
        op.create_index("ix_table_rule_evidences_block_id", "table_rule_evidences", ["block_id"])
        op.create_index("ix_table_rule_evidences_table_code", "table_rule_evidences", ["table_code"])
        op.create_index("ix_table_rule_evidences_rule_hash", "table_rule_evidences", ["rule_hash"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    dialect = bind.dialect.name

    if "table_rule_evidences" in tables:
        op.drop_table("table_rule_evidences")

    if "code_sets" in tables:
        ucs = _unique_names(insp, "code_sets")
        cols = _col_names(insp, "code_sets")
        if dialect == "sqlite":
            with op.batch_alter_table("code_sets") as batch_op:
                if "uq_code_set_pkg_version" in ucs:
                    batch_op.drop_constraint("uq_code_set_pkg_version", type_="unique")
                if "uq_code_set_version" not in ucs:
                    batch_op.create_unique_constraint("uq_code_set_version", ["code", "current_version"])
                if "package_id" in cols:
                    batch_op.drop_column("package_id")
        else:
            if "uq_code_set_pkg_version" in ucs:
                op.drop_constraint("uq_code_set_pkg_version", "code_sets", type_="unique")
            if "uq_code_set_version" not in ucs:
                op.create_unique_constraint("uq_code_set_version", "code_sets", ["code", "current_version"])
            if "package_id" in cols:
                op.drop_column("code_sets", "package_id")

    if "table_metadata" in tables and "package_id" in _col_names(insp, "table_metadata"):
        if "ix_table_metadata_package_id" in _index_names(insp, "table_metadata"):
            op.drop_index("ix_table_metadata_package_id", table_name="table_metadata")
        op.drop_column("table_metadata", "package_id")

    if "source_documents" in tables:
        ucs = _unique_names(insp, "source_documents")
        cols = _col_names(insp, "source_documents")
        if dialect == "sqlite":
            with op.batch_alter_table("source_documents") as batch_op:
                if "uq_doc_package_sha256" in ucs:
                    batch_op.drop_constraint("uq_doc_package_sha256", type_="unique")
                if "doc_role" in cols:
                    batch_op.drop_column("doc_role")
                if "package_id" in cols:
                    batch_op.drop_column("package_id")
        else:
            if "uq_doc_package_sha256" in ucs:
                op.drop_constraint("uq_doc_package_sha256", "source_documents", type_="unique")
            if "ix_source_documents_package_id" in _index_names(insp, "source_documents"):
                op.drop_index("ix_source_documents_package_id", table_name="source_documents")
            if "doc_role" in cols:
                op.drop_column("source_documents", "doc_role")
            if "package_id" in cols:
                op.drop_column("source_documents", "package_id")

    if "spec_packages" in tables:
        op.drop_table("spec_packages")
