"""Store compact profiling quality separately from the archival trace."""

from __future__ import annotations

from alembic import op

revision = "20260825_0002"
down_revision = "20260319_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        'ALTER TABLE "public"."nutrients-recipe-profiles" '
        "ADD COLUMN IF NOT EXISTS profiling_quality jsonb"
    )
    op.execute(
        """
        UPDATE "public"."nutrients-recipe-profiles"
        SET profiling_quality = COALESCE(
          trace -> 'profiling_quality',
          trace -> 'profiling' -> 'quality',
          nutrition_profiling_debug -> 'profiling_quality',
          nutrition_profiling_debug -> 'profiling' -> 'quality'
        )
        WHERE profiling_quality IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        'ALTER TABLE "public"."nutrients-recipe-profiles" '
        "DROP COLUMN IF EXISTS profiling_quality"
    )
