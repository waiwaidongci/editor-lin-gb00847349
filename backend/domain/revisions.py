"""Append-only editor revision selection and cutoff replay.

The intake ledger stores every revision.  This module is deliberately free of
FastAPI/SQLAlchemy so that its replay and conflict rules can be tested without
a database or HTTP client.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
import json
from typing import Any, Iterable

from pydantic import ValidationError

from domain.cohort.models import EditorRow


OUTCOME_FIELDS = (
    "post_minutes",
    "assigned_manuscripts",
    "sla_breaches",
    "rework_manuscripts",
)

EDITOR_KEYS = ("editor_id", "editorId", "editor", "id", "key", "编辑编号")
REVISION_KEYS = (
    "revision", "revision_no", "revision_number", "revisionNo", "rev_no",
    "rev", "version", "version_no", "version_number", "revision_index",
    "update_no", "版本号", "修订号", "修订版本",
)
RECEIVED_KEYS = (
    "received_at", "receivedAt", "received_time", "received_on", "received_date",
    "receipt_at", "receipt_time", "receipt_date", "intake_at", "intake_time",
    "incoming_at", "arrived_at", "arrival_at", "arrival_time", "submitted_at",
    "timestamp", "收件时间", "收件日期", "收件时刻",
)
STORED_KEYS = (
    "stored_at", "created_at", "inserted_at", "persisted_at",
    "warehoused_at", "recorded_at", "入库时间", "入库时刻",
)
BATCH_KEYS = (
    "batch_id", "idempotency_key", "request_key", "client_token",
    "batch_key", "batch", "批次号",
)
CHECKSUM_KEYS = ("content_checksum", "checksum", "fingerprint", "hash")
WITHDRAWN_KEYS = (
    "withdrawn", "is_withdrawn", "withdrawal", "is_withdrawal", "revoked",
    "is_revoked", "retracted", "deleted", "remove", "removed", "recalled",
    "takeback", "tombstone", "撤回", "已撤回", "撤稿",
)
STATUS_KEYS = ("status", "state", "op", "operation", "action")
WITHDRAWN_VALUES = {
    "withdraw", "withdrawn", "withdrawal", "revoke", "revoked", "retract",
    "retracted", "delete", "deleted", "remove", "removed", "recall",
    "recalled", "takeback", "tombstone", "cancel", "cancelled", "canceled",
    "撤回", "已撤回", "撤稿", "删除",
}

METADATA_KEYS = {
    key for keys in (
        EDITOR_KEYS, REVISION_KEYS, RECEIVED_KEYS, STORED_KEYS, BATCH_KEYS,
        CHECKSUM_KEYS, WITHDRAWN_KEYS, STATUS_KEYS,
        ("revision_conflict",),
    ) for key in keys
}


class RevisionValidation(ValueError):
    """A revision record cannot be interpreted."""


class RevisionConflict(ValueError):
    """The same natural revision number has two different payloads."""

    def __init__(self, message: str, conflicts: list[dict] | None = None):
        super().__init__(message)
        self.conflicts = conflicts or []


class RevisionReplayError(ValueError):
    """Selected revisions do not form a valid editorial row."""


def _first(raw: Mapping[str, Any], keys: Iterable[str]) -> tuple[str | None, Any]:
    for key in keys:
        if key in raw:
            return key, raw[key]
    return None, None


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=_json_default,
    )


def content_fingerprint(
    editor_id: str,
    revision: int,
    data: Mapping[str, Any],
    received_at: str | None = None,
    withdrawn: bool = False,
) -> str:
    import hashlib

    envelope = {
        "editor_id": editor_id,
        "revision": revision,
        "received_at": received_at,
        "withdrawn": withdrawn,
        "data": data,
    }
    return hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()


def parse_bound(value: Any) -> date | datetime | None:
    """Parse a cutoff, retaining date-only granularity."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
    if not isinstance(value, str):
        raise RevisionValidation("时间截止须为 ISO 日期或时间")
    text = value.strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise RevisionValidation("时间截止须使用 YYYY-MM-DD 或带偏移的 ISO 时间") from exc


def parse_timestamp(value: Any) -> tuple[datetime | None, date | None, str | None]:
    bound = parse_bound(value)
    if bound is None:
        return None, None, None
    if isinstance(bound, datetime):
        return bound, bound.date(), "datetime"
    return datetime(bound.year, bound.month, bound.day), bound, "date"


def _parse_revision_number(value: Any) -> int:
    if isinstance(value, bool):
        raise RevisionValidation("修订号须为非负整数")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = int(value.strip())
        except ValueError:
            try:
                number = int(float(value.strip()))
            except ValueError as exc:
                raise RevisionValidation("修订号须为非负整数") from exc
    else:
        raise RevisionValidation("修订号须为非负整数")
    if number < 0:
        raise RevisionValidation("修订号不可为负数")
    return number


def _is_withdrawn(raw: Mapping[str, Any]) -> bool:
    for key in WITHDRAWN_KEYS:
        if key in raw:
            value = raw[key]
            if isinstance(value, bool):
                return value
            if value is None:
                continue
            text = str(value).strip().lower()
            if text in {"1", "true", "yes", "y", "是", "撤回"}:
                return True
            if text in {"0", "false", "no", "n", "否", "active", "valid"}:
                return False
    _, status = _first(raw, STATUS_KEYS)
    if status is not None and str(status).strip().lower() in WITHDRAWN_VALUES:
        return True
    return False


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


def normalize_revision(
    raw: Mapping[str, Any],
    position: int | str | None = None,
    default_received_at: datetime | date | str | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        where = f"第 {position} 条" if position is not None else "记录"
        raise RevisionValidation(f"{where}须为对象")

    # Accept {"editor_id": ..., "revision": 2, "fields": {...}} envelopes.
    for nested_key in ("fields", "changes", "patch", "correction", "values", "row", "data"):
        nested = raw.get(nested_key)
        if isinstance(nested, Mapping):
            merged = dict(nested)
            merged.update({key: value for key, value in raw.items() if key != nested_key})
            raw = merged
            break

    _, editor_value = _first(raw, EDITOR_KEYS)
    editor_id = editor_value.strip() if isinstance(editor_value, str) else editor_value
    if not isinstance(editor_id, str) or not editor_id:
        where = f"第 {position} 条记录" if position is not None else "记录"
        raise RevisionValidation(f"{where}缺少有效的 editor_id")
    if len(editor_id) > 80:
        raise RevisionValidation(f"编辑 {editor_id} 的编号不可超过 80 字符")

    _, revision_value = _first(raw, REVISION_KEYS)
    revision_explicit = revision_value is not None and revision_value != ""
    try:
        revision = _parse_revision_number(1 if revision_value is None or revision_value == "" else revision_value)
    except RevisionValidation as exc:
        raise RevisionValidation(f"编辑 {editor_id}：{exc}") from None

    received_key, explicit_received = _first(raw, RECEIVED_KEYS)
    implicit_received = explicit_received is None or explicit_received == ""
    _, stored_value = _first(raw, STORED_KEYS)
    if not implicit_received:
        received_source: Any = explicit_received
    elif stored_value not in (None, ""):
        # Raw payloads often call the arrival timestamp created_at.  The
        # repository still assigns its own persistence time separately.
        received_source = stored_value
    else:
        received_source = default_received_at
    received_at, received_on, precision = parse_timestamp(received_source)
    stored_at, _, _ = parse_timestamp(stored_value)
    _, batch_value = _first(raw, BATCH_KEYS)
    _, checksum_value = _first(raw, CHECKSUM_KEYS)
    withdrawn = _is_withdrawn(raw)

    data = {
        key: value for key, value in raw.items()
        if key not in METADATA_KEYS and key not in RECEIVED_KEYS and key not in STORED_KEYS
        and key not in BATCH_KEYS and key not in CHECKSUM_KEYS and key not in WITHDRAWN_KEYS
        and key not in STATUS_KEYS
    }
    data["editor_id"] = editor_id

    explicit_received_iso = None
    if not implicit_received and received_at is not None:
        explicit_received_iso = received_at.isoformat()
    checksum = checksum_value if isinstance(checksum_value, str) and checksum_value else content_fingerprint(
        editor_id, revision, data, explicit_received_iso, withdrawn
    )
    return {
        "editor_id": editor_id,
        "revision": revision,
        "revision_explicit": revision_explicit,
        "received_at": received_at,
        "received_on": received_on,
        "received_precision": precision,
        "received_implicit": implicit_received,
        "stored_at": stored_at,
        "batch_key": batch_value if isinstance(batch_value, (str, int)) else None,
        "withdrawn": withdrawn,
        "data": data,
        "content_checksum": checksum,
    }


def as_revision(record: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize either a raw request record or a repository record."""
    if isinstance(record, Mapping) and isinstance(record.get("data"), Mapping):
        editor_id = str(record["editor_id"]).strip()
        revision = _parse_revision_number(record.get("revision", record.get("revision_no", 1)))
        received_at, received_on, precision = parse_timestamp(record.get("received_at"))
        stored_at, _, _ = parse_timestamp(record.get("stored_at"))
        data = dict(record["data"])
        data["editor_id"] = editor_id
        checksum = record.get("content_checksum") or content_fingerprint(
            editor_id, revision, data,
            received_at.isoformat() if received_at and not record.get("received_implicit") else None,
            bool(record.get("withdrawn", False)),
        )
        return {
            "editor_id": editor_id,
            "revision": revision,
            "revision_explicit": bool(record.get("revision_explicit", True)),
            "received_at": received_at,
            "received_on": received_on or (received_at.date() if received_at else None),
            "received_precision": precision,
            "received_implicit": bool(record.get("received_implicit", False)),
            "stored_at": stored_at,
            "batch_key": record.get("batch_key") or record.get("batch_id"),
            "withdrawn": bool(record.get("withdrawn", False)),
            "data": data,
            "content_checksum": checksum,
        }
    return normalize_revision(record)


def conflict_list(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, int], dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    for raw in records:
        item = as_revision(raw)
        key = (item["editor_id"], item["revision"])
        previous = seen.get(key)
        if previous is None:
            seen[key] = item
        elif previous["content_checksum"] != item["content_checksum"]:
            conflicts.append({
                "editor_id": item["editor_id"],
                "revision": item["revision"],
                "checksums": sorted({previous["content_checksum"], item["content_checksum"]}),
            })
    return conflicts


def ensure_no_conflicts(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    conflicts = conflict_list(records)
    if conflicts:
        first = conflicts[0]
        raise RevisionConflict(
            f"编辑 {first['editor_id']} 的修订号 r{first['revision']} 存在不同内容，整批已拒收",
            conflicts,
        )
    return conflicts


def _eligible(item: Mapping[str, Any], cutoff: date | datetime | None, inclusive: bool,
              visible_at: datetime | None) -> bool:
    if visible_at is not None and item.get("stored_at") and item["stored_at"] > visible_at:
        return False
    if cutoff is None:
        return True
    received_at = item.get("received_at") or item.get("stored_at")
    if received_at is None:
        # Persistence always fills received_at.  For ad-hoc domain use, an
        # undated revision cannot be placed before an explicit receipt cutoff.
        return False
    if isinstance(cutoff, datetime):
        current = received_at
        return current <= cutoff if inclusive else current < cutoff
    received_on = item.get("received_on") or received_at.date()
    return received_on <= cutoff if inclusive else received_on < cutoff


def select_revisions(
    records: Iterable[Mapping[str, Any]],
    *,
    received_cutoff: date | datetime | str | None = None,
    cutoff: date | datetime | str | None = None,
    intake_cutoff: date | datetime | str | None = None,
    received_before: date | datetime | str | None = None,
    inclusive: bool | None = None,
    visible_at: datetime | str | None = None,
    **_aliases: Any,
) -> dict[str, dict[str, Any]]:
    if received_cutoff is None:
        received_cutoff = cutoff or intake_cutoff or received_before
    if inclusive is None:
        inclusive = received_before is None
    items = [as_revision(record) for record in records]
    ensure_no_conflicts(items)
    cutoff = parse_bound(received_cutoff)
    visible = parse_bound(visible_at)
    if not isinstance(visible, datetime) and visible is not None:
        visible = datetime(visible.year, visible.month, visible.day, 23, 59, 59, 999999)

    exact: dict[tuple[str, int], dict[str, Any]] = {}
    for item in items:
        if not _eligible(item, cutoff, inclusive, visible):
            continue
        key = (item["editor_id"], item["revision"])
        previous = exact.get(key)
        if previous is not None and previous["content_checksum"] != item["content_checksum"]:
            raise RevisionConflict(f"编辑 {item['editor_id']} 的 r{item['revision']} 内容冲突")
        exact[key] = item

    selected: dict[str, dict[str, Any]] = {}
    for (editor_id, revision), item in exact.items():
        current = selected.get(editor_id)
        if current is None or revision > current["revision"]:
            selected[editor_id] = item
    return selected


def replay_revisions(
    records: Iterable[Mapping[str, Any]],
    *,
    as_of_date: date | str | None = None,
    analysis_date: date | str | None = None,
    received_cutoff: date | datetime | str | None = None,
    intake_cutoff: date | datetime | str | None = None,
    received_before: date | datetime | str | None = None,
    inclusive: bool | None = None,
    visible_at: datetime | str | None = None,
    validate: bool = True,
) -> dict[str, Any]:
    """Return validated snapshot rows and the revision adopted per editor."""
    if as_of_date is None:
        as_of_date = analysis_date
    if received_cutoff is None:
        received_cutoff = intake_cutoff or received_before
    if inclusive is None:
        inclusive = received_before is None
    items = [as_revision(record) for record in records]
    ensure_no_conflicts(items)
    cutoff = parse_bound(received_cutoff)
    visible = parse_bound(visible_at)
    if isinstance(visible, date) and not isinstance(visible, datetime):
        visible = datetime(visible.year, visible.month, visible.day, 23, 59, 59, 999999)
    visible_dt = visible if isinstance(visible, datetime) else None

    eligible = [item for item in items if _eligible(item, cutoff, inclusive, visible_dt)]
    exact: dict[tuple[str, int], dict[str, Any]] = {}
    for item in eligible:
        key = (item["editor_id"], item["revision"])
        previous = exact.get(key)
        if previous is not None and previous["content_checksum"] != item["content_checksum"]:
            raise RevisionConflict(f"编辑 {item['editor_id']} 的 r{item['revision']} 内容冲突")
        exact[key] = item

    selected: dict[str, dict[str, Any]] = {}
    for (editor_id, revision), item in exact.items():
        current = selected.get(editor_id)
        if current is None or revision > current["revision"]:
            selected[editor_id] = item

    analysis_day = parse_bound(as_of_date)
    if isinstance(analysis_day, datetime):
        analysis_day = analysis_day.date()

    by_editor: dict[str, list[dict[str, Any]]] = {}
    for item in eligible:
        if item["editor_id"] in selected:
            by_editor.setdefault(item["editor_id"], []).append(item)

    rows: list[dict[str, Any]] = []
    adoptions: list[dict[str, Any]] = []
    for editor_id in sorted(by_editor):
        versions = sorted(by_editor[editor_id], key=lambda item: item["revision"])
        merged: dict[str, Any] = {}
        for item in versions:
            merged.update(dict(item["data"]))
            if item["withdrawn"]:
                for field in OUTCOME_FIELDS:
                    merged[field] = None
        chosen = selected[editor_id]
        merged["editor_id"] = editor_id
        if chosen["withdrawn"]:
            for field in OUTCOME_FIELDS:
                merged[field] = None

        if validate:
            try:
                model = EditorRow.model_validate(merged)
            except ValidationError as exc:
                raise RevisionReplayError(
                    f"编辑 {editor_id} 截止当前所选修订无法形成有效快照：{exc.errors()[0]['msg']}"
                ) from None
            row = model.model_dump(mode="json")
        else:
            row = dict(merged)
        rows.append(row)
        outcome_missing = all(row.get(field) is None for field in OUTCOME_FIELDS)
        adoption = {
            "editor_id": editor_id,
            "revision": chosen["revision"],
            "revision_no": chosen["revision"],
            "revision_number": chosen["revision"],
            "adopted_revision": chosen["revision"],
            "content_checksum": chosen["content_checksum"],
            "checksum": chosen["content_checksum"],
            "withdrawn": chosen["withdrawn"],
            "outcome_missing": outcome_missing,
            "result_missing": outcome_missing,
            "missing_outcome": outcome_missing,
            "received_at": _iso_utc(chosen.get("received_at")),
            "stored_at": _iso_utc(chosen.get("stored_at")),
            "batch_key": chosen.get("batch_key"),
        }
        adoptions.append(adoption)

    cutoff_iso = cutoff.isoformat() if cutoff is not None else None
    return {
        "rows": rows,
        "adopted_revisions": adoptions,
        "adoptions": adoptions,
        "count": len(rows),
        "as_of_date": analysis_day.isoformat() if analysis_day else None,
        "received_cutoff": cutoff_iso,
        "cutoff_inclusive": inclusive,
    }


# Concise aliases used by callers and tests.
def replay(*args, **kwargs):
    return replay_revisions(*args, **kwargs)


def highest_revisions(records: Iterable[Mapping[str, Any]], **kwargs):
    return select_revisions(records, **kwargs)


__all__ = [
    "RevisionConflict", "RevisionValidation", "RevisionReplayError",
    "normalize_revision", "as_revision", "conflict_list", "ensure_no_conflicts",
    "select_revisions", "replay_revisions", "replay", "highest_revisions",
    "parse_bound", "content_fingerprint",
]
