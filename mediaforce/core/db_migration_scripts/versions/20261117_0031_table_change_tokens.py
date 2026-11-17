from alembic import op

revision = "20261117_0031"
down_revision = "20261026_0030"
branch_labels = None
depends_on = None

TRACKED_TABLES = (
    "library_items",
    "staged_artifacts",
    "item_events",
    "plex_item_metadata",
    "series_metadata",
    "source_versions",
    "source_admissions",
    "source_measurements",
    "source_transitions",
    "source_moves",
    "target_production_outcomes",
    "motion_keep_decisions",
    "library_item_evidence_state",
)
CHANGES = ("insert", "update", "delete")


def upgrade() -> None:
    op.execute("CREATE TABLE IF NOT EXISTS table_change_tokens (table_name TEXT PRIMARY KEY, token BLOB NOT NULL)")
    for table in TRACKED_TABLES:
        for change in CHANGES:
            op.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_change_token_{change} AFTER {change.upper()} ON {table} BEGIN "
                f"INSERT INTO table_change_tokens (table_name, token) VALUES ('{table}', randomblob(16)) "
                "ON CONFLICT (table_name) DO UPDATE SET token = excluded.token; END"
            )
        op.execute(
            f"INSERT INTO table_change_tokens (table_name, token) VALUES ('{table}', randomblob(16)) "
            "ON CONFLICT (table_name) DO UPDATE SET token = excluded.token"
        )


def downgrade() -> None:
    for table in TRACKED_TABLES:
        for change in CHANGES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_change_token_{change}")
    op.execute("DROP TABLE IF EXISTS table_change_tokens")
