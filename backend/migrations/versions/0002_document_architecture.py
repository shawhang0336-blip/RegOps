"""Add AI document architecture plans.

Revision ID: 0002_document_architecture
Revises: 0001_normalized_assets
"""

from alembic import op

from app.infra.db_tables import DocumentArchitectureModel

revision = "0002_document_architecture"
down_revision = "0001_normalized_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    DocumentArchitectureModel.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    DocumentArchitectureModel.__table__.drop(bind=op.get_bind(), checkfirst=True)
