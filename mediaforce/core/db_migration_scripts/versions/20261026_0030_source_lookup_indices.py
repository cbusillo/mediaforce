from alembic import op

revision = "20261026_0030"
down_revision = "20261025_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_source_admissions_version", "source_admissions", ["version_id"], if_not_exists=True)
    op.create_index("idx_source_transitions_item", "source_transitions", ["library_item_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("idx_source_transitions_item", table_name="source_transitions")
    op.drop_index("idx_source_admissions_version", table_name="source_admissions")
