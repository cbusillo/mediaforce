"""Read saved sample coverage without selecting or measuring another file."""

import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, literal_column, select

from mediaforce.core.db import open_readonly_db
from mediaforce.core.db_tables import calibration_jobs
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.calibration_jobs import SUCCESSFUL_SAMPLE_JOB_STATUSES


def sample_history_payload(db_path: Path, prefix: str, offset: int = 0, limit: int = 10) -> dict[str, Any]:
    offset = max(0, offset)
    limit = max(1, min(100, limit))
    conditions = (
        calibration_jobs.c.prefix == prefix,
        calibration_jobs.c.lane == "sample",
        calibration_jobs.c.status.in_(SUCCESSFUL_SAMPLE_JOB_STATUSES),
        calibration_jobs.c.error.is_(None),
        calibration_jobs.c.finished_at.is_not(None),
    )
    with open_readonly_db(db_path) as connection:
        connection.exec_driver_sql("BEGIN")
        total = connection.execute(select(func.count()).select_from(calibration_jobs).where(*conditions)).scalar_one()
        rows = connection.execute(
            select(
                calibration_jobs.c.job_id,
                calibration_jobs.c.finished_at,
                calibration_jobs.c.sample_item_json,
            ).where(*conditions).order_by(
                calibration_jobs.c.finished_at.desc(),
                calibration_jobs.c.created_at.desc(), literal_column("rowid").desc(),
            ).offset(offset).limit(limit)
        ).mappings().all()
    samples = []
    for row in rows:
        item = object_dict(json.loads(row["sample_item_json"]))
        samples.append({
            "job_id": row["job_id"],
            "finished_at": row["finished_at"],
            "file_name": str(item.get("rel_path") or "").rsplit("/", 1)[-1],
            "selection": {
                key: value for key, value in object_dict(item.get("representative_selection")).items()
                if key in {"coverage_summary", "coverage"}
            },
        })
    return {"prefix": prefix, "offset": offset, "limit": limit, "total": total, "samples": samples}
