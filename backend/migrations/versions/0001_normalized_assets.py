"""Create normalized metadata and generation asset tables.

Revision ID: 0001_normalized_assets
Revises:
"""

from alembic import op

from app.db import Base
from app import asset_models, models

revision = "0001_normalized_assets"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    for table in reversed(Base.metadata.sorted_tables):
        if table.name not in {"specifications", "rules", "runs", "events", "settings"}:
            table.drop(bind=op.get_bind(), checkfirst=True)
