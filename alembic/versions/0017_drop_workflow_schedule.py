"""drop workflow schedule column

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-29
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("workflows", "schedule")


def downgrade() -> None:
    import sqlalchemy as sa
    op.add_column("workflows", sa.Column("schedule", sa.String(100), nullable=True))
