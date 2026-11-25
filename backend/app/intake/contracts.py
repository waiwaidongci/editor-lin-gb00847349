from __future__ import annotations

import csv
from io import StringIO
from typing import Any

from app.errors import Problem
from domain.revisions import RECEIVED_KEYS, REVISION_KEYS, WITHDRAWN_KEYS


CONTAINER_KEYS = ('records', 'revisions', 'rows', 'items', 'observations', 'data',
                  'supplements', 'withdrawals', 'events', 'entries')
NAME_KEYS = ('stream', 'stream_name', 'name', 'ledger', 'ledger_name',
             'dataset_name', 'dataset')
DATASET_ID_KEYS = ('dataset_id', 'datasetId', 'dataset_pk')
KEY_KEYS = ('batch_id', 'idempotency_key', 'key', 'request_key', 'client_token')
DEFAULT_RECEIVED_KEYS = tuple(dict.fromkeys(
    RECEIVED_KEYS + ('default_received_at', 'batch_received_at', 'imported_at')
))


def first_value(payload: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def extract_batch_payload(
    body: Any,
    *,
    stream_name: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[str, str | None, Any, list[dict]]:
    if isinstance(body, list):
        payload: dict[str, Any] = {}
        records_raw = body
    elif isinstance(body, dict):
        payload = body
        records_raw = first_value(payload, CONTAINER_KEYS)
        if isinstance(records_raw, dict):
            records_raw = [records_raw]
        if not isinstance(records_raw, list):
            ids = payload.get('editor_ids') or payload.get('editors')
            if isinstance(ids, list) and ids:
                records_raw = [{'editor_id': item} if not isinstance(item, dict) else item
                               for item in ids]
            elif 'editor_id' in payload or 'editorId' in payload:
                records_raw = [payload]
            elif records_raw is True and ('editor_id' in payload or 'editorId' in payload):
                records_raw = [payload]
    else:
        raise Problem(422, 'invalid_request', 'Body must be a JSON object or array')

    if not isinstance(records_raw, list) or not records_raw:
        raise Problem(422, 'invalid_request',
                      'Provide a nonempty records/revisions/rows array')
    records = []
    for row in records_raw:
        if isinstance(row, str):
            row = {'editor_id': row}
        if not isinstance(row, dict):
            raise Problem(422, 'invalid_request', 'Every revision record must be an object')
        records.append(dict(row))

    default_revision = first_value(payload, REVISION_KEYS)
    if default_revision is not None:
        for record in records:
            if not any(key in record for key in REVISION_KEYS):
                record['revision'] = default_revision
    if payload.get('withdrawn') is True or 'withdrawals' in payload:
        for record in records:
            record.setdefault('withdrawn', True)
    name = (stream_name or first_value(payload, NAME_KEYS) or '').strip() if isinstance(
        stream_name or first_value(payload, NAME_KEYS), str) else ''
    key = idempotency_key or first_value(payload, KEY_KEYS)
    if key is not None and not isinstance(key, str):
        key = str(key)
    default_received = first_value(payload, DEFAULT_RECEIVED_KEYS)
    return name, key, default_received, records


def dataset_identifier(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    value = first_value(payload, DATASET_ID_KEYS)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_revision_csv(body: bytes) -> list[dict]:
    try:
        text = body.decode('utf-8-sig')
        reader = csv.DictReader(StringIO(text, newline=''), strict=True)
        fields = reader.fieldnames or []
        if not fields or len(fields) != len(set(fields)):
            raise ValueError('CSV must have unique column headers')
        records = []
        for index, row in enumerate(reader, 1):
            if None in row:
                raise ValueError(f'CSV row {index} has a different number of columns')
            cleaned = {}
            for key, value in row.items():
                if value is None:
                    continue
                value = value.strip()
                cleaned[key] = None if value == '' else value
            records.append(cleaned)
        if not records:
            raise ValueError('CSV must contain at least one row')
        return records
    except (UnicodeError, csv.Error, ValueError) as exc:
        raise Problem(422, 'invalid_csv', str(exc)) from None


def csv_is_revision_batch(body: bytes) -> bool:
    try:
        first_line = body.decode('utf-8-sig').splitlines()[0]
    except (UnicodeError, IndexError):
        return False
    headers = {item.strip() for item in first_line.split(',')}
    special = set(REVISION_KEYS) | set(RECEIVED_KEYS) | set(WITHDRAWN_KEYS) | {
        'withdrawn', 'status', 'op', 'operation', 'action'}
    return bool(headers & special)
