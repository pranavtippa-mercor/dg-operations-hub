import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "scripts/modules_collect.py"
spec = importlib.util.spec_from_file_location("modules_collect", MODULE)
M = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = M
spec.loader.exec_module(M)
UTC = timezone.utc


def table(rows, total=None, truncated=False):
    lines = ["audit_id\tstatus\tcompleted_at"] + ["\t".join(r) for r in rows]
    return (f"<METADATA><displayed_rows>{len(rows)}</displayed_rows>"
            f"<total_rows>{len(rows) if total is None else total}</total_rows>"
            + ("<is_truncated>true</is_truncated>" if truncated else "")
            + "</METADATA><TSV_DATA>\n" + "\n".join(lines) + "\n</TSV_DATA>"
            + '\n{"_mercor_rid":"not-data"}')


class ModuleTests(unittest.TestCase):
    def test_verified_quiet_window_publishes_zero_instead_of_stale_rates(self):
        class Client:
            def call(self, tool, args):
                if tool == 'datadog_load_datadog_skill':
                    return {}
                return '<METADATA><displayed_rows>0</displayed_rows><total_rows>0</total_rows></METADATA><TSV_DATA></TSV_DATA>'
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'data.json'
            path.write_text('{"previous":"rates"}')
            config={'studio':{'world':'production','campaign':'example'},'modules':{'cache_dir':folder,'data':str(path)}}
            result=M.collect_modules(Client(),config,now=M.parse('2026-09-19T00:15:00Z'))
            self.assertEqual(result['modules'],[])
            self.assertEqual(result['coverage']['production_audits'],0)
            self.assertTrue(result['coverage']['audit_event_counts_verified'])
            self.assertEqual(json.loads(path.read_text())['modules'],[])

    def test_tsv_checks_source_coverage_and_ignores_telemetry(self):
        row = ["qcaud_example", "complete", "2026-09-18T23:00:00Z"]
        self.assertEqual(M.parse_table(table([row]))[0]["audit_id"], row[0])
        for payload in (table([row], total=2), table([row], truncated=True),
                        table([row]).replace("<displayed_rows>1", "<displayed_rows>2")):
            with self.assertRaises(M.IncompleteResult):
                M.parse_table(payload)

    def test_exact_latest_completion_dedupes_within_the_same_hour(self):
        class Query:
            def complete(self, *_):
                return [
                    {"audit_id": "qcaud_example", "status": "failed", "completed_at": "2026-09-18T23:01:00Z", "events": "2"},
                    {"audit_id": "qcaud_example", "status": "complete", "completed_at": "2026-09-18T23:02:00Z", "events": "1"},
                    # An interval split repeats its inclusive boundary.
                    {"audit_id": "qcaud_example", "status": "failed", "completed_at": "2026-09-18T23:01:00Z", "events": "2"},
                ]
        audits, events = M.audit_rows(Query(), M.parse("2026-09-18T23:00:00Z"), M.parse("2026-09-19T00:00:00Z"))
        self.assertFalse(audits["qcaud_example"]["failed"])
        self.assertEqual(sum(events.values()), 3)

    def test_rates_scope_neutrals_and_completion_bucket_are_separate(self):
        now = M.parse("2026-09-19T00:15:00Z")
        audits = {
            "qcaud_good": {"at": M.parse("2026-09-18T23:12:00Z"), "failed": False},
            "qcaud_bad": {"at": M.parse("2026-09-18T23:14:00Z"), "failed": True},
            "qcaud_other": {"at": M.parse("2026-09-18T23:16:00Z"), "failed": True},
        }
        registry = {aid: {"world_id": "production" if aid != "qcaud_other" else "test", "spec_id": "spec"} for aid in audits}
        rows = [
            ["qcaud_good", "fail", "Verdict", 1],
            ["qcaud_good", "fail", "Verdict", 1],
            ["qcaud_bad", "pass", "Verdict", 1],
            ["qcaud_bad", "neutral", "Optional", 1],
            ["qcaud_bad", "never_started", "Skipped", 1],
            ["qcaud_other", "fail", "Verdict", 1],
        ]
        result = M.aggregate(audits, registry, {"spec": "Module"}, rows, "production", now)
        module = result["modules"][0]
        self.assertEqual(module["t"], [1, 2])
        self.assertEqual(result["excluded_other_world"], 1)
        self.assertEqual(result["coverage"]["excluded_failed_other_world"], 1)
        dimensions = {d["label"]: d for d in module["dims"]}
        self.assertEqual(dimensions["Verdict"]["t"], [1, 2, 0])
        self.assertEqual(dimensions["Optional"]["t"], [0, 0, 1])
        self.assertEqual(result["coverage"]["ignored_dimension_statuses"], {"never_started": 1})
        self.assertEqual(module["d"][-2:], [2, 0])

    def test_dst_window_is_based_on_real_elapsed_hours(self):
        start, width = M.window(M.parse("2026-11-01T20:00:00Z"))
        self.assertEqual(width, 26)
        self.assertEqual(M.stamp(start), "2026-10-31T19:00:00.000000Z")
        _, width = M.window(M.parse("2026-03-08T19:00:00Z"))
        self.assertEqual(width, 24)
        self.assertEqual(M.parse("2026-09-17T19:03:55.66Z").microsecond, 660000)

    def test_world_mapping_requires_snapshot_evidence(self):
        with self.assertRaises(ValueError):
            M.record_audit({"qc_spec_id": "spec", "created_at": "2026-09-18T00:00:00Z"})

    def test_foreign_burst_is_resolved_by_exact_subject_not_a_baseline(self):
        def row(aid):
            return {"qc_audit_id": aid, "qc_spec_id": "spec", "created_at": "2026-09-18T00:00:00Z",
                    "subject_id": "foreign-task", "subject_state_snapshot": {"world_id": "other-world"}}
        class Client:
            def __init__(self):
                self.exact = []
            def studio(self, method, path, params=None):
                if path == "/qc-audits/":
                    return {"audits": [] if params.get("world_id") else [row("qcaud_foreign1"), row("qcaud_foreign2")]}
                self.exact.append(path)
                return row(path.split("/")[-1])
        client = Client()
        cache = {"audits": {"qcaud_prod": {"world_id": "production", "spec_id": "spec", "subject_id": "prod-task"}},
                 "specs": {"spec": "Module"}}
        with tempfile.TemporaryDirectory() as directory:
            M.resolve_audits(client, cache, dict.fromkeys(["qcaud_prod", "qcaud_foreign1", "qcaud_foreign2"]),
                             {"studio": {"world": "production"}}, Path(directory) / "registry.json")
        self.assertEqual(len(client.exact), 1)
        self.assertEqual(cache["audits"]["qcaud_foreign2"]["world_id"], "other-world")

    def test_closed_dimension_chunk_rechecks_late_indexing(self):
        hour = "2026-09-18T23:00:00.000000Z"
        class Query:
            def __init__(self):
                self.fingerprints, self.pulls = 0, 0
            def run(self, event, start, end, sql, columns=()):
                if sql.startswith(f"SELECT {M.HOUR_SQL} AS hour"):
                    self.fingerprints += 1
                    return [{"hour": hour, "events": "1" if self.fingerprints == 1 else "2"}]
                self.pulls += 1
                return [{"audit_id": "qcaud_example", "status": "pass", "label": "'Verdict'", "hour": hour,
                         "events": "1" if self.pulls == 1 else "2"}]
        query = Query()
        with tempfile.TemporaryDirectory() as directory:
            rows, receipt = M.dimensions(query, M.parse(hour), M.parse("2026-09-19T00:00:00Z"), Path(directory))
            self.assertEqual(query.pulls, 2)
            self.assertEqual(receipt["dimension_source_events"], 2)
            self.assertEqual(rows[0][2], "Verdict")

    def test_incomplete_source_keeps_previous_output(self):
        class Client:
            def call(self, tool, arguments):
                if tool == "datadog_analyze_datadog_logs":
                    raise RuntimeError("Source unavailable")
                return "loaded"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "data.json"
            output.write_text('{"last":"valid"}')
            config = {"studio": {"world": "production", "campaign": "campaign"},
                      "modules": {"cache_dir": directory, "data": str(output)}}
            with self.assertRaises(RuntimeError):
                M.collect_modules(Client(), config)
            self.assertEqual(json.loads(output.read_text()), {"last": "valid"})


if __name__ == "__main__":
    unittest.main()
