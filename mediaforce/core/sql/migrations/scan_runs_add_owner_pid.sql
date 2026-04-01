CREATE TABLE IF NOT EXISTS scan_runs
(
    scan_id         TEXT PRIMARY KEY,
    started_at      TEXT    NOT NULL,
    completed_at    TEXT,
    roots_json      TEXT    NOT NULL,
    scope           TEXT    NOT NULL DEFAULT 'unknown',
    prefixes_json   TEXT,
    file_count      INTEGER NOT NULL DEFAULT 0,
    reprobed_count  INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0
);

ALTER TABLE scan_runs ADD COLUMN owner_pid INTEGER;
