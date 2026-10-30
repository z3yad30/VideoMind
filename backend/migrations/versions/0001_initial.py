"""Initial local-first relational schema.

Revision ID: 0001_initial
Revises:
"""
from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("username", sa.String(32), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("username = lower(username)", name="ck_users_username_lowercase"),
    )
    op.create_index("ix_users_username", "users", ["username"])
    op.create_table(
        "sessions",
        sa.Column("id_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("duration_s", sa.Float()),
        sa.Column("blob_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "videos",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_id", sa.String(32), sa.ForeignKey("media_assets.id", ondelete="SET NULL")),
        sa.Column("title", sa.String(255)),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_videos_user_id", "videos", ["user_id"])
    op.create_table(
        "chats",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("video_id", sa.String(32), sa.ForeignKey("videos.id", ondelete="SET NULL")),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("video_metadata", sa.JSON()),
        sa.Column("summary", sa.JSON()),
        sa.Column("transcript", sa.JSON()),
    )
    op.create_index("ix_chats_user_id", "chats", ["user_id"])
    op.create_index("ix_chats_video_id", "chats", ["video_id"])
    op.create_index("ix_chats_updated_at", "chats", ["updated_at"])
    op.create_table(
        "messages",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("chat_id", sa.String(32), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answer_audio_ref", sa.Text()),
        sa.Column("timestamps", sa.JSON(), nullable=False),
    )
    op.create_index("ix_messages_chat_id", "messages", ["chat_id"])
    op.create_table(
        "message_sources",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("message_id", sa.String(128), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("start_s", sa.Float(), nullable=False),
        sa.Column("end_s", sa.Float(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("message_id", "position"),
    )
    op.create_index("ix_message_sources_message_id", "message_sources", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_message_sources_message_id", table_name="message_sources")
    op.drop_table("message_sources")
    op.drop_index("ix_messages_chat_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_chats_updated_at", table_name="chats")
    op.drop_index("ix_chats_video_id", table_name="chats")
    op.drop_index("ix_chats_user_id", table_name="chats")
    op.drop_table("chats")
    op.drop_index("ix_videos_user_id", table_name="videos")
    op.drop_table("videos")
    op.drop_table("media_assets")
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
