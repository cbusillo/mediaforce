CREATE TABLE IF NOT EXISTS scan_runs
(
    scan_id         TEXT PRIMARY KEY,
    started_at      TEXT    NOT NULL,
    completed_at    TEXT,
    roots_json      TEXT    NOT NULL,
    file_count      INTEGER NOT NULL DEFAULT 0,
    reprobed_count  INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0
);

ALTER TABLE scan_runs ADD COLUMN scope TEXT NOT NULL DEFAULT 'unknown';
UPDATE scan_runs SET scope = 'unknown' WHERE scope = 'full';
