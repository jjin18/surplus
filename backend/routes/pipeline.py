"""routes/pipeline.py — stage 02-03. Prospecting and outreach, split."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..pipeline import run_prospect, run_outreach_stage, run_pipeline
from ..agents.outreach import compose
from ..providers import get_provider

router = APIRouter(prefix="/events", tags=["02-03 · pipeline"])


def _wipe_prior_prospects(db: Session, ev: models.Event) -> None:
    """Cascade-delete every prospect (and their outreach + conversions)."""
    for p in list(ev.prospects):
        db.delete(p)
    db.commit()


@router.post("/{event_id}/prospect", response_model=schemas.PipelineResult)
async def prospect_only(event_id: int, db: Session = Depends(get_db)):
    """
    Stage 02 + 03a only: fan-out + score + threshold. No outreach.

    Marks every prospect 'approved' or 'below'. Idempotent — wipes prior
    prospects (and their outreach + conversions) first.
    """
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")

    _wipe_prior_prospects(db, ev)
    prospects = await run_prospect(db, ev)
    return schemas.PipelineResult.build(ev, prospects)


@router.post("/{event_id}/outreach", response_model=schemas.OutreachRunResult)
def outreach_only(event_id: int, db: Session = Depends(get_db)):
    """
    Stage 03b only: provider-backed outreach for everyone 'approved'.

    DRY_RUN is the default (UNIPILE_DRY_RUN=true). Idempotent — wipes prior
    outreach logs and resets contacted/rsvp back to approved before re-running.
    409s if /prospect has not been called or if no prospect passed the threshold.
    """
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")
    if not ev.prospects:
        raise HTTPException(409, "no prospects — call /prospect first")
    targets = [p for p in ev.prospects
               if p.status in ("approved", "contacted", "rsvp")]
    if not targets:
        raise HTTPException(409, "no approved prospects to contact — "
                                 "threshold may be too high for the pool")

    results = run_outreach_stage(db, ev)
    return schemas.OutreachRunResult.build(ev, list(ev.prospects), results)


@router.post("/{event_id}/run", response_model=schemas.PipelineResult)
async def run(event_id: int, db: Session = Depends(get_db)):
    """
    Convenience: /prospect + /outreach back-to-back. Idempotent.
    """
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")

    _wipe_prior_prospects(db, ev)
    prospects = await run_pipeline(db, ev)
    return schemas.PipelineResult.build(ev, prospects)


@router.get("/{event_id}/prospects", response_model=schemas.PipelineResult)
def get_prospects(event_id: int, db: Session = Depends(get_db)):
    """Read the resolved pool without re-running the pipeline."""
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")
    if not ev.prospects:
        raise HTTPException(409, "pipeline has not been run for this event yet")
    return schemas.PipelineResult.build(ev, ev.prospects)


@router.get("/{event_id}/outreach/preview", response_model=schemas.OutreachPreview)
def outreach_preview(event_id: int, db: Session = Depends(get_db)):
    """
    Show exactly what would be sent for each approved prospect, WITHOUT
    invoking the provider or mutating state.

    For each approved prospect: composed note + message, eligibility flag,
    and (if eligible) the full provider payload that would be POSTed.
    """
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")
    if not ev.prospects:
        raise HTTPException(409, "no prospects — call /prospect first")

    provider = get_provider()
    targets = [p for p in ev.prospects
               if p.status in ("approved", "contacted", "rsvp")]
    peers = targets  # composition reveal uses everyone passing the threshold

    rows: list[schemas.OutreachPreviewRow] = []
    for p in targets:
        eligible = True
        skip_reason = None
        if not p.linkedin_url:
            eligible = False
            skip_reason = "no linkedin_url"
        msg = compose(p, ev, peers=[q.name for q in peers if q.id != p.id])
        lead = provider.build_lead_payload(p, ev, note=msg.note, message=msg.message)
        # Provider-agnostic preview: dry-run is forced to true to guarantee no
        # network call, and the payload that *would* be POSTed is captured on
        # the ProviderResult. Works for any provider.
        payload = None
        if eligible:
            # If the live provider isn't already in dry-run, use a throwaway
            # dry-run instance of the SAME class for the preview.
            preview_provider = provider
            if not provider.dry_run:
                preview_provider = type(provider)(dry_run=True)  # type: ignore[call-arg]
            result = preview_provider.send_connection(lead)
            payload = result.payload
        rows.append(schemas.OutreachPreviewRow(
            prospect_id=p.id,
            name=p.name,
            company=p.company,
            linkedin_url=p.linkedin_url,
            fit_score=p.fit_score,
            eligible=eligible,
            skip_reason=skip_reason,
            note=msg.note,
            note_chars=len(msg.note),
            message=msg.message,
            payload=payload,
        ))

    return schemas.OutreachPreview(
        event_id=ev.id,
        provider=provider.name,
        dry_run=provider.dry_run,
        count_eligible=sum(1 for r in rows if r.eligible),
        count_skipped=sum(1 for r in rows if not r.eligible),
        prospects=rows,
    )


@router.get("/{event_id}/outreach/log", response_model=schemas.OutreachLogResult)
def outreach_log(event_id: int, db: Session = Depends(get_db)):
    """
    Per-event outreach timeline. One entry per OutreachLog row, sorted by
    (prospect, ts). The dashboard renders this as the per-prospect funnel.
    """
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")

    return schemas.OutreachLogResult.build(ev)
