import { DOMAINS, priority, signal, type Module, type Task } from "./model.ts";

export const CONFIRMATIONS: Record<string, string> = {
  confirmed: "Confirmed",
  at_risk: "At risk",
  replied: "Replied",
  awaiting: "Awaiting reply",
  no_request: "No request",
  not_checked: "Not checked",
};

export type TaskSortKey =
  | "priority"
  | "name"
  | "domain"
  | "stage"
  | "owner"
  | "updated"
  | "moved"
  | "due"
  | "confirmation"
  | "slack"
  | "last_actor"
  | "signal";

export type ModuleSortKey = "name" | "window" | number;

const collator = new Intl.Collator("en", {
  numeric: true,
  sensitivity: "base",
});
const compareText = (a: string, b: string) => collator.compare(a, b);
const timestamp = (value: string | null | undefined): number | null => {
  const parsed = value ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed : null;
};

// Keep unavailable values last in either direction, without reversing ties.
function compareValues(
  a: string | number | null,
  b: string | number | null,
  ascending: boolean,
) {
  if (a === null) return b === null ? 0 : 1;
  if (b === null) return -1;
  const result =
    typeof a === "number" && typeof b === "number"
      ? a - b
      : compareText(String(a), String(b));
  return ascending ? result : -result;
}

function taskValue(t: Task, key: TaskSortKey, now: number) {
  switch (key) {
    case "priority":
      return priority(t, now);
    case "domain":
      return DOMAINS[t.domain] || t.domain;
    case "updated":
      return timestamp(t.updated_at);
    case "moved":
      return timestamp(t.transitioned_at);
    case "due":
      return timestamp(t.deadline?.at);
    case "slack":
      return timestamp(t.slack_activity?.at);
    case "confirmation":
      return (
        CONFIRMATIONS[t.confirmation?.state || "not_checked"] || "Not checked"
      );
    case "signal":
      return signal(t, now);
    default:
      return t[key];
  }
}

export function compareTasks(
  a: Task,
  b: Task,
  key: TaskSortKey,
  ascending: boolean,
  now: number,
) {
  return (
    compareValues(
      taskValue(a, key, now),
      taskValue(b, key, now),
      key === "priority" ? !ascending : ascending,
    ) || compareText(a.name, b.name)
  );
}

function moduleValue(m: Module, key: ModuleSortKey, start: number, end: number) {
  if (key === "name") return m.name;
  const sum = (values: number[]) =>
    values.slice(start, end + 1).reduce((total, value) => total + value, 0);
  const failures = key === "window" ? sum(m.f) : m.f[key];
  const denominator = key === "window" ? sum(m.d) : m.d[key];
  return denominator > 0 && Number.isFinite(failures)
    ? failures / denominator
    : null;
}

export function compareModules(
  a: Module,
  b: Module,
  key: ModuleSortKey,
  ascending: boolean,
  start: number,
  end: number,
) {
  return (
    compareValues(
      moduleValue(a, key, start, end),
      moduleValue(b, key, start, end),
      ascending,
    ) || compareText(a.name, b.name)
  );
}
