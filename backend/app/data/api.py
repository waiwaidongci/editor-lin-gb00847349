import json
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.auth.dependencies import account, require
from app.auth.roles import Role
from app.clock import utc_iso
from app.data.contracts import DatasetImport
from app.data.csv_input import parse_csv
from app.data.models import Dataset
from app.data.repository import get_dataset, import_dataset, observation_query
from app.database import Session
from app.errors import Problem
from app.intake.contracts import csv_is_revision_batch, extract_batch_payload, parse_revision_csv
from app.intake.repository import import_revision_batch, materialize_stream_dataset
from app.settings import settings
from domain.revisions import RECEIVED_KEYS, REVISION_KEYS, WITHDRAWN_KEYS


router = APIRouter(prefix='/datasets', tags=['Data intake'])
analyst = require(Role.analyst)


def describe(dataset):
    return {'id': dataset.id, 'name': dataset.name, 'checksum': dataset.checksum,
            'row_count': dataset.row_count, 'created_by': dataset.created_by,
            'created_at': utc_iso(dataset.created_at)}


def _is_revision_payload(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    if any(key in payload for key in ('records', 'revisions')):
        return True
    rows = payload.get('rows')
    if not isinstance(rows, list):
        return False
    special = set(REVISION_KEYS) | set(RECEIVED_KEYS) | set(WITHDRAWN_KEYS) | {
        'withdrawn', 'status', 'op', 'operation', 'action',
    }
    return any(isinstance(row, dict) and any(key in special for key in row) for row in rows)


async def _import_revisions(request: Request, response: Response, body: bytes, user,
                            query_name: str | None):
    media = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    header_key = request.headers.get('Idempotency-Key') or request.headers.get('X-Idempotency-Key')
    if media == 'text/csv':
        if query_name is None:
            raise Problem(422, 'missing_name', 'Provide the CSV stream name as a query parameter')
        name, records = query_name, parse_revision_csv(body)
        key, default_received = header_key, None
    else:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            raise Problem(422, 'invalid_request', 'Provide valid JSON') from None
        name, body_key, default_received, records = extract_batch_payload(
            parsed, stream_name=query_name, idempotency_key=header_key,
        )
        key = header_key or body_key
    if not name:
        raise Problem(422, 'missing_name', 'Provide a dataset/stream name')
    result = await run_in_threadpool(
        import_revision_batch, name, records, user.email,
        batch_key=key, default_received_at=default_received,
    )
    dataset, materialization_error = await run_in_threadpool(
        materialize_stream_dataset, name, user.email,
    )
    if dataset is not None:
        result.update({
            'id': dataset.id, 'name': dataset.name, 'checksum': dataset.checksum,
            'row_count': dataset.row_count,
        })
        result['current_dataset'] = describe(dataset)
    if materialization_error:
        result['materialization_error'] = materialization_error
    response.status_code = 200 if not result['created'] else 201
    return result


async def bounded_upload(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > settings.max_upload_bytes:
            raise Problem(413, 'upload_too_large', 'Maximum upload size is 2 MiB')
        body.extend(chunk)
    return bytes(body)


@router.get('')
def list_datasets(user=Depends(account), limit: Annotated[int, Query(ge=1, le=100)] = 25):
    with Session() as db:
        datasets = db.scalars(select(Dataset).order_by(Dataset.id.desc()).limit(limit))
        return [describe(dataset) for dataset in datasets]


@router.get('/{dataset_id}/rows')
def rows(dataset_id: int, user=Depends(account),
         offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=250)] = 50):
    with Session() as db:
        dataset = get_dataset(db, dataset_id)
        records = list(db.scalars(observation_query(dataset_id).offset(offset).limit(limit)))
    return {'dataset': describe(dataset), 'offset': offset, 'rows': records}


@router.post('', openapi_extra={'requestBody': {'required': True, 'content': {
    'application/json': {'schema': DatasetImport.model_json_schema()},
    'text/csv': {'schema': {'type': 'string'}},
}}})
async def ingest(request: Request, response: Response, user=Depends(analyst),
                 name: Annotated[str | None, Query(max_length=120)] = None):
    media = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if media not in {'application/json', 'text/csv'}:
        raise Problem(415, 'unsupported_media_type', 'Use application/json or text/csv')
    body = await bounded_upload(request)
    if media == 'text/csv':
        if csv_is_revision_batch(body):
            return await _import_revisions(request, response, body, user, name)
        if name is None:
            raise Problem(422, 'missing_name', 'Provide the CSV dataset name as a query parameter')
        records = await run_in_threadpool(parse_csv, body)
    else:
        try:
            raw_payload = json.loads(body)
        except json.JSONDecodeError:
            raise Problem(422, 'invalid_request', 'Provide a JSON name and a nonempty rows array') from None
        if isinstance(raw_payload, list) or _is_revision_payload(raw_payload):
            return await _import_revisions(request, response, body, user, name)
        try:
            payload = DatasetImport.model_validate(raw_payload)
        except ValidationError:
            raise Problem(422, 'invalid_request', 'Provide a JSON name and a nonempty rows array') from None
        name, records = payload.name, payload.rows
    dataset, created = await run_in_threadpool(import_dataset, name, records, user.email)
    response.status_code = 201 if created else 200
    return {**describe(dataset), 'created': created}
