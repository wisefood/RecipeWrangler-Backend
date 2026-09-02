"""Store active regional recipe-cost calibrations in PostgreSQL."""

from __future__ import annotations

from alembic import op


revision = "20260902_0004"
down_revision = "20260902_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.cost_recipe_calibrations (
            calibration_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            region text NOT NULL CHECK (region IN ('EU', 'IE', 'HU', 'SI')),
            calibration_version text NOT NULL,
            q33_cost_per_serving_eur double precision NOT NULL,
            q67_cost_per_serving_eur double precision NOT NULL,
            reference_recipe_count integer NOT NULL,
            minimum_weight_coverage double precision NOT NULL,
            quantile_method text NOT NULL,
            generated_at timestamptz NOT NULL DEFAULT now(),
            is_active boolean NOT NULL DEFAULT true,
            CHECK (q33_cost_per_serving_eur > 0),
            CHECK (q67_cost_per_serving_eur > q33_cost_per_serving_eur)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS cost_recipe_calibrations_one_active_region
        ON public.cost_recipe_calibrations (region)
        WHERE is_active
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.cost_recipe_calibrations")
