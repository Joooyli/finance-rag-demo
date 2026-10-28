import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import api, config, web_ui
from app.document_schema import DocumentSource, file_hash


class FakeManager:
    def __init__(self):
        self.parses, self.indexes = [], []

    def submit_parse(self, source, **kwargs):
        self.parses.append((source, kwargs))
        return {"job_id": "job-parse"}

    def submit_index(self, run_id):
        self.indexes.append(run_id)
        return {"job_id": "job-index"}

    def get_status(self, job_id):
        return {"job_id": job_id, "status": "running"} if job_id == "exists" else None

    def list_jobs(self):
        return []

    def cancel(self, job_id):
        if job_id != "exists":
            raise KeyError(job_id)
        return {"job_id": job_id, "status": "cancelling"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    manager = FakeManager()
    monkeypatch.setattr(api, "get_job_manager", lambda: manager)
    monkeypatch.setattr(api, "shutdown_job_manager", lambda: None)
    monkeypatch.setattr(web_ui, "get_job_manager", lambda: manager)
    monkeypatch.setattr(config, "PARSE_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "PARSE_LOG_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DOCUMENT_DIR", tmp_path / "documents")
    monkeypatch.setattr(api, "collection_count", lambda: 7)
    monkeypatch.setattr(web_ui, "list_active_chunks", lambda: [])
    monkeypatch.setattr(web_ui, "list_runs", lambda **kwargs: [])
    with TestClient(api.app) as http:
        yield http, manager


def test_upload_is_background_and_index_true_still_requires_review(client, monkeypatch, tmp_path):
    http, manager = client
    calls = []

    def prepare(path, **kwargs):
        calls.append((path, kwargs, path.read_bytes()))
        return DocumentSource("doc-new", kwargs["source"], kwargs["source_key"], "sha", "fixed.pdf")

    monkeypatch.setattr(api, "prepare_source", prepare)
    response = http.post('/documents/index?index=true', files={'file': ('same.pdf', b'%PDF-1.4 fixture', 'application/pdf')})
    assert response.status_code == 202
    assert response.json()['index_requested'] is True
    assert response.json()['doc_id'] == 'doc-new'
    assert manager.parses[0][1]['index_requested'] is True
    assert not manager.indexes
    assert calls[0][1]['source_key'].startswith('upload:')
    assert not calls[0][0].exists()


def test_invalid_upload_is_rejected_and_temporary_file_removed(client):
    http, manager = client
    response = http.post('/documents/index', files={'file': ('fake.pdf', b'not a PDF')})
    assert response.status_code == 400 and not manager.parses
    assert not list((config.PARSE_JOBS_DIR / 'uploads').glob('*.pdf'))


def test_index_endpoint_validates_review_before_queueing(client, monkeypatch):
    http, manager = client

    def fail(run_id):
        raise ValueError('review pending')

    monkeypatch.setattr(api, 'validate_indexable_run', fail)
    response = http.post('/runs/run/index')
    assert response.status_code == 409 and not manager.indexes
    monkeypatch.setattr(api, 'validate_indexable_run', lambda run_id: ({}, [], {}))
    assert http.post('/runs/run/index').status_code == 202
    assert manager.indexes == ['run']


def test_review_uses_human_payload_and_job_status_has_404(client, monkeypatch):
    http, _ = client
    calls = []
    monkeypatch.setattr(api, 'approve_run', lambda run_id, **kwargs: calls.append((run_id, kwargs)) or {'decision': kwargs['decision']})
    response = http.post('/runs/run/review', json={'reviewer': 'Joy', 'decision': 'approved', 'notes': 'checked'})
    assert response.status_code == 200 and calls[0][1]['reviewer'] == 'Joy'
    assert http.get('/jobs/exists').json()['status'] == 'running'
    assert http.get('/jobs/missing').status_code == 404
    assert http.post('/jobs/missing/cancel').status_code == 404
    assert http.get('/health').json()['indexed_chunks'] == 7


def _record(identity='vector-1', pages=None):
    pages = pages or [28, 29]
    metadata = {'vector_record_id': identity, 'chunk_id': 'logical-1', 'source': 'sample.pdf',
                'doc_id': 'doc', 'chunk_index': 0, 'content_type': 'table', 'engine': 'mineru'}
    return {**metadata, 'metadata': metadata, 'pages': pages, 'text': 'table value 42'}


def test_ui_uses_active_records_and_exact_page_filter(client, monkeypatch):
    http, _ = client
    monkeypatch.setattr(web_ui, 'list_active_chunks', lambda: [_record(), _record('vector-2', [28, 30])])
    response = http.get('/ui/chunks?page=29')
    assert response.status_code == 200
    assert '/ui/chunks/vector-1' in response.text and '/ui/chunks/vector-2' not in response.text
    assert '28–29' in response.text
    assert http.get('/ui/chunks/vector-1').status_code == 200
    assert http.get('/ui/chunks/logical-1').status_code == 404
    assert http.get('/ui/chunks?page=').status_code == 200
    assert http.get('/ui/chunks?page=invalid').status_code == 400


def test_table_preview_removes_active_content():
    output = str(web_ui._safe_table('<table onclick="bad()"><tr><td rowspan="2">1<eq>x+y</eq><img src="evil"></td></tr></table><script>alert(1)</script>'))
    assert '<table>' in output and 'rowspan="2"' in output
    assert 'onclick' not in output and '<script' not in output and 'alert(1)' not in output
    assert '<img' not in output and '<code>x+y</code>' in output


def test_assets_cannot_escape_run_and_html_downloads(client, monkeypatch):
    http, _ = client
    root = config.PARSE_LOG_DIR / 'run'
    root.mkdir(parents=True)
    (root / 'table.html').write_text('<script>bad()</script>', encoding='utf-8')
    (config.PARSE_LOG_DIR / 'outside.txt').write_text('secret', encoding='utf-8')
    monkeypatch.setattr(web_ui, 'load_run', lambda run_id: {'run_id': run_id})
    response = http.get('/ui/runs/run/assets/table.html')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/octet-stream'
    assert 'attachment' in response.headers['content-disposition']
    assert http.get('/ui/runs/run/assets/%2E%2E%2Foutside.txt').status_code == 404


def test_original_source_requires_immutable_root_and_matching_hash(client, monkeypatch):
    http, _ = client
    source = config.DOCUMENT_DIR / 'doc' / 'sha' / 'source.pdf'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'%PDF-fixture')
    meta = {'pdf_path': str(source), 'pdf_sha256': file_hash(source), 'source': 'sample.pdf'}
    monkeypatch.setattr(web_ui, 'load_run', lambda run_id: meta)
    assert http.get('/ui/runs/run/source').status_code == 200
    source.write_bytes(b'changed')
    assert http.get('/ui/runs/run/source').status_code == 409


def test_run_page_renders_independent_states_structure_and_review_form(client, monkeypatch):
    http, _ = client
    root = config.PARSE_LOG_DIR / 'run'
    root.mkdir(parents=True)
    meta = {'run_id': 'run', 'source': 'sample.pdf', 'schema_version': 3,
            'parse_status': 'completed', 'quality_status': 'pass', 'review_status': 'pending',
            'index_status': 'not_indexed', 'counts': {'pages_total': 1, 'chunks': 1}}
    monkeypatch.setattr(web_ui, 'load_run', lambda run_id: meta)
    page = {'page': 1, 'text': 'value', 'route': 'mineru', 'quality_status': 'pass'}
    chunk = {'chunk_id': 'chunk', 'pages': [1], 'embedding_text': 'value 42', 'source_spans': []}
    block = {'block_id': 'block', 'page': 1, 'type': 'table', 'content_text': '42',
             'quality_status': 'pass', 'html': '<table><tr><td>42</td></tr></table>'}
    for name, records in [('pages.jsonl', [page]), ('chunks.jsonl', [chunk]),
                          ('blocks.jsonl', [block]), ('logical_blocks.jsonl', []), ('merges.jsonl', [])]:
        (root / name).write_text(''.join(json.dumps(item) + '\n' for item in records), encoding='utf-8')
    response = http.get('/ui/runs/run')
    assert response.status_code == 200
    assert 'completed' in response.text and 'pending' in response.text and 'not_indexed' in response.text
    assert '<td>42</td>' in response.text and 'value 42' in response.text
    assert 'action="/ui/runs/run/review"' in response.text
