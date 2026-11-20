"""Persistent campaign and call records for local and Cloud Run deployments."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func, or_, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    business_name: Mapped[str] = mapped_column(String(200), nullable=False)
    approved: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    calls: Mapped[list[CallRecord]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class CallRecord(Base):
    __tablename__ = "call_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    seller_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone_number: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    exotel_call_sid: Mapped[str | None] = mapped_column(String(100), nullable=True)
    stream_sid: Mapped[str | None] = mapped_column(String(100), nullable=True)
    transcript: Mapped[str] = mapped_column(Text, default="", nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(100), nullable=True)
    trace_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    campaign: Mapped[Campaign] = relationship(back_populates="calls")


class CampaignStore:
    """Async persistence for campaigns and individual call records."""

    def __init__(self, database_uri: str) -> None:
        self.database_uri = database_uri
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None

    async def initialize(self) -> None:
        self.engine = create_async_engine(self.database_uri)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        if self.engine:
            await self.engine.dispose()

    def _session(self) -> AsyncSession:
        if self.session_factory is None:
            raise RuntimeError("CampaignStore.initialize() must be called first")
        return self.session_factory()

    async def create_campaign(self, name: str, business_name: str) -> Campaign:
        campaign = Campaign(
            id=str(uuid4()),
            name=name,
            business_name=business_name,
            approved=False,
        )
        async with self._session() as session:
            session.add(campaign)
            await session.commit()
            await session.refresh(campaign)
            return campaign

    async def approve_campaign(self, campaign_id: str) -> Campaign | None:
        async with self._session() as session:
            campaign = await session.get(Campaign, campaign_id)
            if campaign is None:
                return None
            campaign.approved = True
            campaign.approved_at = utc_now()
            await session.commit()
            await session.refresh(campaign)
            return campaign

    async def get_campaign(self, campaign_id: str) -> Campaign | None:
        async with self._session() as session:
            return await session.get(Campaign, campaign_id)

    async def create_call(
        self,
        campaign_id: str,
        phone_number: str,
        seller_name: str | None,
    ) -> CallRecord:
        call = CallRecord(
            id=str(uuid4()),
            campaign_id=campaign_id,
            seller_name=seller_name,
            phone_number=phone_number,
            status="queued",
        )
        async with self._session() as session:
            campaign = await session.get(Campaign, campaign_id)
            if campaign is None:
                raise LookupError("Campaign not found")
            if not campaign.approved:
                raise PermissionError("Campaign has not been approved")
            session.add(call)
            await session.commit()
            await session.refresh(call)
            return call

    async def list_calls(self, campaign_id: str) -> list[CallRecord]:
        async with self._session() as session:
            result = await session.execute(
                select(CallRecord)
                .where(CallRecord.campaign_id == campaign_id)
                .order_by(CallRecord.created_at.desc())
            )
            return list(result.scalars().all())

    async def get_call(self, call_id: str) -> CallRecord | None:
        async with self._session() as session:
            return await session.get(CallRecord, call_id)

    async def claim_queued_call(self, call_id: str) -> CallRecord | None:
        """Atomically reserve one queued call for a future dial attempt."""
        async with self._session() as session:
            result = await session.execute(
                update(CallRecord)
                .where(CallRecord.id == call_id, CallRecord.status == "queued")
                .values(status="dialing", started_at=utc_now())
            )
            await session.commit()
            if result.rowcount != 1:
                return None
            return await session.get(CallRecord, call_id)

    async def bind_exotel_call(self, call_id: str, call_sid: str) -> CallRecord:
        """Associate a provider ID without overwriting a conflicting ID."""
        if not call_sid or len(call_sid) > 100:
            raise ValueError("Invalid Exotel call SID")
        async with self._session() as session:
            result = await session.execute(
                update(CallRecord)
                .where(CallRecord.id == call_id,
                       (CallRecord.exotel_call_sid.is_(None) | (CallRecord.exotel_call_sid == call_sid)))
                .values(exotel_call_sid=call_sid)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise ValueError("Call not found or Exotel call SID conflicts")
            await session.commit()
            return await session.get(CallRecord, call_id)

    async def mark_dial_outcome_unknown(self, call_id: str) -> None:
        """Record uncertainty without overwriting a later update or bound SID."""
        async with self._session() as session:
            await session.execute(
                update(CallRecord)
                .where(CallRecord.id == call_id, CallRecord.status == "dialing", CallRecord.exotel_call_sid.is_(None))
                .values(status="dial_outcome_unknown", error="Exotel request outcome requires reconciliation")
            )
            await session.commit()

    async def record_carrier_event(
        self, call_id: str, call_sid: str, *, status: str, stream_sid: str | None = None,
    ) -> CallRecord:
        """Apply a verified carrier event using our ID and the carrier's ID.

        This store method does not authenticate external events. Callers must do
        that before supplying an event, and must not derive call_id from a SID
        alone until provider SID uniqueness is enforced.
        """
        terminal = {"completed", "failed", "busy", "no-answer"}
        if status not in terminal | {"answered"}:
            raise ValueError("Unsupported carrier status")
        if not isinstance(call_sid, str) or not call_sid.strip() or len(call_sid) > 100:
            raise ValueError("Invalid Exotel call SID")
        if stream_sid is not None and (not isinstance(stream_sid, str) or not stream_sid.strip() or len(stream_sid) > 100):
            raise ValueError("Invalid stream SID")
        if stream_sid is not None and status != "answered":
            raise ValueError("Stream SID is only accepted with an answered event")

        active = ("dialing", "dial_outcome_unknown", "answered")
        allowed = (*active, status) if status in terminal else active
        conditions = [CallRecord.id == call_id, CallRecord.status.in_(allowed),
                      or_(CallRecord.exotel_call_sid.is_(None), CallRecord.exotel_call_sid == call_sid)]
        values = {"exotel_call_sid": call_sid, "status": status}
        if status == "answered":
            values["answered_at"] = func.coalesce(CallRecord.answered_at, utc_now())
            if stream_sid is not None:
                conditions.append(or_(CallRecord.stream_sid.is_(None), CallRecord.stream_sid == stream_sid))
                values["stream_sid"] = stream_sid
        else:
            values["completed_at"] = func.coalesce(CallRecord.completed_at, utc_now())

        async with self._session() as session:
            result = await session.execute(update(CallRecord).where(*conditions).values(**values))
            if result.rowcount != 1:
                await session.rollback()
                raise ValueError("Call not found, event out of order, or identifier conflicts")
            await session.commit()
            return await session.get(CallRecord, call_id)


def build_campaign_store() -> CampaignStore:
    """Build a local SQLite store or a deployment-specific async database URI."""
    uri = os.getenv("CAMPAIGN_DATABASE_URI")
    if uri:
        return CampaignStore(uri)

    relative_path = os.getenv("CAMPAIGN_DB_PATH", "data/campaigns.db")
    path = Path(relative_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return CampaignStore(f"sqlite+aiosqlite:///{path}")
