import json
from pathlib import Path

from sqlalchemy import select

from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import calibration_jobs


def add_sample(connection: DBClient, job_id: str, *, prefix: str = 'tv/Show', status: str = 'completed',
               lane: str = 'sample', summary: str | None = None) -> dict:
    selection = {'coverage_summary': summary, 'coverage': {'untested_groups': [{'label': 'Season 2'}]}}
    item = {'rel_path': f'{prefix}/Episode.mkv', 'source_path': '/private/source.mkv',
            'representative_selection': selection}
    connection.execute(calibration_jobs.insert().values(
        job_id=job_id, prefix=prefix, status=status, lane=lane, action='baseline',
        host_json='{}', policy_json='{}', sample_item_json=json.dumps(item),
        created_at='2026-10-01T00:00:00Z', updated_at='2026-10-01T00:00:00Z',
        finished_at='2026-10-01T01:00:00Z',
    ))
    return selection


def test_history_keeps_original_coverage_and_pages_only_finished_samples(tmp_path: Path) -> None:
    from mediaforce.web.runtime.sample_history import sample_history_payload

    path = tmp_path / 'library.sqlite3'
    with open_db(path) as connection:
        original = add_sample(connection, 'original', summary='Original stored coverage')
        add_sample(connection, 'newer', status='pending_review', summary='Newer stored coverage')
        add_sample(connection, 'other', prefix='tv/Showcase', summary='Other show')
        add_sample(connection, 'season', prefix='tv/Show/Season 1', summary='Other scope')
        add_sample(connection, 'running', status='running', summary='Not finished')
        add_sample(connection, 'failed', status='failed', summary='Failed')
        add_sample(connection, 'full', lane='full', summary='Full encode')
        before = connection.execute(select(calibration_jobs)).mappings().all()
    first = sample_history_payload(path, 'tv/Show', 0, 1)
    second = sample_history_payload(path, 'tv/Show', 1, 1)
    assert first['total'] == second['total'] == 2
    assert first['samples'][0]['job_id'] == 'newer'
    assert second['samples'][0]['job_id'] == 'original'
    assert second['samples'][0]['selection'] == original
    assert 'source_path' not in second['samples'][0]
    with open_db(path) as connection:
        assert connection.execute(select(calibration_jobs)).mappings().all() == before


def test_legacy_sample_has_no_invented_coverage(tmp_path: Path) -> None:
    from mediaforce.web.runtime.sample_history import sample_history_payload

    path = tmp_path / 'library.sqlite3'
    with open_db(path) as connection:
        add_sample(connection, 'legacy')
    result = sample_history_payload(path, 'tv/Show', -1, 500)
    assert result['offset'] == 0
    assert result['samples'][0]['selection']['coverage_summary'] is None
    assert sample_history_payload(path, 'tv/Empty', 0, 10)['samples'] == []


def test_real_app_serves_saved_coverage_over_http(tmp_path: Path) -> None:
    import asyncio
    from unittest.mock import patch

    from mediaforce.core.config import ConfigPaths, MediaforceConfig
    from mediaforce.web import app as web_app

    config = MediaforceConfig(raw={}, paths=ConfigPaths(
        project_root=tmp_path, config_path=tmp_path / 'config.toml', db_path=tmp_path / 'library.sqlite3',
        run_manifest_dir=tmp_path / 'runs', web_state_dir=tmp_path / 'web', review_dir=tmp_path / 'review',
        runtime_settings_path=tmp_path / 'settings.json',
    ))
    with patch.dict(vars(web_app), load_config=lambda _path: config):
        app = web_app.create_app(config.paths.config_path)
    assert any(getattr(route, 'path', '') == '/api/folders/{prefix:path}/sample-history' for route in app.routes)
    with open_db(config.paths.db_path) as connection:
        selection = add_sample(connection, 'original', summary='Original historical coverage')
    messages = []

    async def request() -> None:
        async def receive() -> dict:
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message: dict) -> None:
            messages.append(message)

        await app({
            'type': 'http', 'http_version': '1.1', 'method': 'GET', 'scheme': 'http',
            'path': '/api/folders/tv/Show/sample-history', 'query_string': b'offset=0&limit=1',
            'root_path': '', 'headers': [], 'server': ('fixture', 80), 'client': ('fixture', 1),
        }, receive, send)

    asyncio.run(request())
    assert messages[0]['status'] == 200
    assert (b'cache-control', b'no-store') in messages[0]['headers']
    payload = json.loads(b''.join(message.get('body', b'') for message in messages))
    assert payload['samples'][0]['selection'] == selection
    assert payload['samples'][0]['job_id'] == 'original'
