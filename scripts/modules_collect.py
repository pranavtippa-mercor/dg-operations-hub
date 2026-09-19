#!/usr/bin/env python3
"""Deterministic, read-only AutoQC execution/dimension aggregation.

All account/world identifiers and all source records live in private config/cache.
The previous producer is needed only for optional one-time bootstrap files.
Datadog calls and Studio calls are serial; never infer a world from an exclusion
count. A successful snapshot has a resolved world for every finished audit.
"""
from __future__ import annotations

import argparse
import collections
import csv
import fcntl
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
PT = ZoneInfo("America/Los_Angeles")
TOOLS = ["studio", "datadog_load_datadog_skill", "datadog_analyze_datadog_logs"]
INTENT = {"intent": "Collect user-authorized read-only module reliability metrics for the unified operations dashboard."}
HOUR_SQL = "DATE_TRUNC('hour', timestamp)"
STATUS_SQL = "SPLIT_PART(SPLIT_PART(message,'status=',2),' dim=',1)"
LABEL_SQL = "SPLIT_PART(SPLIT_PART(message,'dim=',2),' :: ',1)"


class IncompleteResult(ValueError):
    """A tool response omitted rows; do not publish its aggregates."""


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse(value):
    # Older system Python accepts only 3/6 fractional digits, while Datadog
    # serializes e.g. .66Z when the trailing millisecond digit is zero.
    value = re.sub(r"\.(\d+)(?=Z$|[+-]\d\d:\d\d$)",
                   lambda m: "." + m[1].ljust(6, "0")[:6], value)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Source timestamp has no timezone")
    return result.astimezone(UTC)


def load(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, ensure_ascii=False, separators=(",", ":"))
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def resolve_path(value):
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def window(now):
    local = now.astimezone(PT)
    yesterday = local.date() - timedelta(days=1)
    start = datetime.combine(yesterday, datetime.min.time(), PT).replace(hour=12)
    first = start.astimezone(UTC)
    width = int((now.timestamp() // 3600) - (first.timestamp() // 3600)) + 1
    return first, width


def parse_table(payload):
    """Decode the connector's complete tagged TSV without transcript scraping."""
    if not isinstance(payload, str):
        raise ValueError("Datadog returned an unsupported result shape")
    metadata = re.search(r"<METADATA>(.*?)</METADATA>", payload, re.S)
    table = re.search(r"<TSV_DATA>(.*?)</TSV_DATA>", payload, re.S)
    if not metadata or not table:
        raise ValueError("Datadog returned no verifiable TSV metadata")
    def number(key):
        item = re.search(r"<" + key + r">\s*(\d+)\s*</" + key + ">", metadata[1])
        if not item:
            raise ValueError("Datadog response lacks row coverage metadata")
        return int(item[1])
    displayed, total = number("displayed_rows"), number("total_rows")
    if displayed != total or re.search(r"<is_truncated>\s*(?:true|1)\s*</is_truncated>", metadata[1], re.I):
        raise IncompleteResult("Datadog response is truncated")
    text = table[1].strip("\r\n")
    if total == 0 and not text:
        return []
    reader = csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    rows = list(reader)
    if len(rows) != total or any(None in row or any(v is None for v in row.values()) for row in rows):
        raise IncompleteResult("Datadog TSV rows do not match reported coverage")
    return rows


class Query:
    def __init__(self, client, campaign):
        self.client, self.campaign = client, campaign
        self.calls = 0

    def run(self, event, start, end, sql, columns=()):
        self.calls += 1
        payload = self.client.call("datadog_analyze_datadog_logs", {
            "filter": f"env:prod @campaign_id:{self.campaign} @autoqc_event:{event}",
            "from": stamp(start), "to": stamp(end), "sql_query": sql,
            "extra_columns": [{"name": key, "type": "varchar"} for key in columns],
            "max_tokens": 400000, "telemetry": INTENT,
        })
        return parse_table(payload)

    def complete(self, event, start, end, sql, columns=()):
        """Split overflowing intervals, including their shared edge and deduping later."""
        try:
            return self.run(event, start, end, sql, columns)
        except IncompleteResult:
            if (end - start).total_seconds() <= 1:
                raise IncompleteResult("Too many rows in a one-second interval; snapshot retained")
            mid = start + (end - start) / 2
            return (self.complete(event, start, mid, sql, columns)
                    + self.complete(event, mid, end, sql, columns))


def audit_rows(query, start, end):
    # Returning individual completion timestamps also makes overlapping split
    # boundaries safe: rows have an exact (id,status,timestamp) identity.
    sql = ('SELECT "@qc_audit_id" AS audit_id, "@audit_status" AS status, '
           'timestamp AS completed_at, COUNT(*) AS events FROM logs '
           'GROUP BY "@qc_audit_id", "@audit_status", timestamp ORDER BY timestamp ASC')
    rows = query.complete("audit.completed", start, end, sql, ("@qc_audit_id", "@audit_status"))
    events, latest = {}, {}
    for row in rows:
        aid, state, when = row["audit_id"], row["status"], parse(row["completed_at"])
        if not re.fullmatch(r"qcaud_[A-Za-z0-9_-]+", aid) or state not in ("complete", "failed"):
            raise ValueError("Unexpected audit completion identity/status")
        if not start <= when <= end:
            continue
        identity = (aid, state, stamp(when))
        events[identity] = max(events.get(identity, 0), int(row["events"]))
        old = latest.get(aid)
        if old is None or when > old["at"]:
            latest[aid] = {"at": when, "failed": state == "failed"}
        elif when == old["at"] and old["failed"] != (state == "failed"):
            raise ValueError("Conflicting completion statuses at the same timestamp")
    return latest, events


def fingerprint(query, event, start, end):
    rows = query.run(event, start, end,
                     f"SELECT {HOUR_SQL} AS hour, COUNT(*) AS events FROM logs GROUP BY {HOUR_SQL} ORDER BY {HOUR_SQL}")
    return {stamp(parse(row["hour"])): int(row["events"]) for row in rows}


def migrate(cache, cache_dir, world):
    if cache.get("bootstrap_imported"):
        return
    bootstrap = cache_dir / "bootstrap"
    cache["specs"].update(load(bootstrap / "spec_map.json", {}))
    registry = bootstrap / "studio_audit_registry.tsv"
    if registry.exists():
        with registry.open() as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                cache["audits"].setdefault(row["qc_audit_id"], {
                    "spec_id": row["qc_spec_id"], "world_id": world,
                    "created_at": row["created_at"], "subject_id": row["subject_id"],
                    "evidence": "legacy production-world registry bootstrap",
                })
    cache["bootstrap_imported"] = True


def record_audit(row):
    subject = row.get("subject_state_snapshot") or {}
    world = subject.get("world_id")
    if not world:
        raise ValueError("Audit record lacks its snapshot world; refusing an assumed exclusion")
    if not row.get("qc_spec_id") or not row.get("created_at"):
        raise ValueError("Audit record lacks spec/creation time")
    parse(row["created_at"])
    return {"spec_id": row["qc_spec_id"], "world_id": world,
            "created_at": row["created_at"], "subject_id": row.get("subject_id"),
            "evidence": "Studio audit subject_state_snapshot.world_id"}


def resolve_audits(client, cache, audits, config, cache_path):
    world = config["studio"]["world"]
    unmapped = set(audits) - cache["audits"].keys()
    if unmapped:
        # world_id overrides the required subject_id. A real known task is used
        # only as the API's documented placeholder, never as a filtering guess.
        subject = next((r.get("subject_id") for r in cache["audits"].values() if r.get("subject_id")), None)
        if not subject:
            # Exact lookup bootstraps the placeholder without task-list discovery.
            aid = sorted(unmapped)[0]
            record = client.studio("GET", "/qc-audits/" + aid)
            record = record.get("audit", record)
            cache["audits"][aid] = record_audit(record)
            subject = record.get("subject_id")
        if subject:
            page = client.studio("GET", "/qc-audits/", {
                "subject_kind": "task", "subject_id": subject,
                "world_id": world, "limit": 500,
            })
            if not isinstance(page.get("audits"), list):
                raise ValueError("Unexpected Studio audit page")
            for row in page["audits"]:
                try:
                    cache["audits"][row["qc_audit_id"]] = record_audit(row)
                except ValueError:
                    # A missing snapshot in a list row is resolved by exact GET.
                    continue
            atomic_json(cache_path, cache)
        if subject and len(set(audits) - cache["audits"].keys()) > 100:
            # After a long outage, partition by already-observed production
            # specs before exact lookups. The API has no offset/cursor; a capped
            # page is only a cache top-up, never evidence that absent IDs are
            # non-production. Exact lookups below close any remaining gap.
            spec_ids = sorted({r["spec_id"] for r in cache["audits"].values()
                               if r["world_id"] == world})
            for sid in spec_ids:
                page = client.studio("GET", "/qc-audits/", {
                    "subject_kind": "task", "subject_id": subject,
                    "world_id": world, "qc_spec_id": sid, "limit": 300,
                })
                for row in page.get("audits", []):
                    try:
                        cache["audits"][row["qc_audit_id"]] = record_audit(row)
                    except ValueError:
                        continue
                atomic_json(cache_path, cache)
                if len(set(audits) - cache["audits"].keys()) <= 50:
                    break
        # There is no offset/cursor on this endpoint. Resolve every remaining
        # ID directly, rather than mislabeling lag or a long outage as test data.
        foreign_subjects = set()
        for index, aid in enumerate(sorted(set(audits) - cache["audits"].keys()), 1):
            if aid in cache["audits"]:
                continue
            record = client.studio("GET", "/qc-audits/" + aid)
            mapping = record_audit(record.get("audit", record))
            cache["audits"][aid] = mapping
            # Test/canary bursts often consist of many modules on one subject.
            # Once an exact lookup identifies that subject, one subject-scoped
            # page can resolve the burst without 40 individual audit reads.
            subject = mapping.get("subject_id")
            if mapping["world_id"] != world and subject and subject not in foreign_subjects:
                foreign_subjects.add(subject)
                page = client.studio("GET", "/qc-audits/", {
                    "subject_kind": "task", "subject_id": subject, "limit": 500,
                })
                for row in page.get("audits", []):
                    try:
                        cache["audits"][row["qc_audit_id"]] = record_audit(row)
                    except ValueError:
                        continue
            if index % 10 == 0:
                atomic_json(cache_path, cache)
        atomic_json(cache_path, cache)
    needed = {cache["audits"][aid]["spec_id"] for aid in audits
              if cache["audits"][aid]["world_id"] == world}
    if needed - cache["specs"].keys():
        summary = client.studio("GET", "/qc-specs/summary", {"include_archived": True})
        rows = summary.get("qc_specs", summary.get("specs", [])) if isinstance(summary, dict) else summary
        for row in rows:
            sid = row.get("qc_spec_id") or row.get("id")
            if sid and row.get("name"):
                cache["specs"][sid] = row["name"]
        if needed - cache["specs"].keys():
            raise ValueError("Some executed QC specs have no verified display name")
        atomic_json(cache_path, cache)


def dimension_chunk(query, start, end):
    # Include hour in the grouping so inclusive API bounds cannot contaminate
    # a neighboring bucket. Deduped tuples retain their event count for coverage.
    sql = (f'SELECT "@qc_audit_id" AS audit_id, {STATUS_SQL} AS status, '
           f'{LABEL_SQL} AS label, {HOUR_SQL} AS hour, COUNT(*) AS events FROM logs '
           f'GROUP BY "@qc_audit_id", {STATUS_SQL}, {LABEL_SQL}, {HOUR_SQL}')
    # Raw dimensions are bounded to one hour; split fallbacks use raw events so
    # aggregate COUNTs cannot double-count the overlapping split boundary.
    try:
        rows = query.run("dim.verdict", start, end, sql, ("@qc_audit_id",))
    except IncompleteResult:
        raw_sql = (f'SELECT "@qc_audit_id" AS audit_id, {STATUS_SQL} AS status, '
                   f'{LABEL_SQL} AS label, timestamp AS at, COUNT(*) AS events FROM logs '
                   f'GROUP BY "@qc_audit_id", {STATUS_SQL}, {LABEL_SQL}, timestamp ORDER BY timestamp ASC')
        raw = query.complete("dim.verdict", start, end, raw_sql, ("@qc_audit_id",))
        unique = {}
        for row in raw:
            key = (row["audit_id"], row["status"], row["label"], row["at"])
            unique[key] = max(unique.get(key, 0), int(row["events"]))
        grouped = collections.Counter()
        for (a, s, label, at), count in unique.items():
            grouped[(a, s, label, stamp(parse(at).replace(minute=0, second=0, microsecond=0)))] += count
        rows = [{"audit_id": a, "status": s, "label": label, "hour": hour, "events": n}
                for (a, s, label, hour), n in grouped.items()]
    result = []
    for row in rows:
        if parse(row["hour"]) != start:
            continue
        label = row["label"].strip()
        if len(label) >= 2 and label[0] == label[-1] and label[0] in "\"'":
            label = label[1:-1]
        if not re.fullmatch(r"qcaud_[A-Za-z0-9_-]+", row["audit_id"]) or not label:
            raise ValueError("Malformed dimension verdict identity/label")
        result.append([row["audit_id"], row["status"], label, int(row["events"])])
    return result


def dimensions(query, start, end, cache_dir):
    start = start.replace(minute=0, second=0, microsecond=0)
    all_rows = []
    reused, pulled = 0, 0
    for attempt in range(3):
        counts = fingerprint(query, "dim.verdict", start, end)
        all_rows = []
        for hour_key, expected in sorted(counts.items()):
            hour = parse(hour_key)
            stop = min(hour + timedelta(hours=1) - timedelta(microseconds=1), end)
            path = cache_dir / "dimensions" / (hour.strftime("%Y%m%dT%H") + ".json")
            old = load(path, {})
            cached_rows = old.get("rows", [])
            valid_rows = isinstance(cached_rows, list) and all(
                isinstance(r, list) and len(r) == 4
                and all(isinstance(v, str) for v in r[:3])
                and r[0].startswith("qcaud_") and type(r[3]) is int and r[3] > 0
                for r in cached_rows)
            if (old.get("start") == hour_key and old.get("end") == stamp(stop)
                    and old.get("events") == expected and valid_rows
                    and sum(r[3] for r in cached_rows) == expected):
                rows = old["rows"]
                reused += 1
            else:
                rows = dimension_chunk(query, hour, stop)
                pulled += 1
                atomic_json(path, {"start": hour_key, "end": stamp(stop),
                                   "events": sum(r[3] for r in rows), "rows": rows})
            all_rows.extend(rows)
        final = fingerprint(query, "dim.verdict", start, end)
        actual = {}
        for hour_key in counts:
            path = cache_dir / "dimensions" / (parse(hour_key).strftime("%Y%m%dT%H") + ".json")
            actual[hour_key] = load(path)["events"]
        if actual == final:
            return all_rows, {"dimension_source_events": sum(final.values()),
                              "dimension_chunks_reused": reused, "dimension_chunks_pulled": pulled}
    raise IncompleteResult("Dimension indexing changed during collection; cached progress retained")


def aggregate(audits, registry, specs, dim_rows, world, now):
    start, width = window(now)
    groups = {}
    included, excluded = {}, []
    for aid, event in audits.items():
        mapping = registry[aid]
        if mapping["world_id"] != world:
            excluded.append(aid)
            continue
        bucket = int((event["at"].timestamp() // 3600) - (start.timestamp() // 3600))
        if not 0 <= bucket < width:
            raise ValueError("Audit completion is outside its requested time window")
        included[aid] = (mapping["spec_id"], bucket)
        group = groups.setdefault(mapping["spec_id"], {"f": [0] * width, "d": [0] * width, "dims": {}})
        group["d"][bucket] += 1
        group["f"][bucket] += int(event["failed"])
    seen, ignored = set(), collections.Counter()
    for aid, state, label, _events in dim_rows:
        key = (aid, state, label)
        if aid not in included or key in seen:
            continue
        seen.add(key)
        if state not in ("pass", "fail", "neutral"):
            ignored[state] += 1
            continue
        sid, bucket = included[aid]
        row = groups[sid]["dims"].setdefault(label, {"label": label, "f": [0] * width, "g": [0] * width, "n": [0] * width})
        row["f"][bucket] += int(state == "fail")
        row["g"][bucket] += int(state in ("pass", "fail"))
        row["n"][bucket] += int(state == "neutral")
    modules = []
    names = collections.Counter(specs[sid] for sid in groups)
    for sid, group in groups.items():
        dims = list(group["dims"].values())
        for row in dims:
            row["t"] = [sum(row[k]) for k in ("f", "g", "n")]
        name = specs[sid] if names[specs[sid]] == 1 else specs[sid] + " · " + sid[-8:]
        modules.append({"name": name, "spec_id": sid, "f": group["f"], "d": group["d"],
                        "t": [sum(group["f"]), sum(group["d"])],
                        "dims": sorted(dims, key=lambda d: (-d["t"][0], d["label"]))})
    data = {"generated_at_utc": stamp(datetime.now(UTC)), "snapshot_at_utc": stamp(now),
            "tz": "America/Los_Angeles", "window_start_local": start.astimezone(PT).strftime("%Y-%m-%d %H"),
            "window_end_local": now.astimezone(PT).strftime("%Y-%m-%d %H:%M"), "n_hours": width,
            "hours": [stamp(start + timedelta(hours=i)) for i in range(width)],
            "world_id": world, "source": "Datadog audit.completed + dim.verdict, joined to Studio audit snapshot worlds",
            "excluded_other_world": len(excluded), "modules": sorted(modules, key=lambda m: m["name"]),
            "coverage": {"finished_audits": len(audits), "production_audits": len(included),
                         "unresolved_audits": 0, "ignored_dimension_statuses": dict(ignored),
                         "production_bootstrap_mappings": sum(
                             registry[a].get("evidence", "").startswith("legacy") for a in included),
                         "excluded_failed_other_world": sum(int(audits[a]["failed"]) for a in excluded)}}
    validate_aggregate(data)
    return data


def validate_aggregate(data):
    width = data["n_hours"]
    for module in data["modules"]:
        for row, keys in [(module, ("f", "d"))] + [(d, ("f", "g", "n")) for d in module["dims"]]:
            if any(len(row[k]) != width or any(type(v) is not int or v < 0 for v in row[k]) for k in keys):
                raise ValueError("Invalid module array width/count")
            if any(f > d for f, d in zip(row[keys[0]], row[keys[1]])) or row["t"] != [sum(row[k]) for k in keys]:
                raise ValueError("Module totals do not reconcile")
    if sum(m["t"][1] for m in data["modules"]) != data["coverage"]["production_audits"]:
        raise ValueError("Production denominator does not match mapped audits")


def collect_modules(client, config, *, cache_dir=None, now=None):
    settings = config.get("modules", {})
    if not isinstance(settings, dict):
        raise ValueError("Module collector needs its independent private cache configuration")
    directory = resolve_path(cache_dir or settings.get("cache_dir", ".private/modules"))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A module collection is already running")
        return _collect(client, config, settings, directory, now or datetime.now(UTC))


def _collect(client, config, settings, directory, now):
    now = now.astimezone(UTC)
    start, _ = window(now)
    world, campaign = config["studio"]["world"], config["studio"]["campaign"]
    cache_path = directory / "registry.json"
    cache = load(cache_path, {"version": 1, "audits": {}, "specs": {}})
    if cache.get("world_id", world) != world or cache.get("campaign_id", campaign) != campaign:
        raise ValueError("The private module cache belongs to a different source scope")
    cache.update(world_id=world, campaign_id=campaign)
    migrate(cache, directory, world)
    atomic_json(cache_path, cache)
    for skill in ("datadog/logs", "datadog/ddsql"):
        client.call("datadog_load_datadog_skill", {"skill_name": skill, "header_only": True, "telemetry": INTENT})
    query = Query(client, campaign)
    # A cheap query fails before any bulk collection if the connection is wedged.
    fingerprint(query, "audit.completed", max(start, now - timedelta(hours=1)), now)
    for attempt in range(3):
        audits, events = audit_rows(query, start, now)
        resolve_audits(client, cache, audits, config, cache_path)
        prod = [a for a in audits if cache["audits"][a]["world_id"] == world]
        earliest = min((parse(cache["audits"][a]["created_at"]) for a in prod), default=start)
        if prod:
            dims, receipt = dimensions(query, min(start, earliest), now, directory)
        else:
            # A fully verified quiet window is valid; do not freeze old rates.
            dims, receipt = [], {"dimension_source_events": None, "dimension_chunks_reused": 0,
                                 "dimension_chunks_pulled": 0, "dimension_queries_skipped": "no completed production audits"}
        # Verify the full source event count after all chunks and mappings were
        # retrieved, catching late-indexed executions (including successes).
        final_counts = fingerprint(query, "audit.completed", start, now)
        observed_counts = collections.Counter()
        for (_a, _s, at), count in events.items():
            observed_counts[stamp(parse(at).replace(minute=0, second=0, microsecond=0))] += count
        if dict(observed_counts) == final_counts:
            break
    else:
        raise IncompleteResult("Audit indexing changed during collection; prior snapshot retained")
    data = aggregate(audits, cache["audits"], cache["specs"], dims, world, now)
    data["campaign_id"] = campaign
    data["world_name"] = config["studio"].get("world_name", "Production")
    data["coverage"].update(receipt, datadog_calls=query.calls,
                            verified_at_utc=stamp(datetime.now(UTC)), audit_event_counts_verified=True)
    # Keep the rolling view plus seven days of cheap lookup history; long-running
    # audits included now are retained even when their creation is older.
    cutoff = now - timedelta(days=7)
    cache["audits"] = {a: r for a, r in cache["audits"].items() if a in audits or parse(r["created_at"]) >= cutoff}
    atomic_json(cache_path, cache)
    earliest_hour = min(start, earliest).replace(minute=0, second=0, microsecond=0)
    for file in (directory / "dimensions").glob("*.json"):
        if datetime.strptime(file.stem, "%Y%m%dT%H").replace(tzinfo=UTC) < earliest_hour:
            file.unlink()
    output = resolve_path(settings.get("data", str(directory / "data.json")))
    atomic_json(output, data)
    atomic_json(directory / "receipt.json", {"generated_at": data["generated_at_utc"], "snapshot_at": data["snapshot_at_utc"], **data["coverage"]})
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / ".private/config.json"))
    args = parser.parse_args()
    from source_client import SourceClient
    config = json.loads(Path(args.config).read_text())
    with SourceClient(config, upstream_tools=TOOLS, timeout=180) as client:
        data = collect_modules(client, config)
    print(json.dumps({"ok": True, "modules": len(data["modules"]),
                      "dimensions": sum(len(m["dims"]) for m in data["modules"]),
                      "failed": sum(m["t"][0] for m in data["modules"]),
                      "finished": sum(m["t"][1] for m in data["modules"]),
                      "coverage": data["coverage"]}), flush=True)


if __name__ == "__main__":
    main()
