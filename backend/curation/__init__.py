"""
curation/ : the audience-ingest workflow.

Where the existing pipeline (backend/pipeline.py + backend/agents/) handles
outbound prospecting + Claude-driven discovery, this module handles the
inverse flow : the operator brings their OWN audience (alumni, member
roster, past attendees, RSVPs, nominees), and the engine curates + matches +
activates + measures from there.

Five stages map 1:1 to the brief:

  Stage 1 INGEST       csv_import.py + enrichment.py
  Stage 2 CURATE/SCORE scoring.py + gap_analysis.py + features.NEAR_TERM
  Stage 3 MATCH        intros.py (+ near-term: sponsor/seating/sessions)
  Stage 4 ACTIVATE     outreach.py
  Stage 5 MEASURE ROI  attribution.py + AttendeeFollowUp (+ near-term rollups)

All Claude calls go through claude_log.log_call() so prompt + output land
in the LLMCall audit table. Every scored / attributed row stores its
reasoning. NEAR-TERM features are gated by features.is_enabled().
"""
