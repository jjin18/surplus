"""
routes/relationships.py : read API for the event-native relationship layer.

Surfaces the schema-free timeline + summary built by agents/relationships.py.
Every route is owner-scoped : a prospect is only reachable by the user who owns
its event (same 404-on-not-owned discipline as get_owned_event), so relationship
data never leaks across users.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..agents import relationships
from ..auth import current_user
from ..db import get_db

router = APIRouter(prefix="/api/relationships", tags=["relationships"])


def _owned_prospect(db: Session, prospect_id: int, user: models.User) -> models.Prospect:
    """Fetch a Prospect, requiring `user` to own its event. 404 in both the
    not-found and not-owned cases so we never leak another user's prospects."""
    p = db.get(models.Prospect, prospect_id)
    if p is None:
        raise HTTPException(404, "prospect not found")
    ev = p.event
    if ev is None or getattr(ev, "user_id", None) != user.id:
        raise HTTPException(404, "prospect not found")
    return p


def _prospect_brief(p: models.Prospect) -> dict:
    """Small, safe identity subset : enough for the timeline header, nothing
    sensitive beyond what the CRM already exposes to the host."""
    return {
        "prospect_id": p.id,
        "name": p.name,
        "role": p.role,
        "company": p.company,
        "headline": p.headline,
        "linkedin_url": p.linkedin_url,
        "status": p.status,
        "connection_status": p.connection_status,
        "contact_type": p.contact_type,
        "source": p.source,
        "captured_at": p.captured_at,
    }


@router.get("/prospects/{prospect_id}/timeline")
def prospect_timeline(
    prospect_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """The full relationship timeline + summary for one owned prospect."""
    p = _owned_prospect(db, prospect_id, user)
    return {
        "prospect": _prospect_brief(p),
        "relationship_summary": relationships.relationship_summary(p),
        "timeline": relationships.build_timeline(p),
    }
