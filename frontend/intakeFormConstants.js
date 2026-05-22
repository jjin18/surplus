/** Shared with outbound Intake (App.jsx) and Triage Configure (TriageApp.jsx). */

export const FORMATS = ["Sit-down dinner", "Hackathon", "Workshop", "Mixer", "Roundtable"];

export const GOALS = [
  "Hiring pipeline",
  "Fundraising",
  "Sales pipeline",
  "Product testing",
  "Community density",
];

export const SENIORITY = ["Student", "New grad", "Junior", "Senior", "Staff+", "Leadership"];

export const STAGES_CO = ["Pre-seed", "Seed", "Series A", "Series B+", "Enterprise"];

export const YOE = ["0-2", "3-5", "6-10", "10+"];

// Each prospect source has a backend adapter key (lower-case) and a label.
export const SOURCES = [
  { key: "linkedin", label: "LinkedIn", locked: true },
  { key: "github", label: "GitHub" },
  { key: "scholar", label: "Scholar" },
];

export const FORMAT_CONFIG = {
  "Sit-down dinner": {
    group: "Table",
    topo: "fixed seating : composition locked before doors open",
  },
  Hackathon: { group: "Team", topo: "team formation : complementary skills balanced per team" },
  Workshop: { group: "Breakout", topo: "fluid breakouts : groups regroup between sessions" },
  Mixer: { group: "Cluster", topo: "soft clusters : seeded, not enforced" },
  Roundtable: { group: "Seat", topo: "single ring : seating order is the lever" },
};

/** Default intake profile (matches SurplusApp initial state). */
export const DEFAULT_INTAKE_PROFILE = {
  role: "Infrastructure / ML platform engineers",
  seniority: ["Staff+"],
  coStage: ["Seed"],
  yoe: ["6-10"],
  headcount: 40,
  format: "Sit-down dinner",
  city: "San Francisco",
  eventDate: "",
  eventName: "",
  goal: ["Hiring pipeline"],
  budget: 8000,
  sources: ["linkedin"],
};

/** Map intake Format chip to legacy triage event_type enum for setTriageConfig. */
export const FORMAT_TO_TRIAGE_EVENT_TYPE = {
  "Sit-down dinner": "sponsor_cafe",
  Hackathon: "community_event",
  Workshop: "research_event",
  Mixer: "member_social",
  Roundtable: "partner_dinner",
};
