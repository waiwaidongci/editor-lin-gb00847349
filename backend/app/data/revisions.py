"""Compatibility import path for revision intake/replay services."""

from domain.revisions import *  # noqa: F401,F403
from app.intake.models import EditorRevisionRecord, IntakeBatch
from app.intake.repository import (
    import_revision_batch,
    replay_stream,
    stream_key_for_name,
)

__all__ = [
    'EditorRevisionRecord', 'IntakeBatch', 'import_revision_batch',
    'replay_stream', 'stream_key_for_name',
]
