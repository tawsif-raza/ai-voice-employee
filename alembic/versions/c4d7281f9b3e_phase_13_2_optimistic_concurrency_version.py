"""phase 13.2: add version column to sessions and memory_records for optimistic concurrency

Revision ID: c4d7281f9b3e
Revises: e6799137d151
Create Date: 2026-09-04 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d7281f9b3e'
down_revision: Union[str, Sequence[str], None] = 'e6799137d151'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('sessions') as batch_op:
        batch_op.add_column(sa.Column('version', sa.Integer(), nullable=False, server_default='1'))
    with op.batch_alter_table('memory_records') as batch_op:
        batch_op.add_column(sa.Column('version', sa.Integer(), nullable=False, server_default='1'))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('memory_records') as batch_op:
        batch_op.drop_column('version')
    with op.batch_alter_table('sessions') as batch_op:
        batch_op.drop_column('version')
