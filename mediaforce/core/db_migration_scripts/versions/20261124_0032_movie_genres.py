import sqlalchemy as sa
from alembic import op

revision = "20261124_0032"
down_revision = "20261117_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = sa.inspect(op.get_bind()).get_columns("plex_item_metadata")
    if not any(column["name"] == "plex_genres_json" for column in columns):
        op.add_column("plex_item_metadata", sa.Column("plex_genres_json", sa.Text()))


def downgrade() -> None:
    op.drop_column("plex_item_metadata", "plex_genres_json")
