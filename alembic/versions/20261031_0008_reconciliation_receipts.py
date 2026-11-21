"""Idempotent projection handoff for guarded graph reconciliations."""
from alembic import op

revision = '20261031_0008'
down_revision = '20261029_0007'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE matching_reconciliation_receipts (
        operation_id text PRIMARY KEY,
        recipe_ids jsonb NOT NULL,
        generations jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now())''')


def downgrade():
    op.execute('DROP TABLE matching_reconciliation_receipts')
