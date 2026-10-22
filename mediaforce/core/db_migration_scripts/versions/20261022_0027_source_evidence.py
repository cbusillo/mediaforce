"""Retain source versions, measurements and publication moves independently of the catalog."""

import hashlib
import json
from uuid import uuid4

from alembic import op
from sqlalchemy import Column, CheckConstraint, ForeignKey, ForeignKeyConstraint, Index
from sqlalchemy import Integer, MetaData, Table, Text, UniqueConstraint, inspect, text

revision = "20261022_0027"
down_revision = "20261018_0026"
branch_labels = None
depends_on = None

metadata = MetaData()
Table("library_items", metadata, Column("id", Integer, primary_key=True))

source_generations = Table(
    "source_generations", metadata,
    Column("generation_id", Text, primary_key=True),
    Column("created_at", Text, nullable=False),
)

source_versions = Table(
    "source_versions", metadata,
    Column("version_id", Text, primary_key=True),
    Column("generation_id", Text, ForeignKey("source_generations.generation_id"), nullable=False),
    Column("role", Text, nullable=False),
    Column("sha256", Text),
    Column("size_bytes", Integer),
    Column("sample_fingerprint", Text),
    Column("video_codec", Text),
    Column("identity_json", Text, nullable=False),
    Column("profile_json", Text, nullable=False),
    Column("observed_at", Text, nullable=False),
    CheckConstraint("role IN ('unknown', 'input', 'output')"),
    UniqueConstraint("version_id", "generation_id"),
)

source_admissions = Table(
    "source_admissions", metadata,
    Column("admission_id", Text, primary_key=True),
    Column("generation_id", Text, ForeignKey("source_generations.generation_id"), nullable=False),
    Column("version_id", Text, nullable=False),
    Column("tier", Text, nullable=False),
    Column("basis", Text, nullable=False),
    Column("checks_json", Text, nullable=False),
    Column("confirmation_ref", Text),
    Column("supersedes_id", Text, ForeignKey("source_admissions.admission_id")),
    Column("recorded_at", Text, nullable=False),
    ForeignKeyConstraint(["version_id", "generation_id"], ["source_versions.version_id", "source_versions.generation_id"]),
    CheckConstraint("tier IN ('proven', 'probable', 'unknown')"),
    CheckConstraint("tier != 'proven' OR confirmation_ref IS NOT NULL"),
    UniqueConstraint("supersedes_id"),
)

source_measurements = Table(
    "source_measurements", metadata,
    Column("measurement_id", Text, primary_key=True),
    Column("version_id", Text, ForeignKey("source_versions.version_id")),
    Column("kind", Text, nullable=False),
    Column("binding", Text, nullable=False),
    Column("summary_json", Text, nullable=False),
    Column("summary_sha256", Text, nullable=False),
    Column("provenance_json", Text, nullable=False),
    Column("measured_at", Text),
    Column("recorded_at", Text, nullable=False),
    CheckConstraint("version_id IS NOT NULL OR binding = 'unbound'"),
    CheckConstraint("kind IN ('cadence_analysis', 'media_fingerprint')"),
)
Index("idx_source_measurements_version", source_measurements.c.version_id, source_measurements.c.kind)

source_transitions = Table(
    "source_transitions", metadata,
    Column("operation_id", Text, primary_key=True),
    Column("library_item_id", Integer, ForeignKey("library_items.id", ondelete="SET NULL")),
    Column("before_version_id", Text, ForeignKey("source_versions.version_id"), nullable=False),
    Column("after_version_id", Text, ForeignKey("source_versions.version_id"), nullable=False),
    Column("expected_revision", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("evidence_json", Text, nullable=False),
    Column("recorded_at", Text, nullable=False),
    CheckConstraint("outcome IN ('prepared', 'committed', 'rolled_back', 'unresolved')"),
)
Index(
    "uq_source_transition_pending", source_transitions.c.library_item_id, unique=True,
    sqlite_where=source_transitions.c.outcome.in_(("prepared", "unresolved")),
)

source_moves = Table(
    "source_moves", metadata,
    Column("operation_id", Text, ForeignKey("source_transitions.operation_id"), primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    Column("version_id", Text, ForeignKey("source_versions.version_id"), nullable=False),
    Column("from_path", Text, nullable=False),
    Column("to_path", Text, nullable=False),
    Column("phase", Text, nullable=False),
    Column("checks_json", Text, nullable=False),
)


def upgrade() -> None:
    connection = op.get_bind()
    for table in metadata.sorted_tables:
        if table.name != "library_items":
            table.create(connection, checkfirst=True)
    columns = {column["name"] for column in inspect(connection).get_columns("library_items")}
    if "active_source_version_id" not in columns:
        op.execute("ALTER TABLE library_items ADD COLUMN active_source_version_id TEXT REFERENCES source_versions(version_id)")
        op.add_column("library_items", Column("source_revision", Integer, nullable=False, server_default="0"))
    rows = connection.execute(text("SELECT * FROM library_items WHERE active_source_version_id IS NULL")).mappings().all()
    for row in rows:
        generation_id, version_id = uuid4().hex, uuid4().hex
        recorded_at = row["updated_at"]
        connection.execute(source_generations.insert().values(generation_id=generation_id, created_at=recorded_at))
        profile = {key: row[key] for key in ("video_codec", "width", "height", "duration_seconds", "audio_summary_json")}
        identity = {"path": row["source_path"], "mtime_ns": row["mtime_ns"], "strength": "catalog-unverified"}
        connection.execute(source_versions.insert().values(
            version_id=version_id, generation_id=generation_id, role="unknown",
            size_bytes=row["size_bytes"], sample_fingerprint=row["content_version_fingerprint"],
            video_codec=row["video_codec"], identity_json=json.dumps(identity), profile_json=json.dumps(profile),
            observed_at=recorded_at,
        ))
        connection.execute(text("UPDATE library_items SET active_source_version_id=:version WHERE id=:item"),
                           {"version": version_id, "item": row["id"]})
        for kind, column in (("cadence_analysis", "cadence_summary_json"), ("media_fingerprint", "media_fingerprint_json")):
            summary = row[column]
            if summary is None:
                continue
            connection.execute(source_measurements.insert().values(
                measurement_id=uuid4().hex, kind=kind, binding="unbound", summary_json=summary,
                summary_sha256=hashlib.sha256(summary.encode()).hexdigest(),
                provenance_json=json.dumps({"origin": "legacy-catalog", "library_item_id": row["id"],
                                            "reason": "unverified historical measurement"}),
                recorded_at=recorded_at,
            ))
    for table in (source_generations, source_versions, source_admissions, source_measurements):
        for operation in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER IF NOT EXISTS {table.name}_no_{operation.lower()} "
                       f"BEFORE {operation} ON {table.name} BEGIN "
                       f"SELECT RAISE(ABORT, '{table.name} is append-only'); END")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SELECT 1 FROM source_versions LIMIT 1")).first() is not None:
        raise RuntimeError("Cannot remove retained source evidence")
    op.drop_column("library_items", "source_revision")
    op.drop_column("library_items", "active_source_version_id")
    for table in reversed(metadata.sorted_tables):
        if table.name != "library_items":
            table.drop(connection)
