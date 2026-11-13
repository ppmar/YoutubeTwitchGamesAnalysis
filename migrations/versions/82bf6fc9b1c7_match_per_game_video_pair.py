"""match per game video pair

Revision ID: 82bf6fc9b1c7
Revises: 034e092231e2
Create Date: 2026-11-13 16:22:19.520534
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '82bf6fc9b1c7'
down_revision = '034e092231e2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Hold the game/video match on the pair, and add exclusion terms.

    ``video_snapshots.title_match`` was computed from the union of every linked
    game's terms, so a video counted for each of them (SPEC_FIXES 1.1). The
    backfill carries the old decision onto every pair of that video, which is
    deliberately permissive: `ytc rematch` then re-decides each pair properly,
    without any API call.
    """
    op.add_column("games", sa.Column("exclude_terms", sa.JSON(), nullable=True))
    op.execute("UPDATE games SET exclude_terms = '[]' WHERE exclude_terms IS NULL")

    op.add_column("game_videos", sa.Column("title_match", sa.Boolean(), nullable=True))
    op.add_column("game_videos", sa.Column("match_reason", sa.String(length=128), nullable=True))
    op.add_column("search_slices", sa.Column("query_version", sa.String(length=64), nullable=True))

    op.execute(
        """
        UPDATE game_videos
        SET title_match = (
            SELECT MAX(CASE WHEN s.title_match THEN 1 ELSE 0 END) = 1
            FROM video_snapshots s
            WHERE s.video_id = game_videos.video_id
        )
        """
    )
    # Say where that value came from, so a stale decision is recognisable.
    op.execute(
        "UPDATE game_videos SET match_reason = 'migrated' WHERE title_match IS NOT NULL"
    )

    with op.batch_alter_table("video_snapshots") as batch:
        batch.drop_column("title_match")


def downgrade() -> None:
    with op.batch_alter_table("video_snapshots") as batch:
        batch.add_column(sa.Column("title_match", sa.Boolean(), nullable=True))
    op.drop_column("search_slices", "query_version")
    op.drop_column("game_videos", "match_reason")
    op.drop_column("game_videos", "title_match")
    op.drop_column("games", "exclude_terms")
