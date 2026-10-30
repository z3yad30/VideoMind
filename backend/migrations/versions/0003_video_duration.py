"""Store source duration for per-user ingestion quotas."""

from alembic import op
import sqlalchemy as sa


revision = "0003_video_duration"
down_revision = "0002_durable_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("duration_s", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("videos", "duration_s")
