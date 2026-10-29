"""Create missing NeuroScan tables and apply additive legacy columns.

Revision ID: 20261029_0001
Revises:
Create Date: 2026-10-29

This baseline is intentionally additive: it preserves rows and tables in an
existing deployment, while creating missing tables and legacy columns.
"""
from alembic import op

from app.db import initialize_database

revision = "20261029_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    initialize_database(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Initial schema migration is irreversible; restore from a database backup instead")
