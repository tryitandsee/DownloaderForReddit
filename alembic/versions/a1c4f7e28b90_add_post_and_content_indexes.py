"""add post and content indexes

Revision ID: a1c4f7e28b90
Revises: df9e62309d01
Create Date: 2026-07-29 09:10:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "a1c4f7e28b90"
down_revision = "df9e62309d01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_post_sig_date", "post", ["significant_reddit_object_id", "date_posted"]
    )
    op.create_index("ix_content_post_id", "content", ["post_id"])


def downgrade():
    op.drop_index("ix_content_post_id", "content")
    op.drop_index("ix_post_sig_date", "post")
