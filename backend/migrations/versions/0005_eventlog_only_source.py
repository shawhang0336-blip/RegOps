"""Make EventLog the only source of truth for Run state.

Revision ID: 0005_eventlog_only_source
Revises: 0004_type_basis_and_order

删除 runs 表上的 status/current_node/attempt/result_json 四列。
这四列此前与 events 表双写，存在漂移风险；状态现在统一由事件折叠得到
（见 app/events.py 的 project()）。历史批次的状态可从已有事件账本完整重建。
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_eventlog_only_source"
down_revision = "0004_type_basis_and_order"
branch_labels = None
depends_on = None

_STATE_COLUMNS = ("status", "current_node", "attempt", "result_json")


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column in {item["name"] for item in inspector.get_columns(table)}


def upgrade() -> None:
    for column in _STATE_COLUMNS:
        if _has_column("runs", column):
            op.drop_column("runs", column)


def downgrade() -> None:
    """降级时重建列并置默认值；精确状态仍应从 events 折叠，列只作占位。"""
    if not _has_column("runs", "status"):
        op.add_column("runs", sa.Column("status", sa.String(32), nullable=True, server_default="NEW"))
    if not _has_column("runs", "current_node"):
        op.add_column("runs", sa.Column("current_node", sa.String(64), nullable=True, server_default="created"))
    if not _has_column("runs", "attempt"):
        op.add_column("runs", sa.Column("attempt", sa.Integer(), nullable=True, server_default="0"))
    if not _has_column("runs", "result_json"):
        op.add_column("runs", sa.Column("result_json", sa.JSON(), nullable=True))
