# Surplus — System Architecture

> The map of what this repo is and how it fits together. Read this first; every
> file below has a one-line purpose so you can open any path and know its job.

## 1. What it is

A FastAPI monolith + a multi-app React (Vite) frontend, served from **one origin**.
Two product surfaces share the codebase:

- **Desktop pipeline** (`www.surpluslayer.com`) — event ROI engine: intake →
  prospecting → outreach → matching → ROI. (`App.jsx`)
- **Phone-first relationship CRM** (`event.surpluslayer.com`) — "your book":
  capture people you meet, auto-detect their updates, draft follow-ups in your
  voice. (`BookApp.jsx`) `/demo` drops into a seeded version of this. Each `/demo`
  visit mints a throwaway `User` with `is_demo=True` (on the real auth/book stack,
  but flagged so it's kept out of real queries/counts); the hourly scheduler
  purges stale demo users (`routes/demo._cleanup_stale_demo_users`, full cascade).

Host header picks the shell: `event.*` → `inperson.html` → `main-inperson.jsx` →
**BookApp**; apex → `index.html` → `main.jsx` → **App**.

- **Marketing landing** (`join.surpluslayer.com`) - the public "Try now"
  page. Ported in-app from the old standalone `roi-engine` FastAPI
  service (which 502'd whenever its Postgres blipped at startup). It is a
  self-contained static `backend/landing/join.html` plus assets served at
  `/landing-assets/*`, with **zero DB dependency** (pure file serve). Host
  routing: any `join.*` host serves the landing instead of the React SPA;
  `event.*` and `www`/apex are unchanged. A host-independent preview lives at
  `/landing` (alias `/join`) for staging verification. The hero "Try now" CTA
  points at `https://event.surpluslayer.com/?signup` (the shared sign-up
  target, below); the secondary email-capture posts to a DB-free
  `/api/join/demo-request` (validate + log, no persistence). See `main.py`
  `_is_landing_host` / `_landing_response`.

- **Sign-up entry (`?signup`)** - the app leads with sign-up, not LinkedIn.
  Every "Sign up now" CTA (BookApp demo banner / draft / tour, CaptureShared
  send-gate, TriageApp, App.jsx sign-in modal) and the landing "Try now" button
  navigate to `/?signup` on their host. Both shells read this param at the app
  root and render `<AuthOptions defaultMode="signup">` ("Create account" with
  email / Google / Microsoft) over any state - signed-out OR demo - so a demo
  visitor can convert without a LinkedIn OAuth bounce. A real signed-in
  (non-demo) user who hits `?signup` falls through to their app. LinkedIn is no
  longer a sign-in door; it stays a CONNECT data-source option after sign-up.

## 1b. The two sides (read this to know which half a file belongs to)

The codebase is two product lines sharing infra. Every backend file belongs to
exactly one of these buckets. (Files are NOT yet physically split into
subpackages — this map is the source of truth for the split.)

### EVENTS side — RETIRED 2026-07-07 (surface unmounted); code DELETED 2026-07-21
The desktop event-ROI pipeline (intake → prospect → outreach → match → ROI,
plus triage & curation) is no longer served: its routers are not mounted in
main.py and `www`/apex now serve the marketing landing. As of 2026-07-21 the
modules themselves are DELETED from the repo (git history keeps them); ALL
tables + data remain (models.py unchanged — Event/Prospect/Applicant/
TriageEnrichmentCache etc. still exist, and capture still creates
Event/Prospect rows). A tripwire test
(test_api.py::test_retired_pipeline_surface_stays_dark) fails if the surface
is ever re-mounted by accident.

Deleted: routes `events`/`pipeline`/`matching`/`roi`/`triage`/`curation`/`jobs`;
`backend/pipeline.py`; `backend/seed.py`; `backend/agents/events/` (prospector,
scorer, matcher, matcher_lib, sponsor_matcher, roi, pair_explainer);
`backend/agents/sources/`; `backend/triage/`; `backend/curation/`;
`backend/matching/`; the Modal triage/prospect/pipeline/match job definitions
in `modal_jobs.py`; and the prospect/match Job dispatch branches in
`backend/jobs.py`.

Two survivors were reclassified to the relationship side:
- `backend/agents/relationship/enrichment_cache.py` — the identity-key kernel
  (`identity_keys` / `_linkedin_slug`), moved out of `backend/triage/` because
  every relationship sync path (email, LinkedIn chat, Google contacts,
  WhatsApp, spine dedup) keys people by it. The triage-only DB read/write half
  (cache_get/cache_put) was dropped; the `TriageEnrichmentCache` table and its
  data are kept.
- `backend/agents/outreach.py` — the invite/DM composer, formerly pipeline
  stage 03b, now relationship-side: the live send flow (pipeline/send/flow.py,
  followup_scheduler, investor_campaign, live_enrich, routes/admin/inperson/
  webhooks) composes through it.

Frontend events-side shells (`App.jsx`, `TriageApp.jsx`, `SharedIntake.jsx`,
`components/MatchingRadarGraph.jsx`) are unreferenced but still on disk.

### RELATIONSHIP side — the phone-first "book" / CRM (`event.*`, `BookApp.jsx`)
Capture people → detect their updates → draft follow-ups in your voice.
- routes: `book` (carries both relationship routers), `inperson`, `followups`
- agents: `book`, `relationships`, `relationship_agent`, `relationship_watch`, `updates_engine`, `updates_scheduler`, `updates_watch`, `drafting`, `reply_agent`, `capture_enrich`, `resolver`, `email_sync`, `message_sink` (shared ingest sink; the device-outbox routes around it were retired 2026-07-21), `send_flow`, `sender`, `followup_scheduler`
- frontend: `BookApp.jsx`, `CaptureShared.jsx`, `main-inperson.jsx`, `components/ContactsButton.jsx`, `components/ContactsPage.jsx`

### SHARED — used by both
- routes: `auth`, `billing`, `demo`, `webhooks`, `admin`
- agents/infra: `llm`, `agent_loop`, `rategate`, `voice`, `exa`, `usage`, `failure_log`, `live_enrich`
- core: `main`, `db`, `models`, `models_monitoring`, `auth`, `schemas`, `config`, `billing_plans`, `jobs`, `hosts`, `rate_limit`, `jsonx`, `metrics`, `reqlog`, `env_loader`, `demo_seed`
- providers: `base`, `unipile`, `brightdata`
- frontend lib/components: `lib/*`, `UpgradePaywall`, `surplusTheme`, `intakeFormConstants`

`main.py` mounts its routers in these three groups (with section headers) so the
split is visible at the entrypoint.

## 2. Deploy topology

- **Railway** runs the web service (`railway.json` → `Dockerfile`, multi-stage:
  build frontend with Node, serve via uvicorn). Env: `production` (branch `main`,
  `event.surpluslayer.com`) + `staging` (branch `demo`). 2 replicas. Cloudflare in front.
  - **Deploy healthcheck posture**: Railway probes `/api/health` with a 600s
    `healthcheckTimeout` window. The in-process scheduler threads sleep an
    initial delay before their first claim (default 420s,
    `UPDATES_SCHEDULER_INITIAL_DELAY_SECONDS`) so a fresh container is healthy
    before it spends CPU on sweeps: a heavy first gathering pass during boot
    starved `/api/health` on the single worker and failed deploy 247f9eb2
    (2026-07-01). Steady-state cadence is unaffected (claims are shared, an
    already-running replica keeps ticking).
- **Modal** (`modal_jobs.py`, app `surplus-jobs`) runs off-box batch + scheduled
  jobs when `USE_MODAL=1` (CRM refresh, the on-connect WhatsApp first sync,
  detached seeds, the hourly updates sweep, investor outreach). Secrets:
  `surplus-jobs` (DB/Anthropic/etc) + `surplus-brightdata`.
- **Postgres** (Railway) in prod; SQLite (`backend/data/surplus.db`) for local dev.
  Schema migrations are inline idempotent `_migrate_*()` functions in `db.py`
  (no Alembic).
- Prod DB from a laptop: use the Postgres service's `DATABASE_PUBLIC_URL`
  (`zephyr.proxy.rlwy.net`), not the internal `DATABASE_URL`.

## 3. Request lifecycle

`main.py` (FastAPI app + lifespan) mounts the routers, CORS, request-log
middleware, and serves the SPA. Auth is **session-cookie** based: LinkedIn via
Unipile hosted-auth → `User` row → `current_user` dependency. No passwords.
`lifespan` runs `init_db()` (migrations) and starts the in-process scheduler
threads (updates/gathering sweeps + the punctual follow-up dispatcher, §6c).

## 4. Subsystems (backend/)

### Core (`backend/*.py`)
- `main.py` — app, lifespan, middleware, SPA routing, health/diagnostics.
- `db.py` — engine, `SessionLocal` (autoflush=False), `get_db()`, inline migrations.
- `models.py` — ORM schema (~25 tables: Event, Prospect, Contact, RelationshipInteraction, Conversion, MatchEdge, User, Session, Applicant, Job, …).
- `models_monitoring.py` — MonitoredPerson / HostPersonLink (continuous-enrichment dedup).
- `auth.py` — sessions, cookies, `current_user`, send kill-switches.
- `schemas.py` — Pydantic request/response shapes.
- `config.py` — policy tables (funnel/follow-up/format/goal levers).
- `billing_plans.py` — plan tiers + metered-usage limits.
- `pipeline.py` — stage 02–03 orchestrator (prospect + outreach).
- `jobs.py` — job dispatch: local BackgroundTask vs Modal (`use_modal()`).
- `hosts.py` — in-person host detection. `rate_limit.py` — per-IP limiter.
- `jsonx.py` — robust JSON extraction from LLM output. `metrics.py` / `reqlog.py` — request/LLM stats + logging. `env_loader.py` — load .env first.
- `demo_seed.py` — demo workspace bootstrap. (`seed.py`, the events-pipeline dev CLI, was deleted with the events side.)

### Routes (`backend/routes/`) — all mounted in `main.py`
- `auth.py` — LinkedIn/email sign-in (Unipile), session, `/api/me`, onboarding, **auto-import on connect** (background worker seeds the Book from genuine DM conversations AND auto-syncs the host's voice from their own sent messages via `live_enrich.sync_host_voice_on_connect` — same ban-safe own-account read, idempotent). The WhatsApp connect webhook dispatches its first conversation sync DURABLY off the request lifecycle via `jobs.dispatch_whatsapp_first_sync` (Modal `run_whatsapp_first_sync` when `USE_MODAL`, else a daemon thread that owns its own DB session): minutes of Unipile I/O can't run in the webhook thread or it gets killed mid-sync. `whatsapp_sync` fetches each chat's attendees+messages concurrently (bounded `ThreadPoolExecutor`, read-only HTTP) then ingests single-threaded; idempotent by message id.
- `book.py` — the WHOLE relationship-side surface, two routers in one module. `/api/book` (BookApp): `/today` feed, `/draft`(+stream), `/ask`(+stream), `/relationship/{id}` — THE canonical contact-detail endpoint (absorbed the old `/api/relationships/contacts/{id}`; spine payload rides along as `contact_summary`/`events`/`spine_timeline`), `run-updates` sweep, `_diagnostics` (admin: one call returning `{status, updates}` — request/LLM/rate-gate health + updates-engine cutover state; absorbed `_status` and `_updates-status`), `_updates-test`, `_draft-preview` (admin: composes drafts across a user's top contacts + the "natural move" reasoning, to inspect messaging quality — read-only, bounded). `/api/relationships` (`relationships_router`): contact spine read API, star/VIP, email threads/channel, **import-conversations**, chat(+stream), followup/schedule, snooze.
- `relationships.py` — thin re-export shim for the old module path (`router` = `book.relationships_router`; all other attributes delegate to `book.py`).
- `demo.py` — token-gated demo entry + public walkthrough.
- `inperson.py` - phone capture (QR/paste/manual). **Scan fast-path**: `POST
  /api/inperson/scan` does only the DB upsert and returns immediately with
  `draft_status="pending"`; the slow half (Unipile resolve + enrichment + draft
  compose, `finish_scan_capture`) runs detached on its own DB session
  (`jobs.run_detached`) and the UI polls `GET /scan/{id}/draft` until
  `ready`/`failed`.
- `followups.py` — scheduled follow-up queue (Gmail-style). `billing.py` — Stripe. `admin.py` — token-gated ops. `webhooks.py` — Unipile / Bright Data / Stripe ingestion.
- `civic.py` — Civic policy search (§5b). `/api/civic/ask` + `/api/civic/answer/{id}`, and the map page at `/civic`. No DB, no auth, no session : the only shared state is a 24h in-process answer cache.

### Agents / logic (`backend/agents/`)
LLM + business logic. Infra: `llm.py` (Anthropic client + models), `agent_loop.py`
(multi-turn tool loop), `rategate.py` (concurrency gate), `voice.py` (host voice
extraction/matching), `exa.py` (Exa search), `jsonx` use.

**The relationship / "what's new" system (current focus):**
- `relationships.py` — event-native **read model** (timeline, contact_summary, list_contacts) + `import_conversation_contacts()`. *(distinct from routes/relationships.py)*
- `updates_engine.py` — **the updates orchestrator**: `run_sweep` (Bright Data primary → Exa fallback), `due_contacts` (vip=daily/others=weekly tiering), `apply_profile`/`apply_posts` (diff + baseline-first), `autodraft` (drafts only `_DRAFTWORTHY_KINDS`).
- `updates_scheduler.py` — in-process daemon that claims+runs the sweep hourly (shared `scheduler_claims` row dedups with Modal).
- `updates_watch.py` — Exa fallback search. `relationship_watch.py` — Unipile CRM poller; `_emit()` writes every `activity_update` **and fires autodraft** (single choke point).
- `drafting.py` — the one voice-matched follow-up composer (`compose_followup`/`compose_batch`/stream), used by autodraft, book, and the agent. A draft runs a **4-stage pipeline** (full design in `docs/draft-pipeline.md`) so the per-person honing is principled, not an accreting pile of prompt clauses:
  - **① GATHER** (`build_context` → `_relationship_facts`, all DB reads on the request thread): the host's **packaged voice** (`voice.build_voice_context` — distilled `<host_voice_profile>` + ground-truth `<style_examples>`, channel-scoped), **person facts** (name/role/company), the **real prior thread**, **relationship grounding** (met where/when, the host's own noted next step, stage, relationship types), **their most recent detected update** + the **real content behind it** (`latest_update_detail` = actual post text / role detail, so a draft says "your iHeartRadio feature on The Hospitality Reset", not "saw your post"), the contact's **register** (`voice.detect_register`), and low-confidence **About** (`about`, graceful read — no-op until enrichment populates it).
  - **② RESOLVE** (`_resolve_voice`, `_natural_action` / `Intent`): collapse the competing voice signals into ONE instruction by precedence — **FORMAL register > thread dynamic > host voice profile** (formal is a hard no-emoji constraint that must outrank the casual host voice even mid-thread; the thread mirror is for non-formal threads). The message's GOAL comes from an optional **`Intent`** (hybrid: a taxonomy `kind` from `INTENT_KINDS` + a free-form `objective` + optional `must`/`avoid`) passed by the caller; when none is passed, the goal is derived from `_natural_action` (deliver-on-promise / react-to-update / reply-when-they-spoke-last / re-engage-stale) exactly as before. This is the seam that lets the SAME engine write any message (congratulate / intro / ask / thank / schedule / ...), not just a follow-up — the relationship agent will eventually decide an `Intent` and hand it here instead of drafting inline (see `docs/draft-pipeline.md`).
  - **③ SELECT** (`_select_grounding`): order facts strongest-first and gate by confidence — **verified** facts (their update, your open loop, where you met) may be asserted; **low-confidence color** (what they work on) is offered as optional, so anti-fabrication is structural rather than a prompt plea.
  - **④ RENDER** (`_user_prompt`): assemble the user message from the resolved situation; the system prompt carries the resolved voice. Brevity (2-3 sentences) + use-only-stated-facts are enforced here.

  The host's free-form **ask-bar instruction** threads through as a shared `directive` (`compose_from_context`/`compose_batch`/`stream_from_context`): `/ask`+`/ask/stream` pass the typed query so one intent ("mention the webinar Thursday") lands in every draft, while the per-person `reason` + facts keep each message differentiated rather than a pasted line.
- `messaging_eval.py` — repeatable quality eval for the composer (messaging is the crux). A fixed scenario set (voiced/no-voice, recent update, open loop, live thread, stale, formal, cold) → real drafts → deterministic gates (no em dash / concise / not-generic) + an LLM judge (voice_match, specificity, correct_intent, natural, 1-5). `python -m backend.agents.messaging_eval [--runs N] [--dump out.json]` prints a per-case scorecard; `--pairwise base.json new.json` runs a position-randomized head-to-head judge (lower-variance than the absolute 1-5 means, which are ceiling-limited). Run before/after any prompt or context change to catch regressions — dump both, then pairwise. Baseline ~voice 4.2 / spec 3.7 / intent 4.5 / natural 4.6, gates clean; the 4-stage pipeline holds this at parity (48% pairwise vs the pre-pipeline composer) and turns formal-register adaptation from the known weak spot into a win (4-1).
- `relationship_agent.py` — propose-only multi-turn CRM agent (the /ask bar). When a call is the move, a `draft_message` also carries a meeting `booking_payload` (scheduling link / proposed time woven into the body); the booking fires only when the draft is SENT (see §6b).
- `book.py` — BookApp "today" engine: health scoring + update detection + `build_today` feed (drafts surfaced first).

**The account layer (company-wide relationship graph — design: `docs/accounts-architecture.md`):**
- Thesis: the company account is a LENS over individual graphs, never a bucket. Company rows are GLOBAL + public-data-only + pipeline-owned (user corrections live in per-user `CompanyOverlay`); everything relationship-flavored stays per-user and team views are assembled at query time through gates (wall -> compliance profile -> owner sharing level), so walls are instant/provable and leaving a team removes edges with nothing to claw back.
- Models (end of `models.py`): `Company`/`CompanyIdentity` (global, mirrors ContactIdentity: domain + linkedin_company strong keys, name_norm weak/never-automerge), `CompanyOverlay`, `AccountMembership` (per-user TIME-BOUNDED person<->company edge; job change = close+reopen), `Account` (per-owner view: tier/objective/sharing_level + cached rollups), `Team`/`TeamMembership` (compliance_profile: collaborative default / strict = Level-1 ceiling + pending view interlock; share_signals kill switch), `Wall` (ethical wall: bidirectional invisibility incl. counts, beats every level, query-layer enforcement).
- `company_resolve.py` — person->company resolution: strong keys (non-freemail domain, linkedin company id) auto-link at 1.0; company-name / headline extraction ("X at Acme") deterministic-first, LLM disambiguation below threshold -> `pending_review`. `backfill()` with dry-run report; `scripts/backfill_accounts.py`.
- Law-firm readiness (shipped): `agents/relationship/audit.py` -> `TeamAuditLog` (append-only team-plane trail: every gated read with counts, every wall/policy/membership change; mutations commit ATOMICALLY with their audit row, reads are best-effort; admin `GET /api/teams/{id}/audit`, itself audited). `conflict_import.py` + `routes/team_conflicts.py` -- deterministic conflict-list import per docs/accounts-architecture.md §6b: parse in code, provisional name-walls written before review, coverage invariant (every line lands in an accounted state), confirm converts single-match walls to entity walls + unlocks `view_state`, audited skip. Delete semantics: graph rows cascade; compliance rows (teams/walls/audit) survive their creator via SET NULL (`_migrate_fk_cascade` actions).
- `accounts_read.py` + `routes/accounts.py` — the owner's account read model (members warmest-first via `score_health`, unioned timeline, coverage/single-threaded, rollup recompute). `routes/teams.py` + `team_view.py` — the team plane: Level-1 metadata-only aggregates ({member, contact, warmth band, recency band} — never content), gates enforced pre-aggregation.

**Composer:** `outreach.py` (relationship-side invite/DM composer, formerly pipeline stage 03b).
**Messaging:** `reply_agent.py` (inbound DM classify, propose-only) `sender.py` `send_flow.py` `followup_scheduler.py` `email_sync.py`.
**Enrichment:** `capture_enrich.py` `live_enrich.py` `resolver.py`.
**Utils:** `failure_log.py` `usage.py`.

### Providers (`backend/providers/`)
- `base.py` — `LinkedInProvider` contract + payload/result types + dash hygiene.
- `unipile.py` — Unipile (sends, profile/posts reads, chats, relations, **list_active_conversation_contacts**).
- `brightdata.py` — Bright Data scraper client (async profile/posts trigger → webhook).

### Other backend dirs
- `triage/` — applicant intake pipeline (CSV → ICP → enrich → score → review).
- `curation/` — event-curation (capture, enrich, draft, score, attribution).
- `matching/` — symbiotic matching (ingest, rubric, GitHub enrich, matrix, explain).
- `data/` — `prospect_pool.json` (mock pool), `surplus.db` (local SQLite).

## 5. Frontend (frontend/)

- Entries: `main.jsx` → `App.jsx` (desktop); `main-inperson.jsx` → `BookApp.jsx` (phone).
- Apps: `App.jsx` (5-stage pipeline), `BookApp.jsx` (relationship CRM), `TriageApp.jsx` (inbound), `SharedIntake.jsx` (unified intake), `CaptureShared.jsx` (capture/in-person).
- Shared: `lib/api.js` (all endpoints), `lib/labels.js` `lib/notify.js` `lib/analytics.js` `lib/resilience.jsx`; components `UpgradePaywall` `ContactsButton` `ContactsPage` `MatchingRadarGraph`; `surplusTheme.js` / `intakeFormConstants.js`.
- Build: Vite multi-page (`vite.config.js`); BookApp kept in its own chunk for health-fingerprint tracking.

## 5b. Civic policy search (`/civic`) — standalone surface

A separate product living in the same process, sharing nothing with the CRM
but the process and the two API keys it already has. A resident drops a pin on
a 3D satellite map, asks why something is happening there, and gets an answer
whose sources are ranked by how they were produced.

Files:
- `backend/civic.py` — the engine: prompt, the evidence ladder, URL grounding,
  schema coercion, the 24h answer cache. No FastAPI, no DB.
- `backend/civic_sources.py` — retrieval: ten backends queried in parallel,
  deduplicated, snippet-capped, tier-classified by host, and cached 5 minutes.
- `backend/routes/civic.py` — HTTP: rate limit (10/min/IP), cache lookup,
  permalinks, the page.
- `backend/civic_ui/index.html` — the whole client, one file. MapLibre GL from
  a CDN over keyless tiles (Esri World Imagery + AWS terrarium terrain) and
  Nominatim for geocoding, so the map needs no key of its own. If WebGL or the
  library is missing, the page degrades to a typed-location form.

**Retrieval is a fan-out, and that is the whole latency story.** Handing Claude
a `web_search` tool and letting it look is slow for a structural reason: every
search is a serial round-trip — the model decides, the search runs, the results
land in context, the model re-reads all of it and decides again. A dozen of
those is minutes of wall-clock, and both the context and the bill grow with
each one. So `civic_sources.gather()` does the searching: every backend at
once, deduplicated, snippet-capped (700 chars), and tier-classified by host
before the model reads a word. Breadth costs threads, not seconds.

| Rung | Backend | Key | Covers |
|---|---|---|---|
| A | GovTrack | — | US federal bills, by name and status |
| A | UK Parliament Bills | — | bills before Parliament |
| A | OpenStates | `OPENSTATES_API_KEY` (free) | all 50 US state legislatures |
| A | Federal Register | — | US rules, proposed rules, notices |
| A | data.gov catalogue | — | the dataset behind the number |
| B | OpenAlex, Crossref | — | papers and DOIs, worldwide |
| E | GDELT | — | news in 100+ languages (rate-limits hard) |
| E | Google News RSS | — | the second opinion when GDELT says slow down |
| F | Hacker News, Reddit | — | what people are arguing about |
| * | Exa | `EXA_API_KEY` | neural search across all six rungs |

Every backend is failure-isolated: one that is down, rate-limited, or has
changed shape contributes nothing, is recorded in `civic_sources.LAST_RUN`, and
the answer is built from what did come back. A 429 from a free API is weather
rather than a fault — one short backoff, then the ladder is built from the
others — and each backend's answer is cached per process for five minutes,
which is what keeps two replicas from hammering the same free endpoints.
Claude's own `web_search` is the safety net for when *everything* returns
empty, capped at `CIVIC_MAX_SEARCHES` (3).

**An answer with an empty ladder is refused, not rendered.** Two failures used
to produce a confident page with six grey "nothing found at this level" rungs
under it: a `web_search` tool error (which arrives as HTTP 200 with an error
object inside the result block, not as an exception) and a model that answers
from memory when its searches came back empty. Both now raise — the reader
gets "the search itself failed, here is why", never an unsourced answer wearing
the ladder's authority.

**The map is a lens on a stack of governments.** Every address sits inside
seven or eight of them at once — congressional district, two state legislative
districts, county, city, council district, school district, land use — each
with its own election, its own money and its own powers, and residents
experience them as one blur. `backend/civic_geo.py` names that stack for a
point from two keyless sources (the US Census Geocoder for the districts
nobody can name from memory; OpenStreetMap's `is_in` for the council district
and the relation ids an outline needs), and carries **what each layer actually
decides** — the part a resident cannot look up. Picking a lens in the rail
paints that boundary in its colour, scopes the map's pins to the things that
layer governs, and asks its question in its own vocabulary: a school board
question is about budgets, boundaries and closures; a state-senate question is
about preemption and the funding formula. `GET /api/civic/jurisdictions`
returns the stack, `GET /api/civic/outline` the geometry (by OSM relation, or
by name for the Census-named districts that arrive without a shape).

The legal zoning code is not in OpenStreetMap, which knows how land is *used*
rather than what an ordinance permits — so the land-use layer says so, and two
things close most of the gap without a per-city parcel integration. First,
**the ordinance is treated as primary law**: municipal-code hosts (Municode,
American Legal, eCode360, Sterling, legislation.gov.uk) classify as tier A,
Exa gets a rung pinned to those domains, and the prompt requires a section
citation — "§17.13.040 caps height at 45 feet" — rather than a description of
the rule. Second, **each layer carries its own site and the one thing worth
looking up on it** (`website` off the boundary relation, `LOOKUPS` per layer):
a tax question ends at the assessor's record for your parcel and a
what-may-be-built question ends at the city's permit portal, and no synthesis
replaces the reader clicking through. A per-city parcel API would add
lot-level zoning designations and permit history on tap — worth it for a
launch city, not required for the answer to be right.

**Two speeds, because two different questions.** Tapping something on the map
is answered in about a second and never reaches the model: `whatsHere()` asks
OpenStreetMap which boundaries the point falls inside (`is_in`) and what stands
on it, and the card names them — city, council district, county, state, the
district that runs the school — alongside the building's own facts. Then
`GET /api/civic/place` fetches recent coverage from the two news backends
only: no synthesis, no ladder, no cost. The button on the card asks the
question that point raises, naming those bodies ("what zoning, property-tax and
rent rules apply to 1418 Ninth Street in Oakland, Alameda County, California"),
which is what makes the answer about an address rather than about a city. The
box on the left stays the deliberate path for a question of your own.

**Dropping a pin is a briefing.** The page asks about the place ~900ms after
the pin settles, without waiting for anyone to type, and sends `brief: true` —
which tells the model to report what is live there rather than ask for a
narrower question. A typed question always outranks the pending briefing.
Zoomed past `SITES_ZOOM`, the map also draws what is physically there from
OpenStreetMap (construction, city halls, courts, schools, hospitals, transit,
data centres, substations, parks) as labelled pins; clicking one asks the
question that thing raises, by name and street. Keyless, instant, and free —
the fastest honest answer on the page.

**A long search loop pauses the turn.** With server-side tools the API can
return `stop_reason: "pause_turn"` — searching done, answer unwritten — and
expects the assistant content back to continue. `_create()` resumes up to
`MAX_TURNS` (4), keeping every turn's blocks so citations found before the
pause still count as grounded.

Latency, and what is done about it. A question is 15-40s: searches, then a
JSON answer written a token at a time. `POST /api/civic/ask/stream` reports the
work as server-sent events — each search, the moment writing starts, and the
**headline as soon as it exists** (it is the first key in the JSON, so it lands
seconds into the write). The page reads that instead of running a timer.
`POST /api/civic/ask` stays as the non-streaming path and the fallback for a
proxy that eats event streams. On the `web_search` fallback each search is a
serial round-trip, so it is capped at 3 and a second pass needs the answer to
be thinner (`MIN_TIERS_FALLBACK`) than it does when we searched ourselves.

`GET /api/civic/selftest` answers "why is every question failing" without the
deploy logs, in two tiers: anyone gets the configuration and the *shape* of the
last failure (`NotFoundError`, `404`), which is what diagnoses a broken deploy;
the upstream error text and `?probe=1` (ten outbound calls) need
`X-Admin-Token`, because unauthenticated error strings are an information leak
and an unauthenticated fan-out is a cost-DoS — the same pair this repo already
fixed once for `/api/diagnostics` (security review H-2). The token is checked
locally against `ADMIN_TOKEN` rather than through `routes/admin`, which takes a
DB session Civic must not have. Production runs two replicas behind one
URL and its counters are per-process, so it carries a `boot` id — refresh until
you have seen both. Every failure is also printed as one greppable line
(`[civic] error boot=… type=… status=…`), which makes the aggregated deploy log
the shared error store, and is optionally POSTed to `CIVIC_ERROR_WEBHOOK`. It
reports: how the surface is configured, which model is actually in use,
and the last upstream failure (type, status, truncated message — no keys, no
question text). Two failures are self-healing or self-explaining: a model this
account cannot use falls back to the one `agents/llm.py` already runs in
production, and a missing web_search entitlement with no `EXA_API_KEY` says so
in the answer instead of quietly answering from memory — an unsourced answer
being precisely what this surface exists to prevent.

Flow: `POST /api/civic/ask {question, location, lat, lon}` → retrieve →
one Claude call → strip fences → schema-validate → **drop any evidence or
action whose URL is not in the source set** → if the answer spans fewer than
three tiers, search once more (harder at tiers A and B) and keep whichever
pass reached further. Answers are cached by `sha256(question|location)` for
24h and shared as `/civic/r/{id}`.

**An answer with an empty ladder is refused, not rendered.** Two failures used
to produce a confident page with six grey "nothing found at this level" rungs
under it: a `web_search` tool error (which arrives as HTTP 200 with an error
object inside the result block, not as an exception) and a model that answers
from memory when its searches came back empty. Both now raise — the reader gets
"the search itself failed, here is why", never an unsourced answer wearing the
ladder's authority.

The rules that must not drift into being suggestions live in `validate()`, not
in the prompt: a citation the search never returned is dropped; the tier comes
from the publisher, so a Reddit thread cannot be cited as official data and a
think tank cannot be cited as peer-reviewed; tier F is never support for a
claim; there is exactly one two-minute action.

**Isolation — what stops it affecting the CRM.** Civic is a bolt-on, so it is
fenced rather than trusted:

- **No shared state.** No DB, no session, no user, no models. The only app
  module it imports is `rate_limit`; `tests/test_civic.py` asserts that by
  parsing the imports, so the surface cannot quietly grow into the product.
- **Mounted defensively.** `main.py` wraps the include in try/except: a
  failure inside Civic logs `[civic] not mounted` and the app boots anyway.
- **Capped threads.** A synthesis holds a threadpool thread for 15-25s and
  that pool is the CRM's (`WEB_CONCURRENCY=1` by default). At most
  `CIVIC_MAX_CONCURRENCY` (2) run at once; past that a request is shed with a
  503, never queued — queueing is what would hold the threads.
- **Bounded request time.** 75s per model call, and the second search only
  runs if the first finished inside 45s.
- **Capped spend.** `CIVIC_DAILY_ANSWERS` (250) uncached answers per process
  per UTC day is the ceiling on what Civic can take of the shared Anthropic /
  Exa keys and their rate limits. Cache hits don't count.
- **Off switch.** `CIVIC_ENABLED=0` returns 404 for the page and 503 for the
  API — an env change, not a deploy.

Env: `ANTHROPIC_API_KEY` (required — without it `/api/civic/ask` returns 503
and says why), `EXA_API_KEY` (optional, switches on the retrieval path),
`CIVIC_MODEL` (default `claude-sonnet-5`, falling back to `claude-sonnet-4-6`
if the account cannot use it), `CIVIC_MAX_SEARCHES` (default 5, only used on
the `web_search` fallback), `CIVIC_EFFORT` (unset; `low` trades depth for
speed), plus the four caps above.

## 6. The updates → draft → Book flow (end to end)

1. **Scheduler** (Modal hourly primary, in-process fallback; claim-deduped) calls `run_sweep`.
2. `due_contacts` picks who's due (⭐ vip daily / others weekly, via `watched_at`).
3. Bright Data scrapes each contact's public profile/posts on its own infra → delivers to `/webhooks/brightdata`. Posts use `only_authored_posts=true` (their own posts only, not the activity feed) — keeps the signal clean and slashes credit burn (a non-poster = 0 records).
4. `apply_profile`/`apply_posts` diff vs baseline (first scrape = silent baseline) → `_emit` an `activity_update`.
5. `_emit` auto-drafts a follow-up **for important kinds only** (`job_change`, milestone `new_post`) in the host's voice.
6. `/api/book/today` surfaces draft-bearing updates **first**, with the ready message inline.

## 6b. Meeting booking (a side effect of SENDING a draft)

When a CALL is the natural next step, surplus can book the meeting itself. Booking is **coupled to the draft+send flow**, not a standalone agent action: the agent's draft carries the scheduling offer in its text **and** a structured booking payload, and the actual calendar event fires **when that draft is sent**.

- **Availability** (`integrations/booking.find_open_slot`): reads the host's connected calendar (Google or Outlook, via the same `fetch_calendar_events` the read-sync uses) and returns the earliest open, business-hours, timezone-aware slot over the next ~5 business days. Never double-books. Host tz defaults to `SURPLUS_BOOKING_TZ` (no per-user tz column yet); business hours / lead time are env-tunable (`SURPLUS_BOOKING_START_HOUR` / `_END_HOUR` / `_MIN_LEAD_HOURS`).
- **Draft-time decision** (`booking.propose_meeting_slot`): Calendly connected -> put the self-serve link in the draft (the link IS the booking); else propose a concrete open slot in the draft text. The relationship agent (`pipeline/agent/run.py`) detects a meeting cue on a `draft_message`, precomputes the slot **on the main thread** (the fan-out can't touch the DB session), appends the link/time to the body, and attaches the payload to the staged `Proposal` (surfaced as `booking_payload`).
- **Booking action** (`booking.agent_book_meeting`): picks/uses a slot, invites the **contact** at their email (`Contact.email` else a strong `ContactIdentity` of kind `email`), attaches a **Zoom** link when Zoom is connected (else native Meet/Teams), and records a `meeting_booked` `RelationshipInteraction`. **Idempotent** (a live future booking for that contact is returned, never duplicated) and **email-required** (no email -> raises, so the message still sends with just the text/link, no broken attendee-less event).
- **Send fires it** (`pipeline/send/sender.fire_booking_on_send`): every send path (`/api/relationships/contacts/{id}/schedule` send-now, `/api/followups/{id}/send-now`, and the `run-followups` cron) calls this **after** a clean dispatch. A `propose_time` payload creates the event+invite; a `calendly` payload is a no-op (the link in the body is the booking). A booking miss never fails the message that already went out. The payload rides on `ScheduledFollowup.booking_payload` (new nullable column) for scheduled/cron sends.
- **The gate** (the general-send master `SURPLUS_AUTOMATED_SENDS`, OFF by default; see §6c): **manual** (default) -> the agent drafts the message with the link/time and stages it; nothing books until the HOST approves/sends, at which point send fires the booking. **automatic** (master on) -> the dispatcher auto-sends and auto-books, no approval. No surprise invites for anyone while the flag is off.

## 6c. Send automation (who may send with no human, and how it dispatches)

Three kinds of automated send, two env gates (both OFF by default in code;
per-channel allowlist `SURPLUS_AUTOMATED_SEND_CHANNELS` applies to both):

1. **Post-accept first follow-up** - BUILT-IN product behavior: when an invite
   is accepted, `webhooks._trigger_auto_dm` sends the first DM. Pre-authorized
   by the host's own action (they sent the invite), so it has its own master:
   `SURPLUS_AUTO_FOLLOWUPS` (`sender.follow_up_send_enabled`). A clean send
   auto-stages the later nudge (`followup_scheduler.stage_followup`).
2. **The later nudge** ("checking in" after no reply) - agent autonomy, NOT a
   built-in. Gated by the general-send master `SURPLUS_AUTOMATED_SENDS`
   (`sender.automated_send_enabled`), shared with:
3. **AI auto-reply** to an inbound DM - same general-send master.

Manual UI sends (send-now, approve-a-draft) never pass through either gate.
The per-user `users.auto_followups_enabled` column is LEGACY: it gates neither
staging nor dispatch, its settings routes/UI toggle are gone, and only a few
relationships.py approve/schedule paths still read it (False for new users).

**Dispatch topology**: due `ScheduledFollowup` rows are sent by the in-process
`followup-dispatch` daemon thread (`updates_scheduler`), which ticks every
~60s, claim-guarded via `scheduler_claims`, and calls
`admin.dispatch_due_followups` directly: punctual sends, no external
dependency. The GitHub Actions `run-followups` cron (hits `POST
/api/admin/run-followups`) is redundancy only. Idempotent either way: each row
flips to sent/cancelled/failed the moment it's processed. Gate off -> due rows
HOLD (stay `scheduled` for a manual send-now); a reply cancels; rows overdue
past ~7 days expire as `stale`.

## 6d. Gathering (conversation context the drafter reads)

The per-contact message context is kept fresh by three entry points into the
same idempotent syncs (`linkedin_chat_sync` + `email_sync`, `message_sink` (shared ingest sink; the device-outbox routes around it were retired 2026-07-21), both bounded and
watermarked: LinkedIn by `users.linkedin_chat_synced_at`, dedup by Unipile
message id):

1. **On connect** (`auth._autoimport_conversations`): two durable background
   passes the moment a LinkedIn seat connects: the conversation seed
   (contacts + host voice from the most active chats) and the FULL LinkedIn
   chat sync (message bodies into each contact's timeline). Same
   magic-moment pattern as the WhatsApp first sync: connect -> the book fills
   itself.
2. **Gathering sweep** (`updates_scheduler.run_gathering_sweep`): every 6h
   (`GATHERING_SWEEP_GAP_SECONDS`, claim-guarded, capped at
   `GATHERING_SWEEP_USER_LIMIT` users/pass), runs the INCREMENTAL LinkedIn DM
   sync + email correspondents re-sync for every user with an active seat.
3. **Admin backfill** (`POST /api/admin/sync-linkedin-chats`): on-demand
   dispatch for one user or all active seats; `incremental=false` forces a
   full re-scan (write-idempotent). `POST /api/admin/backfill-contact-links`
   links legacy contact-less prospects so their conversations become visible
   to the relationship layer.

## 7. Conventions

- Commit/push only when asked; prod deploys on `main`.
- Sends are gated by kill-switches + billing; never auto-send without the user.
- LinkedIn reads go through the user's **own** Unipile account or Bright Data's infra — never the host account (ban-safe).
