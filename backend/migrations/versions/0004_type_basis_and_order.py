"""Add type_basis on fields and source_ordinal on code sets.

Revision ID: 0004_type_basis_and_order
Revises: 0003_code_set_validations

type_basis 记录"字段类型有没有原文依据"：
  SOURCE     原文明确声明了类型（如"数值型，22.2"）
  INFERRED   原文没有类型声明，类型是推断或默认值
  MANUAL     人工确认过
source_ordinal 记录码表在文档中的出现顺序，用于列表按原文顺序展示。
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_type_basis_and_order"
down_revision = "0003_code_set_validations"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column in {item["name"] for item in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column("field_metadata", "type_basis"):
        op.add_column(
            "field_metadata",
            sa.Column("type_basis", sa.String(16), nullable=False, server_default="UNKNOWN"),
        )
    if not _has_column("code_sets", "source_ordinal"):
        op.add_column(
            "code_sets",
            sa.Column("source_ordinal", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    if _has_column("field_metadata", "type_basis"):
        op.drop_column("field_metadata", "type_basis")
    if _has_column("code_sets", "source_ordinal"):
        op.drop_column("code_sets", "source_ordinal")
