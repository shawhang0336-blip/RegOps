"""Add code set validation audit records.

Revision ID: 0003_code_set_validations
Revises: 0002_document_architecture
"""

from alembic import op

from app.infra.models import CodeSetValidationModel

revision = "0003_code_set_validations"
down_revision = "0002_document_architecture"
branch_labels = None
depends_on = None


def upgrade() -> None:
    CodeSetValidationModel.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    CodeSetValidationModel.__table__.drop(bind=op.get_bind(), checkfirst=True)
