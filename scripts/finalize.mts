import { readFileSync, writeFileSync, renameSync, existsSync } from "node:fs";
import { signal, validateSnapshot, type Snapshot } from "../lib/model.ts";
const file = process.argv[2] || ".private/snapshot.json",
  historyFile = ".private/history.json";
const data = validateSnapshot(JSON.parse(readFileSync(file, "utf8")));
const code: Record<string, string> = {
  "Red zone": "RZ",
  "No update since claim": "NS",
  "Stale · review reassignment": "SR",
  "Stale · follow up": "SW",
  "Queue aging": "QA",
  Blocked: "BL",
  Ready: "RD",
  Active: "AC",
  "In queue": "IQ",
  Delivered: "PK",
  Frozen: "PK",
};
const history: Snapshot["history"] = existsSync(historyFile)
  ? JSON.parse(readFileSync(historyFile, "utf8"))
  : data.history;
const at = data.sources.tasks.at!;
const now = Date.parse(at);
const entry = {
  at,
  c: {} as Record<string, number>,
  t: {} as Record<string, string>,
};
for (const t of data.tasks) {
  const name = signal(t, now);
  entry.t[t.id] = code[name] || "UK";
  const legacy =
    {
      "Red zone": "RED ZONE",
      "No update since claim": "NEVER STARTED",
      "Stale · review reassignment": "STALE — REASSIGN",
      "Stale · follow up": "STALE — WARN",
    }[name] || name;
  entry.c[legacy] = (entry.c[legacy] || 0) + 1;
}
if (process.argv.includes("--history") && history.at(-1)?.at !== at)
  history.push(entry);
data.history = history.slice(-600);
for (const [path, value] of (process.argv.includes("--history")
  ? [
      [historyFile, data.history],
      [file, data],
    ]
  : [[file, data]]) as [string, unknown][]) {
  writeFileSync(path + ".tmp", JSON.stringify(value), { mode: 0o600 });
  renameSync(path + ".tmp", path);
}
