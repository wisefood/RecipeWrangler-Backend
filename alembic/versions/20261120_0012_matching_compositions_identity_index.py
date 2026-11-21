"""Index the food identity used by identity_composition_records (was a full scan of matching_compositions, ~0.4 s per search)."""
from alembic import op

revision = '20261120_0012'
down_revision = '20261118_0011'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE INDEX matching_compositions_identity ON public.matching_compositions
        (release_id, collection, (COALESCE(annotation->'text_annotation'->'facets'->>'base_identity',
                                           annotation->'text_annotation'->>'main_ingredient')))""")


def downgrade():
    op.execute('DROP INDEX public.matching_compositions_identity')
