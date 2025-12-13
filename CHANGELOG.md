# Changelog

## 0.3.0 (2025-12-13)

- Promotion safety: verify-before-promote (ffprobe sanity checks), atomic staging, and rollbackable promotion.
- Worker coordination: API-backed claim/release/progress/report so workers can run without direct SQLite access.
- Quality loop: motion-weighted VMAF sampling with persisted reasoning + "flag bad choice" feedback.
- Config hardening: `show_config.json` removed as a runtime config surface (explicit import only).
- Operations: purge promotion backups CLI (dry-run by default), CI quality gates (ruff+mypy+pytest), and structured JSON logs.

