from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.clock import now_utc
from app.database import Base


class AnalysisRun(Base):
    __tablename__ = 'analysis_runs'
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    options: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Review(Base):
    __tablename__ = 'reviews'
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), primary_key=True)
    decision: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class RunReplay(Base):
    """Frozen revision-selection manifest for one queued analysis run."""

    __tablename__ = 'run_replays'
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), primary_key=True)
    snapshot_dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), index=True)
    source_run_id: Mapped[int | None] = mapped_column(ForeignKey('analysis_runs.id'), index=True)
    stream_key: Mapped[str] = mapped_column(String(160), index=True)
    stream_name: Mapped[str | None] = mapped_column(String(120))
    received_cutoff: Mapped[datetime | None] = mapped_column(DateTime)
    cutoff_inclusive: Mapped[bool] = mapped_column(Boolean, default=True)
    frozen_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    adopted: Mapped[list] = mapped_column(JSON)
