import { test } from "node:test";
import assert from "node:assert/strict";
import { type Module, type Task } from "../lib/model.ts";
import {
  compareModules,
  compareTasks,
  type TaskSortKey,
} from "../lib/sort.ts";

const now = Date.parse("2026-09-19T00:00:00Z");
const ago = (hours: number) => new Date(now - hours * 3600000).toISOString();
function task(name: string, overrides: Partial<Task> = {}): Task {
  return {
    id: name,
    name,
    cohort: "docx",
    artifact: "docx",
    domain: "BUS",
    stage: "Stage 1 - Writing",
    status_id: "writing",
    rfd: false,
    owner: "Example owner",
    owner_id: "example-user",
    last_actor: "Example actor",
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
    ...overrides,
  };
}
const moduleRow = (name: string, f: number[], d: number[]): Module => ({
  name,
  f,
  d,
  t: [f.reduce((a, b) => a + b, 0), d.reduce((a, b) => a + b, 0)],
  dims: [],
});
const names = (rows: { name: string }[]) => rows.map((row) => row.name);

test("task names sort naturally in either direction without changing input", () => {
  const rows = [task("Task 10"), task("task 2"), task("Task 1")];
  assert.deepEqual(
    names([...rows].sort((a, b) => compareTasks(a, b, "name", true, now))),
    ["Task 1", "task 2", "Task 10"],
  );
  assert.deepEqual(
    names([...rows].sort((a, b) => compareTasks(a, b, "name", false, now))),
    ["Task 10", "task 2", "Task 1"],
  );
  assert.deepEqual(names(rows), ["Task 10", "task 2", "Task 1"]);
});

test("all task timestamp sorts keep missing and invalid dates last", () => {
  const values = [null, ago(1), ago(2), "not-a-date"];
  for (const key of ["updated", "moved", "due", "slack"] as TaskSortKey[]) {
    const rows = values.map((value, i) => {
      const evidence = value ? { at: value, source: "Test", raw: value } : null;
      return task(`Task ${i}`, {
        updated_at: value,
        transitioned_at: value,
        deadline: evidence,
        slack_activity: evidence,
      });
    });
    assert.deepEqual(
      names([...rows].sort((a, b) => compareTasks(a, b, key, true, now))),
      ["Task 2", "Task 1", "Task 0", "Task 3"],
      `${key} ascending`,
    );
    assert.deepEqual(
      names([...rows].sort((a, b) => compareTasks(a, b, key, false, now))),
      ["Task 1", "Task 2", "Task 0", "Task 3"],
      `${key} descending`,
    );
  }
});

test("Unix epoch remains a valid date and ties keep natural name order", () => {
  const rows = [
    task("Task 10", { updated_at: ago(1) }),
    task("Task 2", { updated_at: ago(1) }),
    task("Epoch", { updated_at: "1970-01-01T00:00:00Z" }),
  ];
  assert.deepEqual(
    names([...rows].sort((a, b) => compareTasks(a, b, "updated", true, now))),
    ["Epoch", "Task 2", "Task 10"],
  );
  assert.deepEqual(
    names([...rows].sort((a, b) => compareTasks(a, b, "updated", false, now))),
    ["Task 2", "Task 10", "Epoch"],
  );
});

test("text keys use displayed labels and their own values", () => {
  const rows = [
    task("One", {
      domain: "SUP",
      confirmation: { state: "replied" },
      owner: "Zeta",
      last_actor: "Alpha",
    }),
    task("Two", {
      domain: "BUS",
      confirmation: { state: "awaiting" },
      owner: "Alpha",
      last_actor: "Zeta",
    }),
    task("Three", {
      domain: "LAW",
      confirmation: { state: "at_risk" },
      owner: "Beta",
      last_actor: "Beta",
    }),
  ];
  for (const [key, expected] of [
    ["domain", ["Two", "Three", "One"]],
    ["confirmation", ["Three", "Two", "One"]],
    ["owner", ["Two", "Three", "One"]],
    ["last_actor", ["One", "Three", "Two"]],
  ] as [TaskSortKey, string[]][]) {
    assert.deepEqual(
      names([...rows].sort((a, b) => compareTasks(a, b, key, true, now))),
      expected,
      key,
    );
  }
});

test("signal labels are alphabetical while priority retains highest first", () => {
  const rows = [
    task("Ready", { rfd: true }),
    task("Stale", { updated_at: ago(13) }),
    task("Active"),
  ];
  assert.deepEqual(
    names([...rows].sort((a, b) => compareTasks(a, b, "signal", true, now))),
    ["Active", "Ready", "Stale"],
  );
  assert.deepEqual(
    names([...rows].sort((a, b) => compareTasks(a, b, "priority", true, now))),
    ["Stale", "Active", "Ready"],
  );
  assert.deepEqual(
    names([...rows].sort((a, b) => compareTasks(a, b, "priority", false, now))),
    ["Active", "Ready", "Stale"],
  );
});

test("module rates use weighted sums rather than counts or mean percentages", () => {
  const rows = [
    moduleRow("High count", [9, 1], [90, 10]),
    moduleRow("High rate", [0, 2], [1, 3]),
    moduleRow("One sparse failure", [1, 0], [1, 100]),
  ];
  assert.deepEqual(
    names([...rows].sort((a, b) => compareModules(a, b, "window", false, 0, 1))),
    ["High rate", "High count", "One sparse failure"],
  );
});

test("module window and absolute hour sorts follow the chosen range", () => {
  const rows = [
    moduleRow("A", [9, 1, 0], [10, 10, 10]),
    moduleRow("B", [1, 9, 5], [10, 10, 10]),
  ];
  assert.deepEqual(
    names([...rows].sort((a, b) => compareModules(a, b, "window", false, 0, 0))),
    ["A", "B"],
  );
  assert.deepEqual(
    names([...rows].sort((a, b) => compareModules(a, b, "window", false, 1, 2))),
    ["B", "A"],
  );
  assert.deepEqual(
    names([...rows].sort((a, b) => compareModules(a, b, 0, false, 1, 2))),
    ["A", "B"],
  );
  assert.deepEqual(
    names([...rows].sort((a, b) => compareModules(a, b, 1, false, 0, 2))),
    ["B", "A"],
  );
});

test("module no-execution cells stay last and equal rates use natural names", () => {
  const rows = [
    moduleRow("Module 10", [2], [4]),
    moduleRow("Module 2", [1], [2]),
    moduleRow("No executions", [0], [0]),
    moduleRow("Zero failures", [0], [10]),
  ];
  for (const key of ["window", 0] as const) {
    assert.deepEqual(
      names([...rows].sort((a, b) => compareModules(a, b, key, true, 0, 0))),
      ["Zero failures", "Module 2", "Module 10", "No executions"],
    );
    assert.deepEqual(
      names([...rows].sort((a, b) => compareModules(a, b, key, false, 0, 0))),
      ["Module 2", "Module 10", "Zero failures", "No executions"],
    );
  }
  assert.ok(compareModules(rows[0], rows[1], "name", true, 0, 0) > 0);
  assert.ok(compareModules(rows[0], rows[1], "name", false, 0, 0) < 0);
});
