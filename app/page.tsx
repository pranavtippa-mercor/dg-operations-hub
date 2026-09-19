"use client";
import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  COHORTS,
  DOMAINS,
  ageLabel,
  attention,
  csv,
  dateLabel,
  elapsed,
  flaggedSince,
  isParked,
  isReady,
  isStale,
  moduleHours,
  overdue,
  priority,
  safeUrl,
  signal,
  threshold,
  validateSnapshot,
  type Snapshot,
  type Task,
} from "../lib/model";
import { isEnvelope, unlock, type Envelope } from "../lib/crypto";
const CONF: Record<string, string> = {
  confirmed: "Confirmed",
  at_risk: "At risk",
  replied: "Replied",
  awaiting: "Awaiting reply",
  no_request: "No request",
  not_checked: "Not checked",
};
type Notes = Record<string, { note: string; at: string }>;
function loadNotes(): Notes {
  if(typeof window === "undefined") return {};
  try {
    const value = JSON.parse(
      localStorage.getItem("dg-operations-local-notes") || "{}",
    );
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(
      Object.entries(value).filter(
        ([, v]) =>
          v &&
          typeof v === "object" &&
          "note" in v &&
          typeof v.note === "string" &&
          "at" in v &&
          typeof v.at === "string",
      ),
    ) as Notes;
  } catch {
    return {};
  }
}
function loadPercent() {
  if(typeof window === "undefined") return true;
  try {
    return localStorage.getItem("dg-operations-percent") !== "false";
  } catch {
    return true;
  }
}
function download(name: string, content: string, type = "text/plain") {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([content], { type }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
function Link({
  href,
  children,
}: {
  href?: string | null;
  children: React.ReactNode;
}) {
  const url = safeUrl(href);
  return url ? (
    <a href={url} target="_blank" rel="noreferrer">
      {children} ↗
    </a>
  ) : null;
}
function Badge({ value }: { value: string }) {
  return (
    <span
      className={
        "badge " +
        (value === "Red zone" || value === "Blocked" || value === "At risk"
          ? "red"
          : value.includes("Stale") ||
              value === "Queue aging" ||
              value === "No update since claim"
            ? "amber"
            : ["Ready", "Delivered", "Confirmed"].includes(value)
              ? "green"
              : "neutral")
      }
    >
      {value}
    </span>
  );
}
function metric(n: number, d: number, percent: boolean) {
  return d ? (percent ? `${((100 * n) / d).toFixed(1)}%` : `${n}/${d}`) : "—";
}
export default function Home() {
  const [data, setData] = useState<Snapshot | null>(null),
    [tab, setTab] = useState("tasks"),
    [view, setView] = useState("completion"),
    [cohort, setCohort] = useState("all"),
    [domain, setDomain] = useState("all"),
    [stage, setStage] = useState("all"),
    [owner, setOwner] = useState("all"),
    [filter, setFilter] = useState("all"),
    [confirm, setConfirm] = useState("all"),
    [search, setSearch] = useState(""),
    [sort, setSort] = useState("priority"),
    [ascending, setAscending] = useState(true),
    [page, setPage] = useState(1),
    [selected, setSelected] = useState<string | null>(null),
    [now, setNow] = useState(() => Date.now()),
    [notes, setNotes] = useState<Notes>(loadNotes),
    [note, setNote] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [feed, setFeed] = useState(false),
    [envelope, setEnvelope] = useState<Envelope | null>(null),
    [password, setPassword] = useState(""),
    [percent, setPercent] = useState(loadPercent),
    [moduleSearch, setModuleSearch] = useState(""),
    [failuresOnly, setFailuresOnly] = useState(false),
    [expanded, setExpanded] = useState<Set<string>>(new Set()),
    [from, setFrom] = useState(0),
    [to, setTo] = useState(9999);
  const mode = useRef<"feed" | "import" | "locked">("feed"),
    epoch = useRef(0),
    latest = useRef<Snapshot | null>(null),
    inflight = useRef(false);
  const key = useRef(""),
    remote = useRef(false),
    detail = useRef<HTMLDialogElement>(null),
    lastFetch = useRef("");
  const accept = useCallback((s: Snapshot) => {
    if (latest.current)
      for (const [name, source] of Object.entries(latest.current.sources)) {
        const next = s.sources[name];
        if (
          source.at &&
          (!next?.at || Date.parse(next.at) < Date.parse(source.at))
        )
          throw Error(
            "This snapshot contains older source evidence. The newer snapshot is retained.",
          );
      }
    latest.current = s;
    setData(s);
    setError("");
    setNow(Date.now());
    setPage(1);
  }, []);
  const refresh = useCallback(
    async (manual = false) => {
      if (mode.current !== "feed" || inflight.current) return;
      inflight.current = true;
      const ticket = epoch.current;
      if (manual) setBusy(true);
      try {
        const local = ["localhost", "127.0.0.1"].includes(location.hostname);
        const endpoint = local
          ? "./__local/snapshot"
          : "https://raw.githubusercontent.com/pranavtippa-mercor/dg-operations-hub/dashboard-data/snapshot.enc.json";
        const response = await fetch(endpoint, { cache: "no-cache" });
        if (!response.ok) {
          if (remote.current || manual)
            throw Error(
              "No updated snapshot is available. Your last valid data is still shown.",
            );
          return;
        }
        const text = await response.text();
        if (
          ticket !== epoch.current ||
          mode.current !== "feed" ||
          text === lastFetch.current
        )
          return;
        const doc = JSON.parse(text);
        if (isEnvelope(doc)) {
          setFeed(true);
          remote.current = true;
          if (!key.current) {
            setEnvelope(doc);
            return;
          }
          const opened = await unlock(doc, key.current);
          if (ticket !== epoch.current || mode.current !== "feed") return;
          accept(opened);
          setEnvelope(null);
        } else if (local) accept(validateSnapshot(doc));
        else throw Error("Hosted snapshots must be encrypted.");
        lastFetch.current = text;
        remote.current = true;
        setFeed(true);
      } catch (e) {
        if (ticket === epoch.current && (manual || remote.current))
          setError(
            e instanceof Error
              ? e.message
              : "Refresh failed. Last valid snapshot retained.",
          );
      } finally {
        inflight.current = false;
        if (manual) setBusy(false);
      }
    },
    [accept],
  );
  useEffect(() => {
    const params = new URLSearchParams(location.hash.slice(1));
    key.current = params.get("key") || "";
    if (key.current)
      history.replaceState(null, "", location.pathname + location.search);
    // Mount starts an external fetch; synchronous busy state is only used by manual calls.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
    const timer = setInterval(() => {
        if (!document.hidden) void refresh();
      }, 300000),
      clock = setInterval(() => setNow(Date.now()), 60000);
    const visible = () => {
      if (!document.hidden) void refresh();
    };
    document.addEventListener("visibilitychange", visible);
    return () => {
      clearInterval(timer);
      clearInterval(clock);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [refresh]);
  const task = data?.tasks.find((t) => t.id === selected);
  const openTask = (t: Task) => {
    setNote(notes[t.id]?.note || t.warning?.note || "");
    setSelected(t.id);
  };
  useEffect(() => {
    if (task) {
      if (!detail.current?.open) detail.current?.showModal();
    } else detail.current?.close();
  }, [task]);
  async function importFile(file?: File) {
    if (!file) return;
    const ticket = ++epoch.current;
    mode.current = "import";
    if (file.size > 15000000) {
      setError("Snapshot is too large (maximum 15 MB).");
      return;
    }
    try {
      const doc = JSON.parse(await file.text());
      if (ticket !== epoch.current) return;
      if (isEnvelope(doc)) {
        setEnvelope(doc);
        setPassword("");
        setFeed(false);
        remote.current = false;
      } else {
        accept(validateSnapshot(doc));
        remote.current = false;
        setFeed(false);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to read snapshot.");
    }
  }
  async function doUnlock(e: React.FormEvent) {
    e.preventDefault();
    if (!envelope) return;
    setBusy(true);
    const ticket = epoch.current;
    try {
      const opened = await unlock(envelope, password);
      if (ticket !== epoch.current) return;
      accept(opened);
      key.current = password;
      setPassword("");
      setEnvelope(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const base = useMemo(
    () =>
      data?.tasks.filter(
        (t) =>
          (cohort === "all" || t.cohort === cohort) &&
          (domain === "all" || t.domain === domain) &&
          (stage === "all" || t.stage === stage) &&
          (owner === "all" || t.owner === owner) &&
          (!search ||
            [t.name, t.owner, t.stage, t.domain]
              .join(" ")
              .toLowerCase()
              .includes(search.toLowerCase())),
      ) || [],
    [data, cohort, domain, stage, owner, search],
  );
  const filtered = useMemo(
    () =>
      base
        .filter(
          (t) =>
            (confirm === "all" ||
              (t.confirmation?.state || "not_checked") === confirm) &&
            (filter === "all" ||
              (filter === "attention" && attention(t, now)) ||
              (filter === "stale" && isStale(t, now)) ||
              (filter === "overdue" && overdue(t, now)) ||
              (filter === "ready" && isReady(t)) ||
              (filter === "unclaimed" && !t.owner_id) ||
              (filter === "missing" &&
                !t.deadline &&
                !isReady(t) &&
                !isParked(t)) ||
              (filter === "active" && !isReady(t) && !isParked(t))),
        )
        .sort((a, b) =>
          sort === "priority"
            ? priority(b, now) - priority(a, now) ||
              a.name.localeCompare(b.name)
            : sort === "updated"
              ? (Date.parse(a.updated_at || "") || Infinity) -
                (Date.parse(b.updated_at || "") || Infinity)
              : sort === "due"
                ? (Date.parse(a.deadline?.at || "") || Infinity) -
                  (Date.parse(b.deadline?.at || "") || Infinity)
                : sort === "moved"
                  ? (Date.parse(a.transitioned_at || "") || Infinity) -
                    (Date.parse(b.transitioned_at || "") || Infinity)
                  : sort === "confirmation"
                    ? (a.confirmation?.state || "not_checked").localeCompare(
                        b.confirmation?.state || "not_checked",
                      )
                    : (
                        a[sort as "name" | "owner" | "stage"] || ""
                      ).localeCompare(
                        b[sort as "name" | "owner" | "stage"] || "",
                      ),
        ),
    [base, confirm, filter, now, sort],
  );
  const ordered = ascending ? filtered : [...filtered].reverse();
  const pages = Math.max(1, Math.ceil(filtered.length / 40)),
    currentPage = Math.min(page, pages),
    visible = ordered.slice((currentPage - 1) * 40, currentPage * 40);
  const pick = (setter: (x: string) => void, v: string) => {
    setter(v);
    setPage(1);
  };
  const summary = {
    rfd: base.filter((t) => t.rfd).length,
    delivered: base.filter((t) => t.stage === "Delivered").length,
    stale: base.filter((t) => isStale(t, now)).length,
    overdue: base.filter((t) => overdue(t, now)).length,
  };
  function exportTasks() {
    download(
      "dg-tasks.csv",
      csv([
        [
          "Task",
          "Cohort",
          "Domain",
          "Stage",
          "Holder",
          "Signal",
          "Last update",
          "Stage transition",
          "Due PT",
          "Deadline source",
          "Confirmation",
          "Studio",
          "Slack",
        ],
        ...filtered.map((t) => [
          t.name,
          COHORTS[t.cohort],
          t.domain,
          t.stage,
          t.owner,
          signal(t, now),
          t.updated_at,
          t.transitioned_at,
          t.deadline ? dateLabel(t.deadline.at, true) : "",
          t.deadline?.source,
          CONF[t.confirmation?.state || "not_checked"],
          t.studio,
          t.slack,
        ]),
      ]),
      "text/csv",
    );
  }
  function exportDigest() {
    const groups = Object.groupBy(
      filtered.filter((t) => attention(t, now) || overdue(t, now)),
      (t) => t.domain,
    );
    download(
      "dg-follow-up-drafts.md",
      `# Follow-up drafts\n\nEvidence: Studio ${dateLabel(data?.sources.tasks.at, true)}; activity ${dateLabel(data?.sources.activity.at, true)}.\nDrafts only; nothing has been sent. Flags use generic Studio update timestamps and require review.\n\n` +
        Object.entries(groups)
          .map(
            ([d, ts]) =>
              `## ${DOMAINS[d] || d}\n\n` +
              (ts || [])
                .map(
                  (t) =>
                    `- ${t.name} — ${t.owner}: ${signal(t, now)}; stage ${t.stage}; last Studio update ${dateLabel(t.updated_at, true)}; deadline ${dateLabel(t.deadline?.at, true)}.\n  Draft: Hi ${t.owner === "Unclaimed" ? "team" : t.owner}, could you share the current status of ${t.name} and your expected next step?\n  ${t.studio}${t.slack ? "\n  " + t.slack : ""}`,
                )
                .join("\n"),
          )
          .join("\n\n"),
    );
  }
  const moduleData = data?.modules,
    hours = moduleData ? moduleHours(moduleData) : [],
    end = Math.min(to, hours.length - 1),
    start = Math.min(from, end),
    range = hours.slice(start, end + 1),
    sum = (a: number[]) => a.slice(start, end + 1).reduce((x, y) => x + y, 0);
  const modules = (moduleData?.modules || [])
    .filter(
      (m) =>
        m.name.toLowerCase().includes(moduleSearch.toLowerCase()) &&
        (!failuresOnly || sum(m.f) > 0),
    )
    .sort((a, b) => (sum(b.f) / sum(b.d) || 0) - (sum(a.f) / sum(a.d) || 0));
  const totalF = modules.reduce((n, m) => n + sum(m.f), 0),
    totalD = modules.reduce((n, m) => n + sum(m.d), 0),
    worst = modules.filter((m) => sum(m.d) >= 10)[0];
  const previous = data?.history.at(
      data.history.at(-1)?.at === data.sources.tasks.at ? -2 : -1,
    ),
    previousCodes = previous?.t || {},
    cleared = base.filter(
      (t) =>
        ["NS", "SR", "SW", "RZ"].includes(
          previousCodes[t.id] || previousCodes[t.name],
        ) &&
        !isStale(t, now) &&
        !t.activity_conflict,
    );
  const trend = (data?.history || [])
    .slice(-36)
    .map((h) => ({
      at: h.at,
      count: [
        "RED ZONE",
        "NEVER STARTED",
        "STALE — REASSIGN",
        "STALE — WARN",
      ].reduce((n, k) => n + (h.c[k] || 0), 0),
    }));
  const sourceOld = data
    ? ["tasks", "activity"].some((k) => {
        const s = data.sources[k];
        return (
          !s?.at ||
          (elapsed(s.at, now) || 0) > ((s.cadence_minutes || 5) * 3) / 60
        );
      })
    : false;
  return (
    <main className="shell">
      <header className="site-header">
        <a className="brand" href="./">
          <span className="mark">D</span>
          <span>
            Doppelganger <small>OPERATIONS</small>
          </span>
        </a>
        <div className="header-actions">
          <span className="tag">
            <i /> {data ? "Private data loaded" : "Private workspace"}
          </span>
          {data && (
            <button
              className="quiet"
              onClick={() => {
                epoch.current++;
                mode.current = "locked";
                latest.current = null;
                setData(null);
                key.current = "";
                setSelected(null);
                lastFetch.current = "";
                setFeed(false);
                remote.current = false;
                setEnvelope(null);
              }}
            >
              Lock
            </button>
          )}
        </div>
      </header>
      <section className="intro">
        <div>
          <span className="eyebrow">ONE WORKSPACE. THE FULL PICTURE.</span>
          <h1>Keep work moving.</h1>
          <p>Completion, task staleness, and module health. Together.</p>
        </div>
        <div className="intro-actions">
          {data && (
            <>
              <span className="small">
                Studio checked {dateLabel(data.sources.tasks?.at)}
              </span>
              <button
                className="button"
                onClick={() => {
                  if (mode.current === "import")
                    setError(
                      "This is an imported snapshot. Import a newer file or reconnect under Data & refresh.",
                    );
                  else void refresh(true);
                }}
                disabled={busy}
              >
                {busy ? "Checking…" : "↻ Check for updates"}
              </button>
            </>
          )}
        </div>
      </section>
      <nav aria-label="Workspace">
        <button
          className={tab === "tasks" ? "active" : ""}
          onClick={() => setTab("tasks")}
        >
          Tasks <span>{data?.tasks.length || "—"}</span>
        </button>
        <button
          className={tab === "modules" ? "active" : ""}
          onClick={() => setTab("modules")}
        >
          Module health <span>{data?.modules.modules.length || "—"}</span>
        </button>
        <button
          className={tab === "sources" ? "active" : ""}
          onClick={() => setTab("sources")}
        >
          Data & refresh
        </button>
      </nav>
      {error && (
        <div role="alert" className="notice error">
          {error}
          <button aria-label="Dismiss message" onClick={() => setError("")}>
            ×
          </button>
        </div>
      )}
      {sourceOld && (
        <div className="notice">
          Task evidence is older than expected. Elapsed clocks keep moving; the
          underlying records only change after a successful refresh.{" "}
          <button onClick={() => setTab("sources")}>View source dates</button>
        </div>
      )}
      {envelope && (
        <form className="unlock card" onSubmit={doUnlock}>
          <span className="eyebrow">ENCRYPTED WORKSPACE</span>
          <h2>Unlock your operations data.</h2>
          <p>
            Enter your access key. It stays in this tab and is never sent to
            GitHub.
          </p>
          <label>
            Access key
            <input
              autoComplete="off"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          <button className="primary" disabled={busy}>
            {busy ? "Unlocking…" : "Unlock workspace"}
          </button>
        </form>
      )}
      {!data && !envelope && (
        <section className="welcome">
          <span className="eyebrow">YOUR OPERATIONS WORKSPACE</span>
          <h2>One task list. Every signal.</h2>
          <p>
            Track readiness, find stalled work, and trace module failures
            without switching between three dashboards.
          </p>
          <div className="features">
            <div>
              <b>01 / Completion</b>
              <p>Five cohorts, shared filters, and writer confirmations.</p>
            </div>
            <div>
              <b>02 / Staleness</b>
              <p>Stage-aware clocks and individual task deadlines.</p>
            </div>
            <div>
              <b>03 / Module health</b>
              <p>Hourly execution failures with dimension drilldowns.</p>
            </div>
          </div>
          <label className="primary">
            Import private snapshot
            <input
              aria-label="Import private snapshot"
              type="file"
              accept=".json"
              onChange={(e) => void importFile(e.target.files?.[0])}
            />
          </label>
          <button
            className="button"
            style={{ marginLeft: 12 }}
            onClick={() => {
              mode.current = "feed";
              void refresh(true);
            }}
          >
            Load published workspace
          </button>
          <p className="small">
            Data is kept in memory. The public site contains no readable task
            records.
          </p>
        </section>
      )}
      {data && tab === "tasks" && (
        <>
          <section className="cohorts" aria-label="Task cohort">
            {Object.entries(COHORTS).map(([k, v]) => (
              <button
                key={k}
                className={cohort === k ? "selected" : ""}
                onClick={() => pick(setCohort, k)}
              >
                {v}
                <span>
                  {k === "all"
                    ? data.tasks.length
                    : data.tasks.filter((t) => t.cohort === k).length}
                </span>
              </button>
            ))}
          </section>
          <section className="metrics">
            <div>
              <span>Ready for delivery</span>
              <strong>
                {summary.rfd}
                <small> / {base.length}</small>
              </strong>
              <p>Studio-qualified RFD</p>
            </div>
            <div>
              <span>Delivered</span>
              <strong>
                {summary.delivered}
                <small> / {base.length}</small>
              </strong>
              <p>Separate from readiness</p>
            </div>
            <button
              onClick={() => {
                setFilter("stale");
                setView("staleness");
              }}
            >
              <span>Past activity threshold</span>
              <strong className={summary.stale ? "amber-text" : ""}>
                {summary.stale}
                <small> tasks</small>
              </strong>
              <p>Review activity signals →</p>
            </button>
            <button onClick={() => setFilter("overdue")}>
              <span>Overdue</span>
              <strong className={summary.overdue ? "red-text" : ""}>
                {summary.overdue}
                <small> tasks</small>
              </strong>
              <p>Individual RFD deadlines →</p>
            </button>
          </section>
          <div className="section-heading">
            <div>
              <h2>Task workspace</h2>
              <p>One roster. Switch the signals you need.</p>
            </div>
            <div className="segmented">
              <button
                className={view === "completion" ? "selected" : ""}
                onClick={() => setView("completion")}
              >
                Completion
              </button>
              <button
                className={view === "staleness" ? "selected" : ""}
                onClick={() => setView("staleness")}
              >
                Staleness
              </button>
            </div>
          </div>
          {view === "completion" && (
            <div className="domain-grid">
              {Object.entries(DOMAINS).map(([k, label]) => {
                const ts = data.tasks.filter(
                    (t) =>
                      (cohort === "all" || t.cohort === cohort) &&
                      t.domain === k,
                  ),
                  ready = ts.filter(
                    (t) => t.rfd || t.stage === "Delivered",
                  ).length;
                return (
                  <button
                    key={k}
                    className={domain === k ? "chosen" : ""}
                    onClick={() => pick(setDomain, domain === k ? "all" : k)}
                  >
                    <span>{label}</span>
                    <b>
                      {ready}
                      <small> / {ts.length}</small>
                    </b>
                    <i>
                      <em
                        style={{
                          width: `${ts.length ? (ready / ts.length) * 100 : 0}%`,
                        }}
                      />
                    </i>
                    <small>RFD or delivered</small>
                  </button>
                );
              })}
            </div>
          )}
          <details className="pipeline">
            <summary>
              Pipeline distribution <span>{base.length} tasks in scope</span>
            </summary>
            <div className="chips">
              {Object.entries(Object.groupBy(base, (t) => t.stage)).map(
                ([name, tasks]) => (
                  <button
                    className="button"
                    key={name}
                    onClick={() => pick(setStage, name)}
                  >
                    {name.split(" (")[0]} <b>{tasks?.length}</b>
                  </button>
                ),
              )}
            </div>
          </details>
          <section className="workspace card">
            <div className="filters">
              <label className="search">
                <span>Search tasks</span>
                <input
                  value={search}
                  onChange={(e) => pick(setSearch, e.target.value)}
                  placeholder="Task, owner, or stage…"
                />
              </label>
              <label>
                Domain
                <select
                  value={domain}
                  onChange={(e) => pick(setDomain, e.target.value)}
                >
                  <option value="all">All domains</option>
                  {Object.entries(DOMAINS).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Stage
                <select
                  value={stage}
                  onChange={(e) => pick(setStage, e.target.value)}
                >
                  <option value="all">All stages</option>
                  {[...new Set(data.tasks.map((t) => t.stage))]
                    .sort()
                    .map((s) => (
                      <option key={s}>{s}</option>
                    ))}
                </select>
              </label>
              <label>
                Holder
                <select
                  value={owner}
                  onChange={(e) => pick(setOwner, e.target.value)}
                >
                  <option value="all">All holders</option>
                  {[...new Set(data.tasks.map((t) => t.owner))]
                    .sort()
                    .map((s) => (
                      <option key={s}>{s}</option>
                    ))}
                </select>
              </label>
            </div>
            <div className="filter-secondary">
              <label>
                Show
                <select
                  value={filter}
                  onChange={(e) => pick(setFilter, e.target.value)}
                >
                  {Object.entries({
                    all: "All tasks",
                    active: "In progress",
                    attention: "Needs attention",
                    stale: "Past threshold",
                    overdue: "Overdue",
                    ready: "Ready inventory",
                    unclaimed: "No assigned Studio owner",
                    missing: "Missing deadline",
                  }).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Confirmation
                <select
                  value={confirm}
                  onChange={(e) => pick(setConfirm, e.target.value)}
                >
                  <option value="all">All confirmations</option>
                  {Object.entries(CONF).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Sort by
                <select
                  value={sort}
                  onChange={(e) => pick(setSort, e.target.value)}
                >
                  {Object.entries({
                    priority: "Triage priority",
                    name: "Task name",
                    stage: "Stage",
                    owner: "Holder",
                    updated: "Oldest update",
                    moved: "Longest in stage",
                    due: "Earliest deadline",
                    confirmation: "Confirmation",
                  }).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
                </select>
              </label>
              <button
                className="button"
                onClick={() => setAscending((v) => !v)}
                aria-label="Reverse sort order"
              >
                {ascending ? "↑ Default" : "↓ Reverse"}
              </button>
              <button
                className="quiet"
                onClick={() => {
                  setSearch("");
                  setDomain("all");
                  setStage("all");
                  setOwner("all");
                  setFilter("all");
                  setConfirm("all");
                  setPage(1);
                }}
              >
                Reset filters
              </button>
              <span className="spacer" />
              <button className="button" onClick={exportTasks}>
                Export CSV
              </button>
              <button className="button" onClick={exportDigest}>
                Follow-up drafts
              </button>
            </div>
            <div className="table-note">
              {filtered.length} matching tasks{" "}
              <span>
                All times Pacific ·{" "}
                {view === "staleness"
                  ? "Update timestamps are activity proxies, not proof of meaningful work."
                  : `Slack evidence checked ${dateLabel(data.sources.confirmations?.at)}. Coverage is shown in Data & refresh.`}
              </span>
            </div>
            <div className="table-scroll">
              <table className="task-table">
                <thead>
                  <tr>
                    <th>Task / domain</th>
                    <th>Stage / holder</th>
                    {view === "completion" ? (
                      <>
                        <th>Deadline</th>
                        <th>Confirmation</th>
                        <th>Last stage move</th>
                      </>
                    ) : (
                      <>
                        <th>Since update / in stage</th>
                        <th>Deadline / Slack activity</th>
                        <th>Last actor</th>
                      </>
                    )}
                    <th>Signal</th>
                    <th>Open</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((t) => (
                    <tr key={t.id}>
                      <td>
                        <button
                          className="task-link"
                          onClick={() => openTask(t)}
                        >
                          {t.name}
                        </button>
                        <small>
                          {DOMAINS[t.domain] || t.domain} · {COHORTS[t.cohort]}
                          {notes[t.id] ? " · local note" : ""}
                        </small>
                      </td>
                      <td>
                        <span className="stage-text">{t.stage}</span>
                        <small>{t.owner}</small>
                      </td>
                      {view === "completion" ? (
                        <>
                          <td>
                            <span className={overdue(t, now) ? "red-text" : ""}>
                              {t.deadline
                                ? dateLabel(t.deadline.at)
                                : "No deadline"}
                            </span>
                            <small>
                              {t.deadline?.source || "Not captured"}
                            </small>
                          </td>
                          <td>
                            <Badge
                              value={
                                CONF[t.confirmation?.state || "not_checked"] ||
                                "Not checked"
                              }
                            />
                            <small>
                              {t.confirmation?.reply_by ||
                                t.confirmation?.writer ||
                                "—"}
                            </small>
                          </td>
                          <td>
                            {ageLabel(elapsed(t.transitioned_at, now))}
                            <small>{dateLabel(t.transitioned_at)}</small>
                          </td>
                        </>
                      ) : (
                        <>
                          <td>
                            <b>{ageLabel(elapsed(t.updated_at, now))}</b>
                            <small>
                              {ageLabel(elapsed(t.transitioned_at, now))} in
                              stage · {threshold(t)}h threshold
                            </small>
                            <small>
                              Flagged{" "}
                              {ageLabel(
                                elapsed(
                                  flaggedSince(t, data.history, now),
                                  now,
                                ),
                              )}
                            </small>
                          </td>
                          <td>
                            <span className={overdue(t, now) ? "red-text" : ""}>
                              {t.deadline
                                ? dateLabel(t.deadline.at)
                                : "No deadline"}
                            </span>
                            <small>
                              Slack{" "}
                              {ageLabel(elapsed(t.slack_activity?.at, now))} ago
                            </small>
                          </td>
                          <td>
                            {t.last_actor}
                            <small>
                              {notes[t.id]?.at || t.warning?.warned_at
                                ? Date.parse(t.updated_at || "") >
                                  Date.parse(
                                    notes[t.id]?.at ||
                                      t.warning?.warned_at ||
                                      "",
                                  )
                                  ? "Updated since note"
                                  : "No update since note"
                                : ""}
                            </small>
                          </td>
                        </>
                      )}
                      <td>
                        <Badge value={signal(t, now)} />
                      </td>
                      <td className="links">
                        <Link href={t.studio}>Studio</Link>
                        <Link href={t.slack}>Slack</Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!filtered.length && (
                <div className="empty">
                  <h3>No tasks match these filters.</h3>
                  <p>Clear a filter to broaden the view.</p>
                </div>
              )}
            </div>
            <div className="pagination">
              <span>
                {filtered.length
                  ? `${(currentPage - 1) * 40 + 1}–${Math.min(currentPage * 40, filtered.length)} of ${filtered.length}`
                  : "0 tasks"}
              </span>
              <div>
                <button
                  className="button"
                  disabled={currentPage === 1}
                  onClick={() => setPage(currentPage - 1)}
                >
                  ← Previous
                </button>
                <span>
                  {currentPage} / {pages}
                </span>
                <button
                  className="button"
                  disabled={currentPage === pages}
                  onClick={() => setPage(currentPage + 1)}
                >
                  Next →
                </button>
              </div>
            </div>
          </section>
          {view === "staleness" && (
            <>
              <section className="triage-grid">
                <div className="card">
                  <h3>Holder workload</h3>
                  <p className="small">
                    In-progress tasks in the current scope
                  </p>
                  {Object.entries(
                    Object.groupBy(
                      base.filter((t) => !isReady(t) && !isParked(t)),
                      (t) => t.owner,
                    ),
                  )
                    .sort(
                      (a, b) =>
                        (b[1]?.filter((t) => isStale(t, now)).length || 0) -
                        (a[1]?.filter((t) => isStale(t, now)).length || 0),
                    )
                    .slice(0, 10)
                    .map(([person, tasks]) => (
                      <button
                        className="rollup"
                        key={person}
                        onClick={() => pick(setOwner, person)}
                      >
                        <span>{person}</span>
                        <b>
                          {tasks?.length} held{" "}
                          <small>
                            · {tasks?.filter((t) => isStale(t, now)).length}{" "}
                            flagged
                          </small>
                        </b>
                      </button>
                    ))}
                </div>
                <div className="card">
                  <h3>Queues & coverage</h3>
                  {Object.entries(
                    Object.groupBy(
                      base.filter((t) =>
                        ["Queue aging", "In queue"].includes(signal(t, now)),
                      ),
                      (t) => t.stage,
                    ),
                  ).map(([name, tasks]) => (
                    <button
                      className="rollup"
                      key={name}
                      onClick={() => pick(setStage, name)}
                    >
                      <span>{name}</span>
                      <b>{tasks?.length}</b>
                    </button>
                  ))}
                  <div className="rollup">
                    <span>In progress without a deadline</span>
                    <b>
                      {
                        base.filter(
                          (t) => !isReady(t) && !isParked(t) && !t.deadline,
                        ).length
                      }
                    </b>
                  </div>
                  <p className="small">
                    Slack conversation never clears a Studio activity flag.
                    Reassignment signals are prompts for review.
                  </p>
                </div>
                <div className="card">
                  <h3>Recent source history</h3>
                  <div
                    className="trend"
                    role="img"
                    aria-label="Historical flagged task counts, all cohorts"
                  >
                    {trend.map((h, i) => (
                      <i
                        key={i}
                        title={`${dateLabel(h.at)}: ${h.count} flagged`}
                        style={{
                          height: `${Math.max(4, (h.count / Math.max(...trend.map((x) => x.count), 1)) * 90)}px`,
                        }}
                      />
                    ))}
                  </div>
                  <p className="small">
                    All cohorts · last {trend.length} legacy scans. Historical
                    rules may differ from corrected deadline rules.
                  </p>
                  <h4>{cleared.length} flags now clear</h4>
                  <p className="small">
                    Compared with {dateLabel(previous?.at)}. A cleared heuristic
                    flag does not certify completed work.
                  </p>
                  {cleared.slice(0, 5).map((t) => (
                    <button
                      className="text-button"
                      key={t.id}
                      onClick={() => openTask(t)}
                    >
                      {t.name} → {signal(t, now)}
                    </button>
                  ))}
                </div>
              </section>
              <div className="card recent">
                <h3>Recent Studio actors</h3>
                <p className="small">
                  Recorded updates within 90 minutes; this does not establish
                  who is on shift.
                </p>
                <div className="chips">
                  {Object.entries(
                    Object.groupBy(
                      base.filter(
                        (t) => (elapsed(t.updated_at, now) ?? Infinity) <= 1.5,
                      ),
                      (t) => t.last_actor,
                    ),
                  ).map(([actor, tasks]) => (
                    <span className="tag" key={actor}>
                      {actor} · {tasks?.length} updates
                    </span>
                  ))}
                </div>
              </div>
            </>
          )}
        </>
      )}
      {data && tab === "modules" && (
        <>
          <div className="section-heading">
            <div>
              <h2>Module health</h2>
              <p>
                Execution reliability, hour by hour. Expand a module for
                dimension verdicts.
              </p>
            </div>
            <div className="segmented">
              <button
                className={percent ? "selected" : ""}
                onClick={() => {
                  setPercent(true);
                  localStorage.setItem("dg-operations-percent", "true");
                }}
              >
                Percent
              </button>
              <button
                className={!percent ? "selected" : ""}
                onClick={() => {
                  setPercent(false);
                  localStorage.setItem("dg-operations-percent", "false");
                }}
              >
                Fraction
              </button>
            </div>
          </div>
          <section className="metrics">
            <div>
              <span>Execution failure rate</span>
              <strong>{metric(totalF, totalD, true)}</strong>
              <p>
                {totalF} failed / {totalD.toLocaleString()} finished
              </p>
            </div>
            <div>
              <span>Modules exercised</span>
              <strong>
                {modules.filter((m) => sum(m.d) > 0).length}
                <small> / {modules.length}</small>
              </strong>
              <p>Current filters and time window</p>
            </div>
            <div>
              <span>Highest execution failure rate</span>
              <strong>
                {worst ? metric(sum(worst.f), sum(worst.d), true) : "—"}
              </strong>
              <p title={worst?.name}>
                {worst?.name || "No module with 10+ executions"}
              </p>
            </div>
            <div>
              <span>Observed window</span>
              <strong>
                {range.length}
                <small> hours</small>
              </strong>
              <p>Data checked {dateLabel(data.sources.modules.at)}</p>
            </div>
          </section>
          <section className="card module-card">
            <div className="filters">
              <label className="search">
                Find a module
                <input
                  placeholder="Search module names…"
                  value={moduleSearch}
                  onChange={(e) => setModuleSearch(e.target.value)}
                />
              </label>
              <label>
                From
                <select
                  value={start}
                  onChange={(e) => {
                    const v = +e.target.value;
                    setFrom(v);
                    if (v > end) setTo(v);
                  }}
                >
                  {hours.map((h, i) => (
                    <option value={i} key={i}>
                      {h}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Through
                <select value={end} onChange={(e) => setTo(+e.target.value)}>
                  {hours.map((h, i) => (
                    <option value={i} key={i} disabled={i < start}>
                      {h}
                    </option>
                  ))}
                </select>
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={failuresOnly}
                  onChange={(e) => setFailuresOnly(e.target.checked)}
                />
                Execution failures only
              </label>
            </div>
            <div className="filter-secondary">
              <button
                className="button"
                onClick={() => setExpanded(new Set(modules.map((m) => m.name)))}
              >
                Expand all
              </button>
              <button className="button" onClick={() => setExpanded(new Set())}>
                Collapse all
              </button>
              <span className="spacer" />
              <span className="small">
                — no executions · darker cells = higher failure rate
              </span>
              <button
                className="button"
                onClick={() =>
                  download(
                    "dg-module-health.csv",
                    csv([
                      [
                        "Module",
                        "Metric",
                        "Hour PT",
                        "Failures",
                        "Denominator",
                        "Neutral",
                      ],
                      ...modules.flatMap((m) =>
                        range
                          .map((h, i) => [
                            m.name,
                            "Execution",
                            h,
                            m.f[start + i],
                            m.d[start + i],
                            "",
                          ])
                          .concat(
                            m.dims.flatMap((d) =>
                              range.map((h, i) => [
                                m.name,
                                d.label,
                                h,
                                d.f[start + i],
                                d.g[start + i],
                                d.n[start + i],
                              ]),
                            ),
                          ),
                      ),
                    ]),
                    "text/csv",
                  )
                }
              >
                Export CSV
              </button>
            </div>
            <div className="table-note">
              Execution failure = failed / finished{" "}
              <span>
                Dimension failure = fail / (fail + pass); neutral excluded.
                Current hour is partial.
              </span>
            </div>
            <div className="heat-scroll">
              <table className="heatmap">
                <thead>
                  <tr>
                    <th>Module / dimension</th>
                    <th>Window</th>
                    {range.map((h, i) => (
                      <th key={i}>
                        <small>{h.split(" ").slice(0, 2).join(" ")}</small>
                        {h.split(" ").slice(2).join(" ")}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {modules.map((m) => (
                    <Fragment key={m.name}>
                      <tr className="module-row">
                        <th>
                          <button
                            aria-expanded={expanded.has(m.name)}
                            onClick={() =>
                              setExpanded((prev) => {
                                const n = new Set(prev);
                                if (n.has(m.name)) n.delete(m.name);
                                else n.add(m.name);
                                return n;
                              })
                            }
                          >
                            <span>{expanded.has(m.name) ? "−" : "+"}</span>
                            {m.name}
                            <small>{m.dims.length} dimensions</small>
                          </button>
                        </th>
                        <td className="total">
                          {metric(sum(m.f), sum(m.d), percent)}
                        </td>
                        {range.map((_, j) => {
                          const i = start + j;
                          return (
                            <td
                              key={i}
                              title={`${m.f[i]} failed / ${m.d[i]} finished executions`}
                            >
                              <span
                                className="heat"
                                style={{
                                  background: m.d[i]
                                    ? `rgba(192, 73, 39, ${0.06 + (0.58 * m.f[i]) / m.d[i]})`
                                    : "transparent",
                                }}
                              >
                                {metric(m.f[i], m.d[i], percent)}
                              </span>
                            </td>
                          );
                        })}
                      </tr>
                      {expanded.has(m.name) &&
                        m.dims.map((d) => (
                          <tr className="dimension-row" key={d.label}>
                            <th>
                              {d.label}
                              <small>
                                QC verdicts · {sum(d.n)} neutral in window
                              </small>
                            </th>
                            <td className="total">
                              {metric(sum(d.f), sum(d.g), percent)}
                            </td>
                            {range.map((_, j) => {
                              const i = start + j;
                              return (
                                <td
                                  key={i}
                                  title={`${d.f[i]} fail / ${d.g[i]} fail + pass; ${d.n[i]} neutral excluded`}
                                >
                                  <span
                                    className="heat"
                                    style={{
                                      background: d.g[i]
                                        ? `rgba(171, 126, 45, ${0.04 + (0.45 * d.f[i]) / d.g[i]})`
                                        : "transparent",
                                    }}
                                  >
                                    {metric(d.f[i], d.g[i], percent)}
                                  </span>
                                </td>
                              );
                            })}
                          </tr>
                        ))}
                    </Fragment>
                  ))}
                </tbody>
              </table>
              {!modules.length && (
                <div className="empty">No modules match this view.</div>
              )}
            </div>
          </section>
          <p className="small">
            Source: Datadog events joined to Studio production audits.{" "}
            {moduleData?.excluded_other_world} source exclusions. Task filters
            do not apply to these world-wide module aggregates.
          </p>
        </>
      )}
      {data && tab === "sources" && (
        <>
          <div className="section-heading">
            <div>
              <h2>Data & refresh</h2>
              <p>One combined snapshot. Independent evidence dates.</p>
            </div>
            <label className="button file-label">
              Import snapshot
              <input
                type="file"
                accept=".json"
                onChange={(e) => void importFile(e.target.files?.[0])}
              />
            </label>
          </div>
          <section className="sources-grid">
            {Object.entries(data.sources).map(([k, s]) => {
              const old =
                !!s.cadence_minutes &&
                (elapsed(s.at, now) ?? Infinity) > (s.cadence_minutes * 3) / 60;
              return (
                <article className="card" key={k}>
                  <span className="eyebrow">{k}</span>
                  <h3>{s.label}</h3>
                  <strong>{dateLabel(s.at, true)}</strong>
                  <p className="small">
                    {s.cadence_minutes
                      ? `${s.cadence_minutes}-minute source cadence`
                      : "Separate evidence; no automatic freshness guarantee"}
                  </p>
                  <Badge
                    value={
                      old
                        ? "Refresh overdue"
                        : s.cadence_minutes
                          ? "Timestamp available"
                          : "Dated evidence"
                    }
                  />
                </article>
              );
            })}
          </section>
          <section className="card data-details">
            <h3>
              {feed ? "Connected to a published snapshot" : "Imported snapshot"}
            </h3>
            <p>
              {feed
                ? "This page checks for a newer snapshot every five minutes while visible. The collector must publish fresh source data for records to change."
                : "Import an updated snapshot to refresh this session. Reloading the page does not fetch fresh Studio records."}
            </p>
            <p className="small">
              Snapshot assembled {dateLabel(data.generated_at, true)} ·{" "}
              {data.mode}. The module window ends{" "}
              {data.modules.window_end_local} Pacific. Slack confirmation
              classifications include automated interpretations; open the source
              reply before relying on a commitment.
            </p>
            <h3>How the signals work</h3>
            <ul>
              <li>
                Studio’s task-level RFD date wins. Bare dates end at 11:59 PM
                Pacific, including daylight saving changes.
              </li>
              <li>
                Activity thresholds: writing 12 hours; runs 6 hours;
                review/audit/edits 4 hours; queues 2 hours.
              </li>
              <li>
                “No update since claim” means update and transition timestamps
                are within 72 seconds, past half the stage threshold. It is a
                heuristic.
              </li>
              <li>
                Red zone combines a stale signal with an overdue deadline or
                less than a quarter of the stage-to-deadline window remaining.
              </li>
              <li>
                RFD, RFD Audit Passed, Delivered, and Frozen remain distinct.
                Discarded and test records are excluded.
              </li>
              <li>
                Studio updates, stage transitions, Slack mentions, and
                confirmations use independent clocks.
              </li>
              <li>
                Local notes stay in this browser and never write to Studio or
                send a message.
              </li>
            </ul>
            <div className="button-row">
              <button
                className="button"
                onClick={() => {
                  mode.current = "feed";
                  lastFetch.current = "";
                  void refresh(true);
                }}
              >
                Reconnect to published data
              </button>
              <button
                className="button"
                onClick={() =>
                  download(
                    "dg-operations-private-snapshot.json",
                    JSON.stringify(data),
                    "application/json",
                  )
                }
              >
                Export private snapshot
              </button>
              <button
                className="button"
                onClick={() =>
                  download(
                    "dg-local-follow-up-notes.json",
                    JSON.stringify(notes, null, 2),
                    "application/json",
                  )
                }
              >
                Export local notes
              </button>
              <button
                className="quiet"
                onClick={() => {
                  localStorage.removeItem("dg-operations-local-notes");
                  setNotes({});
                }}
              >
                Clear local notes
              </button>
            </div>
          </section>
        </>
      )}
      <footer>
        Doppelganger Operations{" "}
        <span>Readiness ≠ delivery ≠ client acceptance.</span>
      </footer>
      <dialog
        ref={detail}
        className="detail"
        onCancel={() => setSelected(null)}
        onClick={(e) => {
          if (e.target === e.currentTarget) setSelected(null);
        }}
      >
        {task && (
          <>
            <div className="detail-heading">
              <span className="eyebrow">
                {COHORTS[task.cohort]} / {DOMAINS[task.domain]}
              </span>
              <button
                className="quiet"
                onClick={() => setSelected(null)}
                aria-label="Close task details"
              >
                ×
              </button>
            </div>
            <h2>{task.name}</h2>
            <div className="button-row">
              <Badge value={signal(task, now)} />
              <Link href={task.studio}>Studio</Link>
              <Link href={task.slack}>Slack thread</Link>
            </div>
            <dl className="detail-grid">
              <div>
                <dt>Stage</dt>
                <dd>{task.stage}</dd>
              </div>
              <div>
                <dt>Current holder</dt>
                <dd>{task.owner}</dd>
              </div>
              <div>
                <dt>Last generic Studio update</dt>
                <dd>
                  {dateLabel(task.updated_at, true)}
                  <small>by {task.last_actor}</small>
                </dd>
              </div>
              <div>
                <dt>Stage transition</dt>
                <dd>{dateLabel(task.transitioned_at, true)}</dd>
              </div>
              <div>
                <dt>RFD deadline</dt>
                <dd>
                  {dateLabel(task.deadline?.at, true)}
                  <small>
                    {task.deadline?.source || "No deadline captured"}
                  </small>
                </dd>
              </div>
              <div>
                <dt>Threshold</dt>
                <dd>
                  {threshold(task)} hours
                  <small>Generic update / queue clock</small>
                </dd>
              </div>
            </dl>
            {task.activity_conflict && (
              <p className="notice">
                The task stage changed after the activity snapshot. Staleness
                classification is withheld until activity refreshes.
              </p>
            )}
            <h3>Deadline evidence</h3>
            {["assigned", "committed"].map((k) => {
              const d = task.deadline_history?.[k as "assigned" | "committed"];
              return d ? (
                <div className="evidence" key={k}>
                  <b>
                    {k === "assigned"
                      ? "Assigned deadline"
                      : "Holder commitment"}
                  </b>
                  <p>
                    {dateLabel(d.due, true)} · {d.by}
                  </p>
                  {d.quote && <blockquote>{d.quote}</blockquote>}
                  <Link href={d.url}>Source</Link>
                </div>
              ) : null;
            })}
            <p className="small">
              {task.deadline_history?.moves?.length || 0} captured deadline
              changes. Legacy history may be incomplete.
            </p>
            <h3>Slack confirmation</h3>
            <Badge
              value={
                CONF[task.confirmation?.state || "not_checked"] || "Not checked"
              }
            />
            {task.confirmation && (
              <div className="evidence">
                {task.confirmation.thread_gap && (
                  <p className="notice">{task.confirmation.thread_gap}</p>
                )}
                {task.confirmation.observed_at && (
                  <p className="small">
                    Full thread read {dateLabel(task.confirmation.observed_at, true)}
                    {task.confirmation.checked_at &&
                      ` · Changes checked ${dateLabel(task.confirmation.checked_at, true)}`}
                  </p>
                )}
                <p>
                  Asked {dateLabel(task.confirmation.asked, true)} by{" "}
                  {task.confirmation.asked_by || "unknown"}.<br />
                  Reply {dateLabel(task.confirmation.reply, true)} by{" "}
                  {task.confirmation.reply_by || "—"}.
                </p>
                {task.confirmation.deadline && (
                  <p>Requested deadline: {task.confirmation.deadline}</p>
                )}
                {task.confirmation.reply_text && (
                  <blockquote>{task.confirmation.reply_text}</blockquote>
                )}
                <div className="button-row">
                  <Link href={task.confirmation.asked_url}>Request</Link>
                  <Link href={task.confirmation.reply_url}>Reply</Link>
                </div>
              </div>
            )}
            <h3>Last captured Slack activity</h3>
            <p>
              {dateLabel(task.slack_activity?.at, true)} ·{" "}
              {task.slack_activity?.by || "Not captured"}
            </p>
            {task.slack_activity?.text && (
              <blockquote>{task.slack_activity.text}</blockquote>
            )}
            <Link href={task.slack_activity?.url}>Activity source</Link>
            <h3>Local follow-up note</h3>
            <label className="note-label">
              Visible only in this browser
              <textarea
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="Record a follow-up or warning already sent…"
              />
            </label>
            <div className="button-row">
              <button
                className="primary"
                onClick={() => {
                  const next = {
                    ...notes,
                    [task.id]: { note, at: new Date().toISOString() },
                  };
                  setNotes(next);
                  localStorage.setItem(
                    "dg-operations-local-notes",
                    JSON.stringify(next),
                  );
                }}
              >
                Save local note
              </button>
              <span className="small">
                {notes[task.id]
                  ? `Saved ${dateLabel(notes[task.id].at)}`
                  : "Does not send a message or change Studio."}
              </span>
            </div>
          </>
        )}
      </dialog>
    </main>
  );
}
