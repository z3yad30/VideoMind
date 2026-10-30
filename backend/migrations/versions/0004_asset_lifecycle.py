"""Add shared-media asset state and missing-video chat marker."""

from alembic import op
import sqlalchemy as sa


revision = "0004_asset_lifecycle"
down_revision = "0003_video_duration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "media_assets",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
    )
    op.add_column(
        "chats",
        sa.Column("video_missing", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("chats", "video_missing")
    op.drop_column("media_assets", "status")
