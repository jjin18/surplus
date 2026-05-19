"""
backend/triage/ : Applicant Triage workflow.

A separate flow from the outbound prospecting pipeline. Operators
(e.g. Verci) upload a Luma CSV of event applicants and surplus
returns accept / maybe / reject recommendations with fit + confidence
scores per applicant. Sponsor-aware : scoring rubric is generated
per-event from the sponsor's goal + ideal-attendee profile, not a
generic 'is this person interesting?' check.

Module layout:
  csv_parser.py    Luma CSV -> normalized applicant dicts (flexible
                   field mapping, preserves custom-question answers)
  rubric.py        per-event/sponsor scoring rubric synthesis        (PR C)
  enrich.py        optional per-applicant enrichment                 (PR C)
  score.py         apply rubric -> ApplicantEvaluation               (PR C)
  recommend.py     evaluation -> accept/maybe/reject/needs_review    (PR C)
  export.py        reviewed CSV exporter                             (PR E)
"""
