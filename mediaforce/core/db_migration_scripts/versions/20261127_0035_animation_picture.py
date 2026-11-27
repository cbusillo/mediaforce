import sqlalchemy as sa
from alembic import op

revision = "20261127_0035"
down_revision = "20261125_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("animation_sorts")}
    if "choice" in columns:
        op.drop_column("animation_sorts", "choice")
        op.drop_column("animation_sorts", "moment_json")
        op.drop_column("animation_sorts", "kbps")
        op.add_column("animation_sorts", sa.Column("median_score", sa.REAL()))
        op.add_column("animation_sorts", sa.Column("hdr", sa.Boolean()))
        op.execute("DELETE FROM library_item_evidence_state WHERE evidence_kind = 'animation_bitrate'")


def downgrade() -> None:
    op.drop_column("animation_sorts", "median_score")
    op.add_column("animation_sorts", sa.Column("kbps", sa.REAL()))
    op.drop_column("animation_sorts", "hdr")
    op.add_column("animation_sorts", sa.Column("choice", sa.Text()))
    op.add_column("animation_sorts", sa.Column("moment_json", sa.Text()))
    op.execute("DELETE FROM library_item_evidence_state WHERE evidence_kind = 'animation_picture'")
