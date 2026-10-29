"""Add Google OAuth identities and one-time handoff records.

Revision ID: 20261029_0002
Revises: 20261029_0001
Create Date: 2026-10-29
"""
from alembic import op

from app.models import OAuthIdentity, OAuthLoginHandoff, OAuthLoginState


revision = "20261029_0002"
down_revision = "20261029_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The legacy baseline dynamically calls metadata.create_all(), so it may
    # have already created these tables before this revision is reached.
    bind = op.get_bind()
    OAuthIdentity.__table__.create(bind, checkfirst=True)
    OAuthLoginState.__table__.create(bind, checkfirst=True)
    OAuthLoginHandoff.__table__.create(bind, checkfirst=True)


def downgrade() -> None:
    raise RuntimeError(
        "Google OAuth migration is intentionally irreversible to avoid deleting login identities or pending handoffs."
    )
