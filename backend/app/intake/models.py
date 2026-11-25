from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.clock import now_utc
from app.database import Base


class IntakeBatch(Base):
    """An idempotent import request containing one or more revisions."""

    __tablename__ = 'intake_batches'
    __table_args__ = (
        UniqueConstraint('stream_key', 'batch_key', name='uq_intake_batch_stream_key'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stream_key: Mapped[str] = mapped_column(String(160), index=True)
    stream_name: Mapped[str] = mapped_column(String(120), index=True)
    batch_key: Mapped[str] = mapped_column(String(120))
    request_checksum: Mapped[str] = mapped_column(String(64))
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    actor: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class EditorRevisionRecord(Base):
    """Append-only natural key ``(stream, editor, revision)``."""

    __tablename__ = 'editor_revision_records'
    __table_args__ = (
        UniqueConstraint(
            'stream_key', 'editor_id', 'revision_no',
            name='uq_editor_revision_stream_editor_rev',
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stream_key: Mapped[str] = mapped_column(String(160), index=True)
    editor_id: Mapped[str] = mapped_column(String(80), index=True)
    revision_no: Mapped[int] = mapped_column(Integer, index=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey('intake_batches.id'), index=True)
    content_checksum: Mapped[str] = mapped_column(String(64), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    stored_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    withdrawn: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON)
