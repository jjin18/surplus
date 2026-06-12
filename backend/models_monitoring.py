"""models_monitoring.py : the continuous-enrichment ("keep the relationship book
fresh") schema, kept in its own module so it doesn't entangle models.py.

Two GLOBAL, dedup-keyed tables. A real LinkedIn person is stored ONCE
(MonitoredPerson, keyed by the stable member_id), and HostPersonLink records
which hosts are connected to them -- so a mutual shared by many hosts is fetched
once and the freshness update (job change / new post) fans out to every linked
host's contact. This is what makes the tactful, per-account-rate-limited poller
scale: dedup the work, pool the fetch capacity.

Output reuses the existing RelationshipInteraction `activity_update` feed (one
row per linked host's contact); these tables only hold the shared cache + the
rotation/adaptive state the poller needs.

Registered with Base.metadata via an import in db.init_db (before create_all), so
the tables are created automatically -- no migration needed (new tables only).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MonitoredPerson(Base):
    """ONE row per real LinkedIn person we keep enriched -- GLOBAL, deduped by
    member_id. Fetched once; freshness fans out to every linked host."""
    __tablename__ = "monitored_persons"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable LinkedIn id = THE dedup key. public_identifier can change (vanity-URL
    # edits) so it's matched on but never keyed. provider_id is the encoded id
    # some Unipile endpoints require.
    member_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    public_identifier: Mapped[Optional[str]] = mapped_column(String(160), default=None, index=True)
    provider_id: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    name: Mapped[str] = mapped_column(String(160), default="")

    # Job-change signal -- diff last_headline across weekly relations syncs.
    last_headline: Mapped[str] = mapped_column(Text, default="")
    headline_changed_at: Mapped[Optional[datetime]] = mapped_column(default=None)

    # Posts signal -- snapshot of the most recent ORIGINAL post seen (dedup posts).
    last_post_id: Mapped[Optional[str]] = mapped_column(String(160), default=None)
    last_post_at: Mapped[Optional[datetime]] = mapped_column(default=None)

    # Rotation cursor + adaptive cadence (deprioritize people who never post).
    posts_checked_at: Mapped[Optional[datetime]] = mapped_column(default=None, index=True)
    post_checks: Mapped[int] = mapped_column(default=0)
    empty_streak: Mapped[int] = mapped_column(default=0)

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)


class HostPersonLink(Base):
    """Which host is connected to which MonitoredPerson (built from each host's
    relations list). Drives fan-out (update -> every linked host's contact) AND
    fetch-assignment (any linked host's account may fetch this person)."""
    __tablename__ = "host_person_links"
    __table_args__ = (UniqueConstraint("host_user_id", "member_id",
                                       name="uq_host_person"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    host_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[str] = mapped_column(String(120), index=True)  # -> MonitoredPerson.member_id
    contact_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), default=None, index=True)

    # Importance to THIS host -> rotation weight (higher = fresher cadence).
    priority: Mapped[int] = mapped_column(default=50)
    connected_at: Mapped[datetime] = mapped_column(default=_utcnow)
    # Last weekly sync that still saw this link -> prune removed connections.
    last_seen_in_relations_at: Mapped[Optional[datetime]] = mapped_column(default=None)
