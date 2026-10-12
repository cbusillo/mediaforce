"""Remember TMDB genres for each series, so a show can be offered a fitting sound choice."""

import sqlalchemy as sa
from alembic import op

revision = "20261012_0024"
down_revision = "20260929_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("series_metadata")}
    if "tmdb_genres_json" not in columns:
        with op.batch_alter_table("series_metadata") as batch:
            batch.add_column(sa.Column("tmdb_genres_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("series_metadata") as batch:
        batch.drop_column("tmdb_genres_json")
