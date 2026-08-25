"""wager settlement

Weekly wager amounts are deliberately not stored — they are derived from
gw_result, so an FPL points correction flows through and a rerun cannot
double-count. This table exists only to freeze a season's numbers once money
has actually changed hands.

Revision ID: c7d41a9f3b02
Revises: af2bc3e21a4b
Create Date: 2026-08-25 06:10:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c7d41a9f3b02'
down_revision = 'af2bc3e21a4b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'wager_settlement',
        sa.Column('league_id', sa.Integer(), nullable=False),
        sa.Column('season_end_event', sa.Integer(), nullable=False),
        sa.Column('balances', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('payments', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('settled_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('settled_by', sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint('league_id', 'season_end_event'),
    )


def downgrade() -> None:
    op.drop_table('wager_settlement')
