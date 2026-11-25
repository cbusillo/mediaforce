import sqlalchemy as sa
from alembic import op

revision = "20261125_0033"
down_revision = "20261124_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("season_redos"):
        op.create_table(
            "season_redos",
            sa.Column("prefix", sa.Text(), primary_key=True),
            sa.Column("state", sa.Text(), nullable=False),
            sa.Column("remaining_json", sa.Text()),
        )
    op.execute("""
        INSERT OR IGNORE INTO season_redos(prefix, state)
        SELECT prefix, 'marked' FROM encode_jobs WHERE job_id = 'a51c7808ba77' AND job_kind = 'folder'
    """)


def downgrade() -> None:
    op.drop_table("season_redos")
