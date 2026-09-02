"""Add the canonical regional ingredient-cost catalogue."""

from __future__ import annotations

from alembic import op


revision = "20260902_0003"
down_revision = "20260825_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.cost_products (
            product_id text PRIMARY KEY,
            source_ingredient_id text NOT NULL,
            canonical_name text NOT NULL,
            product_detail text,
            product_level text NOT NULL CHECK (product_level IN ('base', 'detail')),
            food_category text NOT NULL,
            pli_category text,
            global_cost_tier text NOT NULL,
            price_evidence_confidence text NOT NULL,
            cost_reference_version text NOT NULL,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.cost_prices (
            product_id text NOT NULL REFERENCES public.cost_products(product_id)
                ON DELETE CASCADE,
            region text NOT NULL CHECK (region IN ('EU', 'IE', 'HU', 'SI')),
            price_eur_kg double precision NOT NULL CHECK (price_eur_kg > 0),
            PRIMARY KEY (product_id, region)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.cost_aliases (
            alias_normalized text PRIMARY KEY,
            product_id text NOT NULL REFERENCES public.cost_products(product_id)
                ON DELETE CASCADE,
            review_status text NOT NULL DEFAULT 'reviewed',
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS cost_products_canonical_name_idx "
        "ON public.cost_products (canonical_name)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS cost_prices_region_idx "
        "ON public.cost_prices (region)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.cost_aliases")
    op.execute("DROP TABLE IF EXISTS public.cost_prices")
    op.execute("DROP TABLE IF EXISTS public.cost_products")
