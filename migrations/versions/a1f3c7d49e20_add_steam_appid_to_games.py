"""add steam_appid to games

Revision ID: a1f3c7d49e20
Revises: 82bf6fc9b1c7
Create Date: 2026-11-22 15:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'a1f3c7d49e20'
down_revision = '82bf6fc9b1c7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Carry the Steam appid on the roster, so the study joins to the genres.

    Nullable on purpose: the games with no Steam page (the mobile versions,
    Fortnite) keep it empty, and that is the expected state, not a gap to fill
    (SPEC_GENRES section 7).
    """
    op.add_column("games", sa.Column("steam_appid", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("games", "steam_appid")
