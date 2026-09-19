import { test } from "node:test";
import assert from "node:assert/strict";
import {
  signal,
  overdue,
  priority,
  validateSnapshot,
  moduleHours,
  csv,
  type Task,
  type Snapshot,
} from "../lib/model.ts";
const now = Date.parse("2026-09-19T00:00:00Z");
const ago = (h: number) => new Date(now - h * 3600000).toISOString();
function task(p: Partial<Task> = {}): Task {
  return {
    id: "sample-1",
    name: "Sample task",
    cohort: "docx",
    artifact: "docx",
    domain: "BUS",
    stage: "Stage 1 - Writing",
    status_id: "writing",
    rfd: false,
    owner: "Example owner",
    owner_id: "example-user",
    last_actor: "Example owner",
    updated_at: ago(1),
    transitioned_at: ago(24),
    activity_conflict: false,
    blocked: false,
    deadline: null,
    deadline_history: {},
    slack_activity: null,
    confirmation: null,
    warning: null,
    studio: "https://example.com/task",
    slack: null,
    ...p,
  };
}
function snapshot(): Snapshot {
  return {
    schema_version: 1,
    generated_at: ago(0),
    mode: "Synthetic test",
    tasks: [task()],
    modules: {
      generated_at_utc: ago(0),
      tz: "America/Los_Angeles",
      window_start_local: "2026-09-18 16",
      window_end_local: "2026-09-18 17:00",
      n_hours: 2,
      hours: ["2026-09-18T23:00:00Z", "2026-09-19T00:00:00Z"],
      excluded_other_world: 0,
      modules: [
        {
          name: "Example module",
          f: [1, 0],
          d: [2, 100],
          t: [1, 102],
          dims: [
            {
              label: "Example dimension",
              f: [1, 0],
              g: [1, 0],
              n: [0, 2],
              t: [1, 1, 2],
            },
          ],
        },
      ],
    },
    sources: Object.fromEntries(
      ["tasks", "activity", "confirmations", "slack", "modules"].map((k) => [
        k,
        { at: ago(0), cadence_minutes: k === "modules" ? 60 : 5, label: k },
      ]),
    ),
    history: [],
    conflicts: [],
  };
}
test("writing/run/review activity boundaries use their own thresholds", () => {
  for (const [stage, h] of [
    ["Stage 1 - Writing", 12],
    ["Stage 2 - Runs, Trajectories & Grading", 6],
    ["In Audit", 4],
  ] as const) {
    assert.equal(
      signal(task({ stage, updated_at: ago(h - 0.001) }), now),
      "Active",
    );
    assert.equal(
      signal(task({ stage, updated_at: ago(h) }), now),
      "Stale · follow up",
    );
    assert.equal(
      signal(
        task({ stage, updated_at: ago(h * 2), transitioned_at: ago(100) }),
        now,
      ),
      "Stale · review reassignment",
    );
  }
});
test("no update since claim has precedence and starts at half threshold", () => {
  assert.equal(
    signal(task({ updated_at: ago(6), transitioned_at: ago(6) }), now),
    "No update since claim",
  );
  assert.equal(
    signal(task({ updated_at: ago(5.999), transitioned_at: ago(5.999) }), now),
    "Active",
  );
});
test("queue and blocked rules; Slack does not reset Studio", () => {
  assert.equal(
    signal(task({ stage: "Ready for Review", transitioned_at: ago(2) }), now),
    "Queue aging",
  );
  assert.equal(
    signal(
      task({ stage: "Ready for Review", transitioned_at: ago(1.99) }),
      now,
    ),
    "In queue",
  );
  assert.equal(
    signal(task({ stage: "Ready for Review", blocked: true }), now),
    "Blocked",
  );
  assert.equal(
    signal(task({ updated_at: ago(13), slack_activity: { at: ago(0) } }), now),
    "Stale · follow up",
  );
});
test("red zone requires both staleness and short deadline runway", () => {
  const deadline = { at: ago(1), source: "Studio", raw: "2026-09-18" };
  assert.equal(
    signal(task({ updated_at: ago(13), deadline }), now),
    "Red zone",
  );
  assert.equal(signal(task({ updated_at: ago(0.1), deadline }), now), "Active");
  assert.equal(overdue(task({ deadline }), now), true);
});
test("ready/parked exemptions preserve distinct statuses", () => {
  for (const stage of ["Delivered", "Frozen"])
    assert.equal(signal(task({ stage, blocked: true }), now), stage);
  assert.equal(signal(task({ rfd: true, blocked: true }), now), "Ready");
  const audited = task({ stage: "RFD Audit Passed" });
  assert.equal(signal(audited, now), "Ready");
  assert.equal(audited.rfd, false);
});
test("unknown, future and mismatched activity never creates Active", () => {
  assert.equal(
    signal(task({ activity_conflict: true }), now),
    "Activity needs refresh",
  );
  assert.equal(signal(task({ updated_at: null }), now), "Activity unknown");
  assert.equal(signal(task({ updated_at: ago(-5) }), now), "Activity unknown");
});
test("priority is zero for ready and positive for stale", () => {
  assert.equal(priority(task({ rfd: true }), now), 0);
  assert.ok(priority(task({ updated_at: ago(13) }), now) > 0);
});
test("valid aggregates retain weighted denominators and neutral counts", () => {
  const s = validateSnapshot(snapshot());
  assert.deepEqual(s.modules.modules[0].t, [1, 102]);
  assert.deepEqual(s.modules.modules[0].dims[0].t, [1, 1, 2]);
});
// Deliberately break types to verify the runtime trust boundary.
/* eslint-disable @typescript-eslint/no-explicit-any */
test("malformed imports are rejected before rendering", () => {
  const mutations = [
    (s: any) => (s.sources = {}),
    (s: any) => (s.tasks[0].rfd = "false"),
    (s: any) => (s.history = [null]),
    (s: any) => (s.modules.modules[0].name = {}),
    (s: any) =>
      (s.tasks[0].confirmation = { state: "confirmed", reply_text: {} }),
    (s: any) => s.tasks.push(s.tasks[0]),
    (s: any) => (s.modules.modules[0].t = [99, 102]),
    (s: any) => (s.modules.modules[0].dims[0].g = [0, 0]),
    (s: any) => (s.modules.hours[1] = s.modules.hours[0]),
  ];
  for (const mutate of mutations) {
    const s = snapshot();
    mutate(s);
    assert.throws(() => validateSnapshot(s));
  }
});
test("Pacific fall DST preserves two different 1 AM instants", () => {
  const s = snapshot().modules;
  s.n_hours = 3;
  s.hours = [
    "2026-11-01T08:00:00Z",
    "2026-11-01T09:00:00Z",
    "2026-11-01T10:00:00Z",
  ];
  const labels = moduleHours(s);
  assert.match(labels[0], /01:00 PDT/);
  assert.match(labels[1], /01:00 PST/);
  assert.match(labels[2], /02:00 PST/);
});
test("CSV escapes quotes and neutralizes spreadsheet formulas", () => {
  assert.equal(
    csv([["=1+1", 'a"b', "ordinary"]]),
    '"\'=1+1","a""b","ordinary"',
  );
});
