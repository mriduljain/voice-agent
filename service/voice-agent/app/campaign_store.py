"""Persistent campaign and call records for local and Cloud Run deployments."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, select
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
