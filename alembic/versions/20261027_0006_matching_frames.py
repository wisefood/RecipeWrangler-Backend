"""Persist grounded ingredient frames and enforce active-profile ownership."""
from alembic import op

revision='20261027_0006'
down_revision='20261027_0005'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE public.matching_ingredient_frames (
        frame_id text PRIMARY KEY,
        release_id text NOT NULL REFERENCES public.matching_releases,
        input_name text NOT NULL, annotation jsonb NOT NULL,
        projection jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('''ALTER TABLE public.matching_profile_runs ADD CONSTRAINT matching_runs_owner
        UNIQUE(run_id,recipe_id,nutrition_source)''')
    op.execute('''ALTER TABLE public.matching_active_profiles ADD CONSTRAINT matching_active_owner
        FOREIGN KEY(run_id,recipe_id,nutrition_source)
        REFERENCES public.matching_profile_runs(run_id,recipe_id,nutrition_source)''')


def downgrade():
    op.execute('ALTER TABLE public.matching_active_profiles DROP CONSTRAINT matching_active_owner')
    op.execute('ALTER TABLE public.matching_profile_runs DROP CONSTRAINT matching_runs_owner')
    op.execute('DROP TABLE public.matching_ingredient_frames')
