"""gw_result.provisional

Two-stage settlement: a gameweek is written once every fixture is complete and
bonus has been folded into player totals (provisional), then rewritten and
un-flagged when FPL sets data_checked (confirmed).

Existing rows were all written on the old data_checked-only trigger, so they
are confirmed by definition — hence server_default false. Adding a NOT NULL
column with a constant default is a metadata-only change on PostgreSQL 11+, so
this does not rewrite the table and does not lock it for any meaningful time.

Revision ID: e5f1a2c8d904
Revises: c7d41a9f3b02
Create Date: 2026-09-07 06:20:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5f1a2c8d904'
down_revision = 'c7d41a9f3b02'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'gw_result',
        sa.Column('provisional', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
    )


def downgrade() -> None:
    op.drop_column('gw_result', 'provisional')
