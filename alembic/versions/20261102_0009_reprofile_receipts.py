"""Atomic backups and operation receipts for scoped nutrition reprofiles."""
from alembic import op

revision = '20261102_0009'
down_revision = '20261031_0008'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE matching_reprofile_receipts (
        operation_id text PRIMARY KEY,
        package_digest text NOT NULL,
        status text NOT NULL CHECK (status IN ('applied','rolled_back')),
        payload jsonb NOT NULL,
        payload_digest text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())''')


def downgrade():
    # Receipts contain the only transactional mirror backups; retain them.
    raise RuntimeError('Export and review retained reprofile backups before dropping receipts')
