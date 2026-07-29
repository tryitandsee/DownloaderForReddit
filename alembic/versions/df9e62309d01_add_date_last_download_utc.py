"""add date_last_download_utc

Revision ID: df9e62309d01
Revises: b838ef3372ca
Create Date: 2026-07-28 22:52:55.452940

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "df9e62309d01"
down_revision = "b838ef3372ca"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "reddit_object", sa.Column("date_last_download_utc", sa.DateTime(), nullable=True)
    )


def downgrade():
    with op.batch_alter_table("reddit_object") as batch:
        batch.drop_column("date_last_download_utc")
