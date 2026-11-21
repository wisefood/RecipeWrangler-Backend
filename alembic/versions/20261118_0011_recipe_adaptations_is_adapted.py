"""Boolean facet on recipe_adaptations: is_adapted = (status = 'adapted'), computed so it cannot disagree with status."""
from alembic import op

revision = '20261118_0011'
down_revision = '20261118_0010'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE recipe_adaptations ADD COLUMN is_adapted boolean GENERATED ALWAYS AS (status = 'adapted') STORED")


def downgrade():
    op.execute('ALTER TABLE recipe_adaptations DROP COLUMN is_adapted')
