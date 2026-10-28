from __future__ import annotations

import json
import logging
from collections import Counter
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app import config
from app.document_schema import file_hash, pages_label
from app.parse_jobs import get_job_manager
from app.parse_log import list_runs, load_run, load_run_chunks, load_run_pages, resolve_run_dir
from app.parse_review import approve_run, validate_indexable_run
from app.vector_store import list_active_chunks

logger = logging.getLogger(__name__)
TEMPLATES_DIR = Path(__file__).resolve().parent / 'templates'
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix='/ui', tags=['ui'], include_in_schema=False)
MAX_PAGE_SIZE = 200
_IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.webp': 'image/webp', '.gif': 'image/gif'}


class _SafeTable(HTMLParser):
    """只重建表格结构与数字跨度，不信任模型输出中的脚本、样式、URL或事件属性。"""
    allowed = {'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'caption', 'colgroup', 'col'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
            return
        if self.hidden:
            return
        if tag in self.allowed:
            spans = ''.join(f' {key}="{value}"' for key, value in attrs
                            if key in {'rowspan', 'colspan'} and value and value.isascii()
                            and value.isdigit() and 1 <= int(value) <= 1000)
            self.parts.append(f'<{tag}{spans}>')
        elif tag == 'br':
            self.parts.append('<br>')
        elif tag == 'img':
            self.parts.append('[内嵌图片，见区域资产]')
        elif tag == 'eq':
            self.parts.append('<code>')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in self.allowed:
            self.parts.append(f'</{tag}>')
        elif not self.hidden and tag == 'eq':
            self.parts.append('</code>')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(escape(data))


def _safe_table(html):
    parser = _SafeTable()
    parser.feed(html)
    return Markup(''.join(parser.parts))


def _row(record):
    metadata = record.get('metadata') or {}
    row = {**metadata, **record}
    pages = row.get('pages')
    if pages is None:
        try:
            pages = json.loads(row.get('pages_json', '[]'))
        except (TypeError, ValueError):
            pages = []
    if not pages and row.get('page'):
        pages = [row['page']]
    row.update(metadata=metadata, pages=pages or [], page_label=pages_label(pages or []),
               text=row.get('text', ''), chars=len(row.get('text', '')))
    return row


def _active_rows():
    # UI列表、详情、来源分布和计数共用同一次活动快照读取，不直接查询整个Chroma集合。
    return [_row(record) for record in list_active_chunks()]


def _source_counts(rows):
    return sorted(Counter(row.get('source', 'unknown') for row in rows).items())


def _run_or_404(run_id):
    try:
        run = load_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail='解析记录不存在')
    return run


def _inside_run(run_id, relative_path):
    try:
        root = resolve_run_dir(run_id).resolve()
        path = (root / relative_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('资产路径越界或文件不存在')
        return path
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=404, detail='资产不存在') from exc


def _records(run_id, name):
    root = resolve_run_dir(run_id)
    path = root / name
    if not path.is_file():
        return []
    try:
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=f'产物损坏: {name}') from exc


def _block_preview(request, run_id, block):
    row = dict(block)
    html = block.get('html', '')
    if not html and block.get('html_ref'):
        html = _inside_run(run_id, block['html_ref']).read_text(encoding='utf-8')
    row['table_preview'] = _safe_table(html) if html else None
    assets = []
    for reference in block.get('asset_refs', []):
        if not isinstance(reference, str):
            continue
        _inside_run(run_id, reference)
        assets.append({'path': reference, 'is_image': Path(reference).suffix.lower() in _IMAGE_TYPES,
                       'url': str(request.url_for('ui_run_asset', run_id=run_id, asset_path=reference))})
    row['preview_assets'] = assets
    return row


@router.get('', response_class=HTMLResponse)
def ui_home(request: Request):
    rows, runs = _active_rows(), list_runs(limit=10)
    return templates.TemplateResponse(request, 'home.html', {
        'collection_name': config.COLLECTION_NAME, 'chunk_total': len(rows),
        'sources': _source_counts(rows), 'runs': runs, 'jobs': get_job_manager().list_jobs(),
        'parse_log_dir': str(config.PARSE_LOG_DIR), 'indexing_enabled': config.ENABLE_INDEXING,
        'parse_log_enabled': config.PARSE_LOG_ENABLED})


@router.get('/chunks', response_class=HTMLResponse)
def ui_chunks(request: Request, q: str = '', source: str = '', page: str = '',
              limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE), offset: int = Query(default=0, ge=0)):
    try:
        page_number = int(page) if page else None
        if page_number is not None and page_number < 1:
            raise ValueError('页号必须大于0')
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='来源页必须是正整数') from exc
    rows = _active_rows()
    filtered = [row for row in rows if (not source or row.get('source') == source)
                and (page_number is None or page_number in row['pages']) and (not q or q in row['text'])]
    return templates.TemplateResponse(request, 'chunks.html', {
        'rows': filtered[offset:offset + limit], 'filtered_total': len(filtered),
        'total_in_collection': len(rows), 'sources': _source_counts(rows),
        'q': q, 'source': source, 'page': page, 'limit': limit, 'offset': offset,
        'prev_offset': max(0, offset - limit) if offset else None,
        'next_offset': offset + limit if offset + limit < len(filtered) else None})


@router.get('/chunks/{vector_record_id}', response_class=HTMLResponse)
def ui_chunk_detail(request: Request, vector_record_id: str):
    rows = _active_rows()
    record = next((row for row in rows if row.get('vector_record_id') == vector_record_id), None)
    if record is None:
        raise HTTPException(status_code=404, detail='当前活动索引中没有此切块')
    siblings = [row for row in rows if row.get('doc_id') == record.get('doc_id') and
                (set(row['pages']) & set(record['pages']) or
                 (record.get('parent_id') and row.get('parent_id') == record['parent_id']))]
    chunks, blocks = [], []
    run_id = record.get('run_id')
    if run_id:
        _run_or_404(run_id)
        chunks = load_run_chunks(run_id)
        chunk = next((item for item in chunks if item.get('chunk_id') == record.get('chunk_id')), {})
        related = set(chunk.get('block_ids', []))
        blocks = [_block_preview(request, run_id, block) for block in _records(run_id, 'blocks.jsonl')
                  if block.get('block_id') in related]
    else:
        chunk = {}
    return templates.TemplateResponse(request, 'chunk_detail.html', {
        'record': record, 'metadata': record['metadata'], 'chunk': chunk,
        'blocks': blocks, 'siblings': sorted(siblings, key=lambda row: row.get('chunk_index', 0))})


@router.get('/runs', response_class=HTMLResponse)
def ui_runs(request: Request):
    return templates.TemplateResponse(request, 'runs.html', {'runs': list_runs(),
                                                           'parse_log_dir': str(config.PARSE_LOG_DIR)})


@router.get('/runs/{run_id}', response_class=HTMLResponse)
def ui_run_detail(request: Request, run_id: str):
    run = _run_or_404(run_id)
    pages, chunks = load_run_pages(run_id), load_run_chunks(run_id)
    counts = Counter(page for chunk in chunks for page in chunk.get('pages', [chunk.get('page')]))
    page_rows = [{**page, 'chunks': counts.get(page.get('page'), 0)} for page in pages]
    total = (run.get('counts') or {}).get('pages_total', len(pages))
    blocks = [_block_preview(request, run_id, block) for block in _records(run_id, 'blocks.jsonl')
              if block.get('selected', True)]
    return templates.TemplateResponse(request, 'run_detail.html', {
        'run': run, 'pages': page_rows, 'chunk_total': len(chunks), 'chunks': chunks,
        'blocks': blocks, 'logical_blocks': _records(run_id, 'logical_blocks.jsonl'),
        'merges': _records(run_id, 'merges.jsonl'),
        'missing_pages': sorted(set(range(1, int(total) + 1)) - {p.get('page') for p in pages}),
        'has_chunks_file': (resolve_run_dir(run_id) / 'chunks.jsonl').is_file()})


@router.post('/runs/{run_id}/review')
def ui_review_run(run_id: str, reviewer: str = Form(...), decision: str = Form('approved'),
                  notes: str = Form('')):
    if decision not in {'approved', 'rejected'}:
        raise HTTPException(status_code=400, detail='无效审核决定')
    try:
        approve_run(run_id, reviewer=reviewer, decision=decision, notes=notes)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f'/ui/runs/{run_id}', status_code=303)


@router.post('/runs/{run_id}/index')
def ui_index_run(run_id: str):
    try:
        validate_indexable_run(run_id)
        get_job_manager().submit_index(run_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse('/ui', status_code=303)


@router.get('/runs/{run_id}/assets/{asset_path:path}', name='ui_run_asset')
def ui_run_asset(run_id: str, asset_path: str):
    _run_or_404(run_id)
    path = _inside_run(run_id, asset_path)
    suffix = path.suffix.lower()
    if suffix in _IMAGE_TYPES:
        return FileResponse(path, media_type=_IMAGE_TYPES[suffix], headers={'X-Content-Type-Options': 'nosniff'})
    # HTML/SVG等原始文件只能下载，不能借资产端点在本机页面执行内容。
    return FileResponse(path, media_type='application/octet-stream', filename=path.name,
                        headers={'X-Content-Type-Options': 'nosniff'})


@router.get('/runs/{run_id}/source')
def ui_run_source(run_id: str):
    run = _run_or_404(run_id)
    path = Path(run.get('pdf_path', '')).resolve()
    # 原件存储在独立不可变目录；位置和hash都来自run，用户不能传入任意本机文件路径。
    if not path.is_relative_to(Path(config.DOCUMENT_DIR).resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail='不可变原件不存在')
    if file_hash(path) != run.get('pdf_sha256'):
        raise HTTPException(status_code=409, detail='PDF原件校验失败')
    return FileResponse(path, media_type='application/pdf', filename=run.get('source', 'source.pdf'))
