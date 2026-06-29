"""add webhook_deliveries

Phase 1 HMAC webhook auth: a ledger of accepted signed deliveries so a replayed
delivery (same X-Sentinel-Event-Id) is a no-op. event_id is UNIQUE - the delivery
identity, distinct from a deployment's external_id.

Revision ID: a1b2c3d4e5f6
Revises: e96f69f637c4
Create Date: 2026-06-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'e96f69f637c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'webhook_deliveries',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('event_id', sa.Text(), nullable=False),
        sa.Column('deployment_id', sa.UUID(), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], name=op.f('fk_webhook_deliveries_deployment_id_deployments')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_deliveries')),
        sa.UniqueConstraint('event_id', name='uq_webhook_deliveries_event_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('webhook_deliveries')
