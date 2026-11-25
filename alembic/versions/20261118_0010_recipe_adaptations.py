"""One row per adapted recipe, mode and region (nutrition, sustainability, cost)."""
from alembic import op

revision = '20261118_0010'
down_revision = '20261102_0009'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE recipe_adaptations (
        recipe_id text NOT NULL,
        mode text NOT NULL CHECK (mode IN ('nutrition','sustainability','cost')),
        region text NOT NULL CHECK (region IN ('IE','HU','SI','EU')),
        version text NOT NULL,
        status text NOT NULL CHECK (status IN ('adapted','no_suggestion','not_scored')),
        reason_code text,
        original jsonb,
        adapted jsonb,
        swap jsonb,
        benefit jsonb,
        provenance jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (recipe_id, mode, region))''')
    op.execute('CREATE INDEX recipe_adaptations_mode_status ON recipe_adaptations (mode, region, status)')


def downgrade():
    # Rows are derived from the adaptation runs and can be rebuilt with scripts/sustainability/load_adaptations.py
    op.execute('DROP TABLE recipe_adaptations')
