"""Add lifecycle metadata for Plex and series policy decisions.

Revision ID: 20260712_0008
Revises: 20260711_0007
Create Date: 2026-07-12 17:30:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260712_0008"
down_revision = "20260711_0007"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(str(column.get("name")) == column_name for column in inspector.get_columns(table_name))


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_column("library_items", "content_version_changed_at"):
        op.add_column("library_items", sa.Column("content_version_changed_at", sa.Text(), nullable=True))
        op.execute(
            "UPDATE library_items "
            "SET content_version_changed_at = discovered_at "
            "WHERE content_version_changed_at IS NULL"
        )

    if not _has_table("plex_item_metadata"):
        op.create_table(
            "plex_item_metadata",
            sa.Column(
                "library_item_id",
                sa.Integer(),
                sa.ForeignKey("library_items.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("plex_server_id", sa.Text(), nullable=False),
            sa.Column("plex_item_rating_key", sa.Text(), nullable=False),
            sa.Column("plex_part_id", sa.Text(), nullable=True),
            sa.Column("plex_show_rating_key", sa.Text(), nullable=True),
            sa.Column("plex_season_index", sa.Integer(), nullable=True),
            sa.Column("plex_added_at", sa.Text(), nullable=True),
            sa.Column("plex_part_path", sa.Text(), nullable=False),
            sa.Column("observed_at", sa.Text(), nullable=False),
        )
        op.create_index("idx_plex_item_metadata_show", "plex_item_metadata", ["plex_show_rating_key"])

    if not _has_table("series_metadata"):
        op.create_table(
            "series_metadata",
            sa.Column("series_prefix", sa.Text(), primary_key=True),
            sa.Column("plex_server_id", sa.Text(), nullable=True),
            sa.Column("plex_show_rating_key", sa.Text(), nullable=True),
            sa.Column("plex_guids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("plex_observed_at", sa.Text(), nullable=True),
            sa.Column("tmdb_series_id", sa.Integer(), nullable=True),
            sa.Column("tmdb_status", sa.Text(), nullable=True),
            sa.Column("tmdb_in_production", sa.Integer(), nullable=True),
            sa.Column("tmdb_observed_at", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.Text(), nullable=False),
        )
        op.create_index("idx_series_metadata_tmdb", "series_metadata", ["tmdb_series_id"])


def downgrade() -> None:
    if _has_table("series_metadata"):
        op.drop_index("idx_series_metadata_tmdb", table_name="series_metadata")
        op.drop_table("series_metadata")
    if _has_table("plex_item_metadata"):
        op.drop_index("idx_plex_item_metadata_show", table_name="plex_item_metadata")
        op.drop_table("plex_item_metadata")
    if _has_column("library_items", "content_version_changed_at"):
        op.drop_column("library_items", "content_version_changed_at")
