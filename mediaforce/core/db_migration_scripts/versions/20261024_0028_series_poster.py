"""Remember each series' TMDB poster, so the Mac app can show its artwork."""

import sqlalchemy as sa
from alembic import op

revision = "20261024_0028"
down_revision = "20261022_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("series_metadata")}
    if "tmdb_poster_path" not in columns:
        with op.batch_alter_table("series_metadata") as batch:
            batch.add_column(sa.Column("tmdb_poster_path", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("series_metadata") as batch:
        batch.drop_column("tmdb_poster_path")
