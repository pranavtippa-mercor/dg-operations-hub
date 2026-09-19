export type Evidence = {
  at?: string;
  by?: string;
  seen_at?: string;
  text?: string;
  url?: string;
  due?: string;
  source?: string;
  quote?: string;
  captured_at?: string;
};
export type Confirmation = {
  state: string;
  asked?: string;
  asked_by?: string;
  asked_url?: string;
  deadline?: string;
  writer?: string;
  reply?: string;
  reply_by?: string;
  reply_text?: string;
  reply_url?: string;
};
export type Task = {
  id: string;
  name: string;
  cohort: string;
  artifact: string;
  domain: string;
  stage: string;
  status_id: string;
  rfd: boolean;
  owner: string;
  owner_id: string | null;
  last_actor: string;
  updated_at: string | null;
  transitioned_at: string | null;
  activity_conflict: boolean;
  blocked: boolean;
  deadline: { at: string; source: string; url?: string; raw: string } | null;
  deadline_history: {
    assigned?: Evidence;
    committed?: Evidence;
    moves?: Evidence[];
  };
  slack_activity: Evidence | null;
  confirmation: Confirmation | null;
  warning: { warned_at?: string; note?: string } | null;
  studio: string;
  slack: string | null;
};
export type Dimension = {
  label: string;
  f: number[];
  g: number[];
  n: number[];
  t: number[];
};
export type Module = {
  name: string;
  f: number[];
  d: number[];
  t: number[];
  dims: Dimension[];
};
export type Snapshot = {
  schema_version: 1;
  generated_at: string;
  mode: string;
  tasks: Task[];
  modules: {
    generated_at_utc: string;
    tz: string;
    window_start_local: string;
    window_end_local: string;
    n_hours: number;
    hours?: string[];
    excluded_other_world: number;
    modules: Module[];
  };
  sources: Record<
    string,
    { at: string | null; cadence_minutes: number | null; label: string }
  >;
  history: {
    at: string;
    c: Record<string, number>;
    t: Record<string, string>;
  }[];
  conflicts: string[];
};
export const COHORTS: Record<string, string> = {
  all: "All work",
  docx: "DOCX EE",
  pptx: "PPTX EE",
  xlsx: "XLSX EE",
  tpl: "Template base",
  html: "HTML EE",
};
export const DOMAINS: Record<string, string> = {
  BUS: "Business",
  DNA: "Data & analytics",
  ENG: "Engineering",
  FIN: "Finance",
  LAW: "Legal",
  MAR: "Marketing",
  OPS: "Operations",
  SUP: "Supply chain",
};
const POOL = new Set([
  "Unclaimed",
  "Ready for Review",
  "Ready for Audit",
  "Awaiting Runs & Trajectories Review",
  "RFD Audit Queue",
]);
const STALE = new Set([
  "Red zone",
  "No update since claim",
  "Stale · review reassignment",
  "Stale · follow up",
]);
export const elapsed = (at: string | null | undefined, now: number) =>
  at && Number.isFinite(Date.parse(at))
    ? Math.max(0, (now - Date.parse(at)) / 3600000)
    : null;
export const isReady = (t: Task) => t.rfd || t.stage === "RFD Audit Passed";
export const isParked = (t: Task) => ["Delivered", "Frozen"].includes(t.stage);
export const threshold = (t: Task) =>
  POOL.has(t.stage)
    ? 2
    : t.stage.startsWith("Stage 1 - Writing")
      ? 12
      : t.stage.startsWith("Stage 2 - Runs")
        ? 6
        : 4;
export function signal(t: Task, now: number) {
  if (isReady(t)) return "Ready";
  if (isParked(t)) return t.stage;
  if (t.blocked) return "Blocked";
  if (t.activity_conflict) return "Activity needs refresh";
  const age = elapsed(t.updated_at, now),
    stage = elapsed(t.transitioned_at, now),
    h = threshold(t);
  if (
    age === null ||
    stage === null ||
    Date.parse(t.updated_at!) > now + 60000 ||
    Date.parse(t.transitioned_at!) > now + 60000
  )
    return "Activity unknown";
  let state = POOL.has(t.stage)
    ? stage >= 2
      ? "Queue aging"
      : "In queue"
    : Math.abs(Date.parse(t.updated_at!) - Date.parse(t.transitioned_at!)) <
          72000 && age >= h / 2
      ? "No update since claim"
      : age >= 2 * h
        ? "Stale · review reassignment"
        : age >= h
          ? "Stale · follow up"
          : "Active";
  if (STALE.has(state) && t.deadline) {
    const left = (Date.parse(t.deadline.at) - now) / 3600000;
    const window = Math.max(
      (Date.parse(t.deadline.at) - Date.parse(t.transitioned_at!)) / 3600000,
      0.5,
    );
    if (left <= 0 || left / window < 0.25) state = "Red zone";
  }
  return state;
}
export const isStale = (t: Task, now: number) => STALE.has(signal(t, now));
export const attention = (t: Task, now: number) =>
  isStale(t, now) ||
  [
    "Queue aging",
    "Blocked",
    "Activity needs refresh",
    "Activity unknown",
  ].includes(signal(t, now));
export const overdue = (t: Task, now: number) =>
  !isReady(t) &&
  !isParked(t) &&
  !!t.deadline &&
  Date.parse(t.deadline.at) < now;
export function priority(t: Task, now: number) {
  const s = signal(t, now),
    sev: Record<string, number> = {
      "Red zone": 4,
      "No update since claim": 3,
      "Stale · review reassignment": 2.5,
      "Stale · follow up": 1.5,
      "Queue aging": 1.2,
      Blocked: 1,
    };
  const distance = t.stage.startsWith("Stage 1 - Writing")
    ? 9
    : t.stage === "Unclaimed"
      ? 10
      : t.stage.includes("RFD Audit")
        ? 1
        : t.stage === "In Audit"
          ? 2
          : t.stage.includes("Audit")
            ? 3
            : t.stage.includes("Runs")
              ? 5
              : 7;
  const urgency: Record<string, number> = {
    html: 1,
    docx: 0.6,
    pptx: 0.6,
    xlsx: 0.3,
    tpl: 0.5,
  };
  let score =
    (sev[s] || 0) *
    (urgency[t.cohort] || 0.4) *
    (1 + (11 - distance) / 10) *
    (1 +
      Math.min(
        (elapsed(POOL.has(t.stage) ? t.transitioned_at : t.updated_at, now) ||
          0) / threshold(t),
        6,
      ));
  if (t.deadline && t.transitioned_at) {
    const window = Math.max(
      Date.parse(t.deadline.at) - Date.parse(t.transitioned_at),
      1800000,
    );
    score *=
      1 +
      2 *
        (1 -
          Math.min(Math.max(0, Date.parse(t.deadline.at) - now) / window, 1));
  } else score *= 1.1;
  return score;
}
export function ageLabel(hours: number | null) {
  if (hours === null) return "—";
  if (hours < 1) return `${Math.floor(hours * 60)}m`;
  if (hours < 48) return `${hours.toFixed(1)}h`;
  return `${(hours / 24).toFixed(1)}d`;
}
export function dateLabel(at: string | null | undefined, full = false) {
  if (!at || !Number.isFinite(Date.parse(at))) return "Not available";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Los_Angeles",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    ...(full ? { year: "numeric", timeZoneName: "short" } : {}),
  }).format(new Date(at));
}
export function safeUrl(value: string | null | undefined) {
  if (!value) return undefined;
  try {
    const u = new URL(value);
    return u.protocol === "https:" ? u.href : undefined;
  } catch {
    return undefined;
  }
}
export function csv(rows: unknown[][]) {
  return rows
    .map((row) =>
      row
        .map((value) => {
          let s = String(value ?? "");
          if (/^[=+@\-\t\r]/.test(s)) s = "'" + s;
          return '"' + s.replaceAll('"', '""') + '"';
        })
        .join(","),
    )
    .join("\r\n");
}
export function validateSnapshot(value: unknown): Snapshot {
  const fail = () => {
    throw Error(
      "Invalid or incomplete operations snapshot. Your previous data is retained.",
    );
  };
  const obj = (x: unknown): x is Record<string, unknown> =>
    !!x && typeof x === "object" && !Array.isArray(x);
  const str = (x: unknown) => typeof x === "string";
  const timestamp = (x: unknown) =>
    typeof x === "string" &&
    /Z$|[+-]\d{2}:\d{2}$/.test(x) &&
    Number.isFinite(Date.parse(x));
  const optional = (x: unknown) => x === null || x === undefined || str(x);
  const evidence = (x: unknown) => {
    if (x == null) return;
    if (!obj(x)) fail();
    for (const [k, v] of Object.entries(x as Record<string, unknown>))
      if (
        [
          "at",
          "by",
          "seen_at",
          "text",
          "url",
          "due",
          "source",
          "quote",
          "captured_at",
          "warned_at",
          "note",
        ].includes(k) &&
        !optional(v)
      )
        fail();
  };
  if (!obj(value)) fail();
  const s = value as Snapshot;
  if (
    s.schema_version !== 1 ||
    !timestamp(s.generated_at) ||
    !str(s.mode) ||
    !Array.isArray(s.tasks) ||
    !s.tasks.length ||
    s.tasks.length > 5000 ||
    !obj(s.sources) ||
    !Array.isArray(s.history) ||
    !obj(s.modules) ||
    !Array.isArray(s.modules.modules) ||
    !Array.isArray(s.conflicts)
  )
    fail();
  for (const key of [
    "tasks",
    "activity",
    "confirmations",
    "slack",
    "modules",
  ]) {
    const v = s.sources[key];
    if (
      !obj(v) ||
      !str(v.label) ||
      (v.at !== null && !timestamp(v.at)) ||
      (v.cadence_minutes !== null &&
        (typeof v.cadence_minutes !== "number" || v.cadence_minutes <= 0))
    )
      fail();
  }
  for (const h of s.history) {
    if (
      !obj(h) ||
      !timestamp(h.at) ||
      !obj(h.c) ||
      !obj(h.t) ||
      Object.values(h.c).some(
        (n) => typeof n !== "number" || !Number.isInteger(n) || n < 0,
      ) ||
      Object.values(h.t).some((v) => !str(v))
    )
      fail();
  }
  const ids = new Set<string>();
  for (const t of s.tasks) {
    if (!obj(t)) fail();
    for (const k of [
      "id",
      "name",
      "cohort",
      "artifact",
      "domain",
      "stage",
      "status_id",
      "owner",
      "last_actor",
      "studio",
    ] as const)
      if (!str(t[k])) fail();
    if (
      ids.has(t.id) ||
      !COHORTS[t.cohort] ||
      t.cohort === "all" ||
      typeof t.rfd !== "boolean" ||
      typeof t.blocked !== "boolean" ||
      typeof t.activity_conflict !== "boolean" ||
      !optional(t.owner_id) ||
      !optional(t.slack) ||
      t.stage === "Discarded"
    )
      fail();
    ids.add(t.id);
    for (const d of [t.updated_at, t.transitioned_at])
      if (d !== null && !timestamp(d)) fail();
    if (t.deadline !== null) {
      if (
        !obj(t.deadline) ||
        !timestamp(t.deadline.at) ||
        !str(t.deadline.source) ||
        !str(t.deadline.raw) ||
        !optional(t.deadline.url)
      )
        fail();
    }
    if (!obj(t.deadline_history)) fail();
    evidence(t.deadline_history.assigned);
    evidence(t.deadline_history.committed);
    if (t.deadline_history.moves !== undefined) {
      if (!Array.isArray(t.deadline_history.moves)) fail();
      t.deadline_history.moves.forEach(evidence);
    }
    evidence(t.slack_activity);
    evidence(t.warning);
    if (t.confirmation !== null) {
      if (
        !obj(t.confirmation) ||
        !str(t.confirmation.state) ||
        Object.values(t.confirmation).some((v) => !optional(v))
      )
        fail();
    }
  }
  const n = s.modules.n_hours;
  if (
    !Number.isInteger(n) ||
    n < 1 ||
    n > 1000 ||
    !str(s.modules.window_start_local) ||
    !str(s.modules.window_end_local) ||
    !timestamp(s.modules.generated_at_utc) ||
    typeof s.modules.excluded_other_world !== "number"
  )
    fail();
  if (
    !Array.isArray(s.modules.hours) ||
    s.modules.hours.length !== n ||
    s.modules.hours.some(
      (h, i) =>
        !timestamp(h) ||
        (i > 0 &&
          Date.parse(h) - Date.parse(s.modules.hours![i - 1]) !== 3600000),
    )
  )
    fail();
  const names = new Set<string>();
  for (const m of s.modules.modules) {
    if (!obj(m) || !str(m.name) || names.has(m.name) || !Array.isArray(m.dims))
      fail();
    names.add(m.name);
    for (const d of m.dims) if (!obj(d) || !str(d.label)) fail();
    for (const r of [
      { f: m.f, d: m.d, t: m.t },
      ...m.dims.map((d) => ({ f: d.f, d: d.g, n: d.n, t: d.t })),
    ]) {
      const arrays = [r.f, r.d, ...("n" in r ? [r.n as number[]] : [])];
      if (
        arrays.some(
          (a) =>
            !Array.isArray(a) ||
            a.length !== n ||
            a.some((x) => !Number.isInteger(x) || x < 0),
        ) ||
        r.f.some((f, i) => f > r.d[i]) ||
        !Array.isArray(r.t) ||
        r.t.length !== arrays.length ||
        arrays.some((a, i) => a.reduce((x, y) => x + y, 0) !== r.t[i])
      )
        fail();
    }
  }
  return s;
}
export function flaggedSince(
  task: Task,
  history: Snapshot["history"],
  now: number,
) {
  if (!isStale(task, now)) return null;
  let since: string | null = null;
  for (const h of [...history].reverse()) {
    if (!["NS", "SR", "SW", "RZ"].includes(h.t[task.id] || h.t[task.name]))
      break;
    since = h.at;
  }
  return since;
}
export function moduleHours(s: Snapshot["modules"]) {
  return (s.hours || []).map((at) =>
    new Intl.DateTimeFormat("en-US", {
      timeZone: "America/Los_Angeles",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
      timeZoneName: "short",
    })
      .format(new Date(at))
      .replace(",", ""),
  );
}
