"""phase 12.9: scope idempotency_records by user_id, composite primary key, add expiration

Revision ID: e6799137d151
Revises: 0c8ab0c30f96
Create Date: 2026-08-19 11:47:47.669201

Hand-adjusted after autogenerate: autogenerate only detected the added
`user_id` column, not the primary-key change (Alembic's autogenerate
does not reliably diff primary-key constraints, especially across
dialects), and this revision also folds in `expires_at` (Step 12.9's
"expired key -> correct expiration behavior" requirement, decided at the
same time as the composite-key change rather than as a separate
migration). Rewritten as a drop-and-recreate rather than an in-place
`ALTER TABLE ... DROP CONSTRAINT / ADD CONSTRAINT` because this table has
no production data anywhere yet (Phase 12 has not shipped) — see
PHASE_12_1_PERSISTENCE_AUDIT.md §12 ("No live data migration is
required — there is no existing persisted data anywhere in this system
to migrate"). A drop-and-recreate is simpler and more obviously correct
than a portable in-place constraint rewrite for a table that is
guaranteed empty in every real environment this migration will ever run
against.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6799137d151'
down_revision: Union[str, Sequence[str], None] = '0c8ab0c30f96'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table('idempotency_records')
    op.create_table(
        'idempotency_records',
        sa.Column('user_id', sa.String(length=128), nullable=False),
        sa.Column('request_id', sa.String(length=64), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('result_status', sa.String(length=32), nullable=False),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('user_id', 'request_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('idempotency_records')
    op.create_table(
        'idempotency_records',
        sa.Column('request_id', sa.String(length=64), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('result_status', sa.String(length=32), nullable=False),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('request_id'),
    )
