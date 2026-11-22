"""Widen MySQL/Oracle text and identifier columns to prevent DataError 1406.

Revision ID: 0006_mysql_column_widths
Revises: 0005_eventlog_only_source

修复真实 MySQL 8 严格模式下的两类列宽截断/报错：
1. raw_blocks.raw_content 与长说明列从 TEXT（64KB 上限）升级为 LONGTEXT（4GB 上限）；
2. specifications.table_code / rules.table_code 对齐为 VARCHAR(96)，rules.rule_key 扩至 VARCHAR(192)。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import LONGTEXT

revision = "0006_mysql_column_widths"
down_revision = "0005_eventlog_only_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        return

    if dialect == "mysql":
        for table, column in (
            ("raw_blocks", "raw_content"),
            ("table_metadata", "table_definition"),
            ("field_metadata", "field_definition"),
            ("field_metadata", "check_text_raw"),
            ("code_sets", "definition"),
        ):
            op.alter_column(table, column, existing_type=sa.Text(), type_=LONGTEXT(), existing_nullable=False)

    op.alter_column(
        "specifications", "table_code", existing_type=sa.String(64), type_=sa.String(96), existing_nullable=False
    )
    op.alter_column(
        "rules", "table_code", existing_type=sa.String(64), type_=sa.String(96), existing_nullable=False
    )
    op.alter_column(
        "rules", "rule_key", existing_type=sa.String(96), type_=sa.String(192), existing_nullable=False
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        return

    op.alter_column(
        "rules", "rule_key", existing_type=sa.String(192), type_=sa.String(96), existing_nullable=False
    )
    op.alter_column(
        "rules", "table_code", existing_type=sa.String(96), type_=sa.String(64), existing_nullable=False
    )
    op.alter_column(
        "specifications", "table_code", existing_type=sa.String(96), type_=sa.String(64), existing_nullable=False
    )
    if dialect == "mysql":
        for table, column in (
            ("code_sets", "definition"),
            ("field_metadata", "check_text_raw"),
            ("field_metadata", "field_definition"),
            ("table_metadata", "table_definition"),
            ("raw_blocks", "raw_content"),
        ):
            op.alter_column(table, column, existing_type=LONGTEXT(), type_=sa.Text(), existing_nullable=False)
