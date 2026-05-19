"""
HTTP routes : one router per mechanism stage.

    events    01       POST /events,                  GET /events/{id}
    pipeline  02-03    POST /events/{id}/run,         GET /events/{id}/prospects
    matching  04       POST /events/{id}/match,       GET /events/{id}/matches
    roi       05       GET  /events/{id}/roi
    triage    06       /events/{id}/triage/...        (Luma CSV applicants)
    curation  07       /events/{id}/curation/...      (CSV-imported attendees,
                                                       5-stage curation flow with
                                                       feature-flagged NEAR-TERM
                                                       extensions)
"""
