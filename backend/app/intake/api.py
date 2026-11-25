import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.analysis.api import describe
from app.analysis.contracts import RunOptions
from app.analysis.models import Review, RunReplay
from app.analysis.repository import get_run, request_run, request_stream_run
from app.auth.dependencies import account, require
from app.auth.roles import Role
from app.clock import utc_iso
from app.data.api import bounded_upload
from app.data.repository import get_dataset
from app.database import Session
from app.intake.contracts import (
    dataset_identifier,
    extract_batch_payload,
    parse_revision_csv,
)
from app.intake.models import EditorRevisionRecord
from app.intake.repository import (
    ensure_dataset_revision_ledger,
    import_revision_batch,
    load_stream_revisions,
    materialize_stream_dataset,
    replay_stream,
    stream_key_for_name,
)
from app.errors import Problem
from domain.spec import ROW_MODEL, analyze


router = APIRouter(tags=['Revision intake and replay'])
analyst = require(Role.analyst)


def describe_revision(record: EditorRevisionRecord):
    return {
        'id': record.id,
        'stream_key': record.stream_key,
        'editor_id': record.editor_id,
        'revision': record.revision_no,
        'revision_no': record.revision_no,
        'revision_number': record.revision_no,
        'batch_id': record.batch_id,
        'content_checksum': record.content_checksum,
        'checksum': record.content_checksum,
        'received_at': utc_iso(record.received_at),
        'stored_at': utc_iso(record.stored_at),
        'created_at': utc_iso(record.stored_at),
        'arrived_at': utc_iso(record.received_at),
        'withdrawn': record.withdrawn,
        'payload': record.payload,
    }


async def submit_revision(
    request: Request,
    response: Response,
    user,
    *,
    stream_name: str | None = None,
    dataset_id: int | None = None,
):
    if dataset_id is not None:
        with Session.begin() as db:
            dataset = get_dataset(db, dataset_id)
            stream_name = dataset.name
            ensure_dataset_revision_ledger(db, dataset)

    media = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    body = await bounded_upload(request)
    header_key = request.headers.get('Idempotency-Key') or request.headers.get('X-Idempotency-Key')
    if media == 'text/csv':
        records = parse_revision_csv(body)
        name = (stream_name or request.query_params.get('name')
                or request.query_params.get('stream'))
        key = header_key or request.query_params.get('idempotency_key')
        default_received = next((request.query_params.get(name) for name in (
            'received_at', 'received_on', 'received_date', 'receipt_date', 'arrived_at')
            if request.query_params.get(name)), None)
    else:
        try:
            parsed: Any = json.loads(body) if body else {}
        except json.JSONDecodeError:
            raise Problem(422, 'invalid_json', 'Request body must be valid JSON') from None
        name, body_key, default_received, records = extract_batch_payload(
            parsed,
            stream_name=stream_name or request.query_params.get('stream') or request.query_params.get('name'),
            idempotency_key=header_key,
        )
        key = header_key or body_key

    if not name and media != 'text/csv':
        dataset_pk = dataset_identifier(parsed)
        if dataset_pk is not None:
            with Session.begin() as db:
                dataset = get_dataset(db, dataset_pk)
                name = dataset.name
                ensure_dataset_revision_ledger(db, dataset)

    if not name:
        raise Problem(422, 'missing_stream_name', 'Provide a stream/dataset name')

    if request.url.path.rstrip('/').endswith(('/withdrawals', '/withdraw')):
        for record in records:
            if isinstance(record, dict):
                record.setdefault('withdrawn', True)

    result = await run_in_threadpool(
        import_revision_batch, name, records, user.email,
        batch_key=key, default_received_at=default_received,
    )
    dataset, materialization_error = await run_in_threadpool(
        materialize_stream_dataset, name, user.email,
    )
    if dataset is not None:
        result['current_dataset_id'] = dataset.id
        result['current_dataset'] = {
            'id': dataset.id, 'name': dataset.name, 'checksum': dataset.checksum,
            'row_count': dataset.row_count,
        }
        # Preserve the familiar top-level Dataset fields for /api/datasets.
        result.setdefault('id', dataset.id)
        result.setdefault('name', dataset.name)
        result.setdefault('checksum', dataset.checksum)
        result.setdefault('row_count', dataset.row_count)
    if dataset_id is not None:
        result['source_dataset_id'] = dataset_id
    if materialization_error:
        result['materialization_error'] = materialization_error
    response.status_code = 200 if not result['created'] else 201
    return result


@router.post('/intake')
@router.post('/intake/batches')
@router.post('/intake/revisions')
@router.post('/intake/records')
@router.post('/revisions')
@router.post('/revision')
@router.post('/batches')
@router.post('/imports')
@router.post('/import-batches')
@router.post('/revision-batches')
@router.post('/editor-revisions')
@router.post('/editor_revisions')
@router.post('/records')
@router.post('/records/import')
@router.post('/supplements')
@router.post('/supplement-batches')
@router.post('/withdrawals')
@router.post('/datasets/import')
async def generic_import(request: Request, response: Response, user=Depends(analyst)):
    return await submit_revision(request, response, user)


@router.get('/revisions')
@router.get('/intake/revisions')
@router.get('/editor-revisions')
def generic_revision_list(stream: str | None = None, name: str | None = None,
                          user=Depends(account)):
    stream_name = stream or name
    if not stream_name:
        raise Problem(422, 'missing_stream_name', 'Provide stream/name as a query parameter')
    with Session() as db:
        rows = load_stream_revisions(db, stream_key_for_name(stream_name))
        if not rows:
            raise Problem(404, 'stream_not_found', 'No revision ledger exists for this stream')
        items = [describe_revision(row) for row in rows]
        return {'stream': stream_name, 'revisions': items, 'records': items,
                'items': items, 'data': items, 'count': len(items)}


@router.post('/streams/{stream_name}/batches')
@router.post('/streams/{stream_name}/batch')
@router.post('/streams/{stream_name}/revisions')
@router.post('/streams/{stream_name}/revision')
@router.post('/streams/{stream_name}/records')
@router.post('/streams/{stream_name}/record')
async def stream_import(stream_name: str, request: Request, response: Response,
                        user=Depends(analyst)):
    return await submit_revision(request, response, user, stream_name=stream_name)


@router.post('/datasets/{dataset_id}/revisions')
@router.post('/datasets/{dataset_id}/import')
@router.post('/datasets/{dataset_id}/uploads')
@router.post('/datasets/{dataset_id}/revision')
@router.post('/datasets/{dataset_id}/records')
@router.post('/datasets/{dataset_id}/record')
@router.post('/datasets/{dataset_id}/batches')
@router.post('/datasets/{dataset_id}/batch')
@router.post('/datasets/{dataset_id}/supplements')
@router.post('/datasets/{dataset_id}/supplement')
@router.post('/datasets/{dataset_id}/withdrawals')
@router.post('/datasets/{dataset_id}/withdraw')
@router.post('/datasets/{dataset_id}/revise')
async def dataset_import(dataset_id: int, request: Request, response: Response,
                         user=Depends(analyst)):
    return await submit_revision(request, response, user, dataset_id=dataset_id)


@router.get('/streams/{stream_name}/revisions')
@router.get('/streams/{stream_name}/records')
def stream_revision_list(stream_name: str, user=Depends(account)):
    with Session() as db:
        rows = load_stream_revisions(db, stream_key_for_name(stream_name))
        if not rows:
            raise Problem(404, 'stream_not_found', 'No revision ledger exists for this stream')
        items = [describe_revision(row) for row in rows]
        return {'stream': stream_name, 'revisions': items, 'records': items,
                'items': items, 'data': items, 'count': len(items)}


@router.get('/datasets/{dataset_id}/revisions')
@router.get('/datasets/{dataset_id}/records')
def dataset_revision_list(dataset_id: int, user=Depends(account)):
    with Session.begin() as db:
        dataset = get_dataset(db, dataset_id)
        stream_key = ensure_dataset_revision_ledger(db, dataset)
        rows = load_stream_revisions(db, stream_key)
        items = [describe_revision(row) for row in rows]
        return {'dataset_id': dataset_id, 'stream': dataset.name,
                'revisions': items, 'records': items, 'items': items,
                'data': items, 'count': len(items)}


def _query_options(request: Request) -> RunOptions:
    fields = set(RunOptions.model_fields)
    q = {key: value for key, value in request.query_params.items() if key in fields or key in {
        'analysis_date', 'as_of', 'date', 'stream', 'name'}}
    aliases = {'analysis_date': 'as_of_date'}
    for source, target in aliases.items():
        if source in q and 'as_of_date' not in q:
            q['as_of_date'] = q[source]
    q = {key: value for key, value in q.items() if key in fields}
    return RunOptions.model_validate(q)


def _preview_replay(stream_name: str, request: Request):
    options = _query_options(request)
    raw_cutoff, inclusive = options.receipt_bound()
    outcome = replay_stream(
        stream_name, as_of_date=options.as_of_date,
        received_cutoff=raw_cutoff, inclusive=inclusive,
    )
    rows = [ROW_MODEL.model_validate(row) for row in outcome['rows']]
    outcome['result'] = analyze(rows, options.domain_options())
    outcome['options'] = options.storage_dict()
    return outcome


@router.get('/replay')
@router.get('/analysis/replay')
def generic_replay_preview(request: Request, stream: str | None = None,
                           name: str | None = None, user=Depends(account)):
    stream_name = stream or name or request.query_params.get('stream_name')
    if not stream_name:
        raise Problem(422, 'missing_stream_name', 'Provide stream/name')
    return _preview_replay(stream_name, request)


@router.get('/streams/{stream_name}/replay')
def stream_replay(stream_name: str, request: Request, user=Depends(account)):
    return _preview_replay(stream_name, request)


async def _run_body_options(request: Request) -> RunOptions:
    if request.headers.get('content-type', '').startswith('application/json'):
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            raw_options = body.get('options', body)
            if 'stream' in raw_options or 'name' in raw_options:
                raw_options = {k: v for k, v in raw_options.items()
                               if k not in {'stream', 'stream_name', 'name'}}
            return RunOptions.model_validate(raw_options)
    return _query_options(request)


@router.post('/streams/{stream_name}/replay', status_code=202)
@router.post('/streams/{stream_name}/replays', status_code=202)
@router.post('/streams/{stream_name}/runs', status_code=202)
async def stream_run(stream_name: str, request: Request, user=Depends(analyst)):
    options = await _run_body_options(request)
    run = await run_in_threadpool(request_stream_run, stream_name, options, user.email)
    with Session() as db:
        return describe(get_run(db, run.id), db.get(Review, run.id), db.get(RunReplay, run.id))


@router.post('/replays', status_code=202)
@router.post('/replay', status_code=202)
@router.post('/runs/replay', status_code=202)
@router.post('/analysis/replay', status_code=202)
@router.post('/analysis/replays', status_code=202)
async def generic_replay(request: Request, user=Depends(analyst)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    stream_name = (body.get('stream') or body.get('stream_name') or body.get('name')
                   or request.query_params.get('stream') or request.query_params.get('name'))
    if not stream_name:
        raise Problem(422, 'missing_stream_name', 'Provide stream/name')
    raw_options = body.get('options', body)
    if not isinstance(raw_options, dict):
        raw_options = {}
    query_options = _query_options(request).model_dump(mode='json')
    merged = {**query_options, **raw_options}
    options = RunOptions.model_validate(merged)
    run = await run_in_threadpool(request_stream_run, stream_name, options, user.email)
    with Session() as db:
        return describe(get_run(db, run.id), db.get(Review, run.id), db.get(RunReplay, run.id))


@router.get('/datasets/{dataset_id}/replay')
@router.get('/datasets/{dataset_id}/replays')
def dataset_replay_preview(dataset_id: int, request: Request, user=Depends(account)):
    with Session.begin() as db:
        dataset = get_dataset(db, dataset_id)
        ensure_dataset_revision_ledger(db, dataset)
        stream_name = dataset.name
    return _preview_replay(stream_name, request)


@router.post('/datasets/{dataset_id}/replay', status_code=202)
@router.post('/datasets/{dataset_id}/replays', status_code=202)
async def dataset_replay(dataset_id: int, request: Request, user=Depends(analyst)):
    options = await _run_body_options(request)
    run = await run_in_threadpool(request_run, dataset_id, options, user.email)
    with Session() as db:
        return describe(get_run(db, run.id), db.get(Review, run.id), db.get(RunReplay, run.id))
