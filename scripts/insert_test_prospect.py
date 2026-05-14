"""
scripts/insert_test_prospect.py

One-off helper: create an event + insert a single hand-crafted prospect
above the threshold so /outreach has exactly one target.

Use this to dry-run + then live-test against a specific real person
(your friend or a fake LinkedIn) without depending on the mock pool.

    python3 -m scripts.insert_test_prospect

Edit the FRIEND dict below before running.
"""
from __future__ import annotations
import sys

# Edit these. linkedin_url must be a real LinkedIn profile URL.
FRIEND = {
    "name": "Friend Real Name",
    "role": "Senior Backend Engineer",
    "company": "Stripe",
    "seniority": "Senior",
    "side": "Builds",
    "works_on": "payments-infra",
    "offers": "Payments-infra depth",
    "seeks": "Founding-engineer scope",
    "gh_stars": 500,
    "x_followers": 1200,
    "linkedin_url": "https://www.linkedin.com/in/<friend-handle>",
}

# Event scaffolding the prospect attaches to. Tweak goal/headcount/etc
# so the generated note frames your event truthfully.
EVENT = {
    "role": "Senior backend engineers",
    "seniority": "Senior",
    "co_stage": "Seed",
    "headcount": 6,
    "format": "Sit-down dinner",
    "city": "San Francisco",
    "goal": "Hiring pipeline",
    "budget": 6000,
}


def main() -> None:
    # late imports so the script doesn't need .env loaded just to parse
    from backend.db import SessionLocal, init_db
    from backend import models

    init_db()
    db = SessionLocal()
    try:
        ev = models.Event(**EVENT)
        db.add(ev)
        db.commit()
        db.refresh(ev)

        # Set a high fit_score so the prospect is above threshold without
        # actually running the scorer (which would need github/x signals).
        # We also set the event.threshold so /outreach's eligibility check passes.
        ev.threshold = 70
        db.commit()

        p = models.Prospect(
            event_id=ev.id,
            identity=FRIEND["linkedin_url"].rstrip("/").split("/")[-1],
            name=FRIEND["name"],
            role=FRIEND["role"],
            company=FRIEND["company"],
            seniority=FRIEND["seniority"],
            side=FRIEND["side"],
            works_on=FRIEND["works_on"],
            offers=FRIEND["offers"],
            seeks=FRIEND["seeks"],
            gh_stars=FRIEND["gh_stars"],
            x_followers=FRIEND["x_followers"],
            li_resolved=True,
            linkedin_url=FRIEND["linkedin_url"],
            sources="manual",
            fit_score=85,
            fit_reason="manually inserted for live test",
            status="approved",
        )
        db.add(p)
        db.commit()
        db.refresh(p)

        print(f"event_id={ev.id}  prospect_id={p.id}")
        print(f"  preview:  curl localhost:8000/events/{ev.id}/outreach/preview")
        print(f"  send:     curl -X POST localhost:8000/events/{ev.id}/outreach")
        print(f"  log:      curl localhost:8000/events/{ev.id}/outreach/log")
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
