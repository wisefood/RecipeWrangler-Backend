"""Initialize nutrition Postgres tables used by API/profiling."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260319_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS "public"."nutrients-ingredients-usda" (
          usda_id   text PRIMARY KEY,
          food_name text NOT NULL,
          nutrients jsonb NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS "public"."nutrients-recipes-usda" (
          recipe_id text PRIMARY KEY,
          title text NOT NULL,
          total_nutrients jsonb NOT NULL,
          total_nutrients_per_serving jsonb,
          nutri_score jsonb,
          source text
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS "public"."nutrients-ingredients-irish" (
          row_id BIGSERIAL PRIMARY KEY,
          "canonical_food_id" text,
          "Food Name" text,
          "Description" text,
          "Group" text,
          "Water (g)" text,
          "Total nitrogen (g)" text,
          "Protein (g)" text,
          "Fat (g)" text,
          "Carbohydrate (g)" text,
          "Energy (kcal) (kcal)" text,
          "Energy (kJ) (kJ)" text,
          "Starch (g)" text,
          "Total sugars (g)" text,
          "Glucose (g)" text,
          "Fructose (g)" text,
          "Satd FA /100g fd (g)" text,
          "Mono FA /100g food (g)" text,
          "Poly FA /100g food (g)" text,
          "Sodium (mg)" text,
          "Potassium (mg)" text,
          "Calcium (mg)" text,
          "Magnesium (mg)" text,
          "Phosphorus (mg)" text,
          "Iron (mg)" text,
          "Copper (mg)" text,
          "Zinc (mg)" text,
          "Chloride (mg)" text,
          "Manganese (mg)" text,
          "Selenium (µg)" text,
          "Iodine (µg)" text,
          "Retinol (µg)" text,
          "Carotene (µg)" text,
          "Retinol Equivalent (µg)" text,
          "Vitamin D (µg)" text,
          "Vitamin E (mg)" text,
          "Vitamin B6 (mg)" text,
          "Vitamin B12 (µg)" text,
          "Vitamin C (mg)" text
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS "nutrients-ingredients-irish_canonical_idx"
        ON "public"."nutrients-ingredients-irish" ("canonical_food_id")
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS "public"."nutrients-recipe-profiles" (
          recipe_id text PRIMARY KEY,
          title text,
          source text,
          nutrition_source text,
          total_nutrients jsonb,
          total_nutrients_per_serving jsonb,
          nutri_score jsonb,
          nutri_score_breakdown jsonb,
          nutrition_profiling_details jsonb,
          nutrition_profiling_debug jsonb,
          trace jsonb,
          pipeline_version text,
          mapping_version text,
          embedding_model text,
          ruleset_version text,
          computed_at timestamptz DEFAULT now(),
          updated_at timestamptz DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute('DROP TABLE IF EXISTS "public"."nutrients-recipe-profiles"')
    op.execute('DROP INDEX IF EXISTS "public"."nutrients-ingredients-irish_canonical_idx"')
    op.execute('DROP TABLE IF EXISTS "public"."nutrients-ingredients-irish"')
    op.execute('DROP TABLE IF EXISTS "public"."nutrients-recipes-usda"')
    op.execute('DROP TABLE IF EXISTS "public"."nutrients-ingredients-usda"')
