import re
from pathlib import PurePosixPath

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
    connection = op.get_bind()
    seasons = connection.execute(
        sa.text("""
        SELECT prefix, 1 AS identified FROM encode_jobs WHERE job_id = 'a51c7808ba77' AND job_kind = 'folder'
        UNION
        SELECT DISTINCT i.parent_dir, 0 FROM library_items i JOIN staged_artifacts s ON s.library_item_id = i.id
        WHERE s.promoted_at IS NOT NULL AND s.archived_source_path IS NOT NULL AND lower(i.parent_dir) LIKE '%/futurama%/season%6'
    """)
    )
    for prefix, identified in seasons:
        parts = PurePosixPath(prefix).parts
        if len(parts) == 3 and (
            identified
            or (
                re.fullmatch(r"Futurama(?: \(1999\))?(?: [\[{](?:tvdb|tmdb)[^\]}]*[\]}])?", parts[1], re.I)
                and re.fullmatch(r"Season\s*0*6", parts[2], re.I)
            )
        ):
            connection.execute(
                sa.text("INSERT OR IGNORE INTO season_redos(prefix, state) VALUES (:prefix, 'marked')"), {"prefix": prefix}
            )


def downgrade() -> None:
    op.drop_table("season_redos")
