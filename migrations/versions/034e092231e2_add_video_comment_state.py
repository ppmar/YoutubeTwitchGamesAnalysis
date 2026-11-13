"""add video comment state

Revision ID: 034e092231e2
Revises: bcdea0c4b164
Create Date: 2026-11-13 15:46:50.771318
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '034e092231e2'
down_revision = 'bcdea0c4b164'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Move comment collection state off the snapshot rows and onto the video.

    The two flags lived on ``video_snapshots``, so every ``refresh-stats`` wrote a
    new row with them back at False and a truncated video silently became eligible
    for method A again (SPEC_CALIBRATION 3.1). The backfill keeps a flag if *any*
    snapshot of that video ever carried it.
    """
    op.create_table(
        "video_comment_state",
        sa.Column("video_id", sa.String(length=32), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("n_stored", sa.Integer(), nullable=False),
        sa.Column("oldest_comment_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("newest_comment_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("video_id"),
    )

    op.execute(
        """
        INSERT INTO video_comment_state
            (video_id, disabled, truncated, complete, n_stored,
             oldest_comment_at, newest_comment_at, last_fetched_at)
        SELECT s.video_id,
               MAX(CASE WHEN s.comments_disabled THEN 1 ELSE 0 END),
               MAX(CASE WHEN s.comments_truncated THEN 1 ELSE 0 END),
               0,
               COALESCE((SELECT COUNT(*) FROM comments c
                         WHERE c.video_id = s.video_id), 0),
               (SELECT MIN(c.comment_published_at) FROM comments c
                WHERE c.video_id = s.video_id),
               (SELECT MAX(c.comment_published_at) FROM comments c
                WHERE c.video_id = s.video_id),
               NULL
        FROM video_snapshots s
        GROUP BY s.video_id
        """
    )
    # A video whose history was walked without hitting the cap counts as complete.
    op.execute(
        """
        UPDATE video_comment_state
        SET complete = 1
        WHERE n_stored > 0 AND truncated = 0 AND disabled = 0
        """
    )

    with op.batch_alter_table("video_snapshots") as batch:
        batch.drop_column("comments_disabled")
        batch.drop_column("comments_truncated")


def downgrade() -> None:
    with op.batch_alter_table("video_snapshots") as batch:
        batch.add_column(
            sa.Column("comments_disabled", sa.Boolean(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("comments_truncated", sa.Boolean(), nullable=False, server_default="0")
        )
    op.drop_table("video_comment_state")
