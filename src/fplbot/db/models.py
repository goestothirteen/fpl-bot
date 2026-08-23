"""SQLAlchemy models. Postgres stores only what FPL can't tell us: bindings,
preferences, and history needed for deltas. Anything derivable from the API
lives in Redis with a TTL."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Chat(Base):
    __tablename__ = "chat"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(32), default="private")
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Singapore")
    alert_profile: Mapped[str] = mapped_column(String(16), default="big-moments")
    quiet_from: Mapped[int] = mapped_column(Integer, default=1)   # local hour
    quiet_to: Mapped[int] = mapped_column(Integer, default=8)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class League(Base):
    __tablename__ = "league"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16), default="classic")
    scoring: Mapped[str | None] = mapped_column(String(8), nullable=True)
    start_event: Mapped[int] = mapped_column(Integer, default=1)
    admin_entry: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatLeague(Base):
    __tablename__ = "chat_league"

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat.id", ondelete="CASCADE"), primary_key=True
    )
    league_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("league.id", ondelete="CASCADE"), primary_key=True
    )
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    added_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Exactly one default league per chat, enforced by a partial unique index.
    __table_args__ = (
        Index(
            "ix_one_default_league_per_chat",
            "chat_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )


class Manager(Base):
    __tablename__ = "manager"

    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    player_name: Mapped[str] = mapped_column(Text, default="")
    team_name: Mapped[str] = mapped_column(Text, default="")
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LeagueManager(Base):
    __tablename__ = "league_manager"

    league_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("league.id", ondelete="CASCADE"), primary_key=True
    )
    entry_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("manager.entry_id", ondelete="CASCADE"), primary_key=True
    )


class Identity(Base):
    """Links a Telegram human to an FPL team, so the bot can @-mention them."""

    __tablename__ = "identity"

    tg_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    display: Mapped[str | None] = mapped_column(Text, nullable=True)


class PicksCache(Base):
    """Picks are immutable after the deadline, so this is a whole-gameweek cache
    and the single biggest saving in the whole system."""

    __tablename__ = "picks"

    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LiveSnapshot(Base):
    """Last observed per-player stats, for delta detection across restarts."""

    __tablename__ = "live_snapshot"

    event: Mapped[int] = mapped_column(Integer, primary_key=True)
    element: Mapped[int] = mapped_column(Integer, primary_key=True)
    stats: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AlertLog(Base):
    """Idempotency. The bot *will* restart mid-gameweek; posting
    '⚽ Haaland scores!' twice is the most obvious possible bug."""

    __tablename__ = "alert_log"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GWResult(Base):
    """Finalised per-gameweek results, powering /form, /streaks, /season."""

    __tablename__ = "gw_result"

    league_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    net_points: Mapped[int] = mapped_column(Integer, default=0)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    bench_points: Mapped[int] = mapped_column(Integer, default=0)
    transfer_cost: Mapped[int] = mapped_column(Integer, default=0)
    captain_element: Mapped[int | None] = mapped_column(Integer, nullable=True)
    captain_points: Mapped[int] = mapped_column(Integer, default=0)
    chip: Mapped[str | None] = mapped_column(String(16), nullable=True)


class LiveMessage(Base):
    """Self-refreshing /live messages: edit one message rather than flooding."""

    __tablename__ = "live_message"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    league_id: Mapped[int] = mapped_column(Integer)
    event: Mapped[int] = mapped_column(Integer)
    view: Mapped[str] = mapped_column(String(32), default="live")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("chat_id", "message_id", name="uq_live_message"),)
