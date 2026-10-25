"""Keep each show's and movie's poster from Plex, and forget the TMDB poster path it replaces."""

import sqlalchemy as sa
from alembic import op

revision = "20261025_0029"
down_revision = "20261024_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "title_posters" not in inspector.get_table_names():
        op.create_table(
            "title_posters",
            sa.Column("title_prefix", sa.Text(), primary_key=True),
            sa.Column("plex_thumb", sa.Text(), nullable=False),
            sa.Column("observed_at", sa.Text(), nullable=False),
        )
    if "tmdb_poster_path" in {column["name"] for column in inspector.get_columns("series_metadata")}:
        with op.batch_alter_table("series_metadata") as batch:
            batch.drop_column("tmdb_poster_path")


def downgrade() -> None:
    with op.batch_alter_table("series_metadata") as batch:
        batch.add_column(sa.Column("tmdb_poster_path", sa.Text(), nullable=True))
    op.drop_table("title_posters")
