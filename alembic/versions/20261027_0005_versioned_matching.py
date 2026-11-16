"""Add immutable matching catalogues, concepts and recipe calculation runs."""
from alembic import op

revision = '20261027_0005'
down_revision = '20260902_0004'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE public.matching_releases (
        release_id text PRIMARY KEY, manifest jsonb NOT NULL,
        status text NOT NULL CHECK(status IN ('loading','ready')),
        created_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('''CREATE TABLE public.matching_compositions (
        release_id text NOT NULL REFERENCES public.matching_releases,
        collection text NOT NULL, candidate_id text NOT NULL, native_id text NOT NULL,
        candidate jsonb NOT NULL, nutrient_row jsonb NOT NULL,
        source_record jsonb, source_profile jsonb, annotation jsonb NOT NULL,
        PRIMARY KEY(release_id,collection,candidate_id))''')
    op.execute('CREATE INDEX matching_compositions_native ON public.matching_compositions(release_id,collection,native_id)')
    op.execute('''CREATE TABLE public.matching_concepts (
        concept_id text PRIMARY KEY, facet_kind text NOT NULL, value text NOT NULL,
        label text NOT NULL, mappings jsonb NOT NULL DEFAULT '[]',
        provenance jsonb NOT NULL, UNIQUE(facet_kind,value))''')
    op.execute('''CREATE TABLE public.matching_profile_runs (
        run_id text PRIMARY KEY, recipe_id text NOT NULL, nutrition_source text NOT NULL,
        release_id text REFERENCES public.matching_releases,
        mechanism_version text NOT NULL, input_hash text NOT NULL,
        profile jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('CREATE INDEX matching_runs_recipe ON public.matching_profile_runs(recipe_id,nutrition_source,created_at DESC)')
    op.execute('''CREATE TABLE public.matching_active_profiles (
        recipe_id text NOT NULL, nutrition_source text NOT NULL,
        run_id text NOT NULL REFERENCES public.matching_profile_runs,
        PRIMARY KEY(recipe_id,nutrition_source))''')
    op.execute('''CREATE TABLE public.matching_deployment (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
        release_id text REFERENCES public.matching_releases,
        mode text NOT NULL DEFAULT 'legacy' CHECK(mode IN ('legacy','shadow','new')),
        updated_at timestamptz NOT NULL DEFAULT now())''')
    op.execute("INSERT INTO public.matching_deployment(singleton,mode) VALUES(true,'legacy')")


def downgrade():
    for table in ['matching_deployment','matching_active_profiles','matching_profile_runs',
                  'matching_concepts','matching_compositions','matching_releases']:
        op.execute('DROP TABLE public.'+table)
