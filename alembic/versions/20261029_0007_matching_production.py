"""Durable matching projection jobs and reversible production promotion."""
from alembic import op

revision = '20261029_0007'
down_revision = '20261027_0006'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE matching_projection_jobs (
        recipe_id text PRIMARY KEY, generation bigint NOT NULL DEFAULT 1,
        completed_generation bigint NOT NULL DEFAULT 0,
        attempts integer NOT NULL DEFAULT 0, last_error text,
        requested_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz)''')
    op.execute('''CREATE TABLE matching_production_backups (
        promotion_id text NOT NULL, recipe_id text NOT NULL, nutrition_source text NOT NULL,
        previous_row jsonb NOT NULL, PRIMARY KEY(promotion_id,recipe_id,nutrition_source))''')
    op.execute('''CREATE TABLE matching_promotions (
        promotion_id text PRIMARY KEY, release_id text NOT NULL REFERENCES matching_releases,
        previous_deployment jsonb NOT NULL, previous_active_profiles jsonb NOT NULL,
        status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('''CREATE TABLE matching_promotion_profiles (
        promotion_id text NOT NULL REFERENCES matching_promotions,
        recipe_id text NOT NULL, nutrition_source text NOT NULL,
        run_id text NOT NULL REFERENCES matching_profile_runs,
        source_run_id text NOT NULL REFERENCES matching_profile_runs,
        PRIMARY KEY(promotion_id,recipe_id,nutrition_source))''')


def downgrade():
    for table in ('matching_promotion_profiles', 'matching_promotions', 'matching_production_backups', 'matching_projection_jobs'):
        op.execute('DROP TABLE ' + table)
