"""Accounting regressions, browser API integration and Python/HTML parity."""
import csv
import io
import json
import shutil
import subprocess
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import hpc_monitor_core as c
from HPC_Monitor_Web import MonitorSession, make_server, web_page


def stamp(hh, day="2026-11-10"):
    return day + "T" + hh + ":00"


def load(store, kind, rows):
    fields = c.SCHEMA[kind]
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter="\t")
    writer.writerow(fields)
    writer.writerows([r.get(k, "") for k in fields] for r in rows)
    store.load_text(stream.getvalue(), kind + ".tsv")
    return store


def node(hh, name="n1", alloc=16, total=64):
    return dict(timestamp=stamp(hh), node=name, partitions="groupA", state="MIXED" if alloc else "IDLE",
                cpu_total=total, cpu_alloc=alloc, mem_total_mb=1024, mem_alloc_mb=128, load_pct=25, mem_pct=12.5)


def user(hh, name="n1", cpu=16, ids="100", owner="alice"):
    return dict(timestamp=stamp(hh), node=name, user=owner, partition="groupA", cpus_alloc=cpu,
                mem_alloc_mb=128, n_jobs=len(c.parse_list(ids)), job_ids=ids)


def started(hh, jid="100", owner="alice", start=None):
    return dict(timestamp=stamp(hh), job_id=jid, user=owner, partition="groupA", submit_time=stamp("00:00"),
                start_time=start or stamp("00:00"), alloc_cpus=16, n_nodes=1, state="RUNNING", wait_sec=0)


def pending(hh):
    return dict(timestamp=stamp(hh), job_id="300", user="alice", partition="groupA",
                submit_time=stamp("00:00"), req_cpus=64, wait_sec=60, reason="Resources", priority=10)


def fixture():
    store = c.LogStore()
    load(store, "node_cpu", [node("00:00"), node("00:30", alloc=32), node("01:00", alloc=0),
                             node("00:00", "n2", 0, 128), node("00:30", "n2", 64, 128), node("01:00", "n2", 0, 128)])
    load(store, "user_cpu", [user("00:00", ids="100,101"), user("00:30", cpu=32), user("00:30", "n2", 64)])
    load(store, "queue_started", [started("00:00"), started("00:30"), started("00:00", "101")])
    return store


class AccountingTests(unittest.TestCase):
    def test_timezones_and_invalid_date(self):
        self.assertEqual(c.parse_time(stamp("00:30")), c.parse_time("2026-11-09T15:30:00Z"))
        self.assertEqual(c.parse_time("2026-11-10T00:30:00+09:00"), c.parse_time(stamp("00:30")))
        self.assertIsNone(c.parse_time("2026-02-30T00:00:00"))
        self.assertIsNone(c.number("NaN"))
        self.assertIsNone(c.number("inf"))

    def test_weighted_capacity_and_multi_node_job(self):
        d = c.analyze(fixture())
        self.assertEqual(d["summary"]["coreHours"], 56)
        self.assertEqual(d["summary"]["running"], 2)
        self.assertEqual(d["summary"]["jobHours"], 1.5)
        self.assertEqual(d["summary"]["busyPct"], 75)
        self.assertAlmostEqual(d["summary"]["allocPct"], 56 / 192 * 100)
        self.assertEqual(d["groups"][0]["jobs"], 2)
        self.assertEqual(d["groups"][0]["jobHours"], 1.5)
        self.assertEqual(d["nodes"][0]["jobs"], 2)
        self.assertEqual(d["nodes"][1]["jobs"], 1)

    def test_disappearance_does_not_imply_success(self):
        d = c.analyze(fixture())
        self.assertEqual(d["summary"]["inferredEnded"], 2)
        self.assertTrue(all(j["status"] == "종료 추정" for j in d["jobs"]))
        self.assertEqual(d["jobs"][0]["state"], "RUNNING")

    def test_job_present_at_last_snapshot_is_not_ended(self):
        store = fixture()
        load(store, "user_cpu", [user("01:00")])
        d = c.analyze(store)
        self.assertIsNone(next(j for j in d["jobs"] if j["id"] == "100")["end"])
        self.assertEqual(d["summary"]["coreHours"], 56)
        self.assertEqual(d["summary"]["inferredEnded"], 1)

    def test_partial_query_clips_runtime(self):
        d = c.analyze(fixture(), {"from": c.parse_time(stamp("00:15")), "to": c.parse_time(stamp("00:45"))})
        self.assertEqual(d["summary"]["coreHours"], 28)
        self.assertEqual(d["summary"]["jobHours"], .75)
        self.assertEqual(d["summary"]["started"], 0)

    def test_node_filter(self):
        d = c.analyze(fixture(), dict(nodes=["n2"]))
        self.assertEqual(d["summary"]["coreHours"], 32)
        self.assertEqual(d["summary"]["running"], 1)
        self.assertEqual(d["summary"]["jobHours"], .5)
        self.assertIsNone(d["summary"]["pending"])

    def test_unknown_group_is_empty(self):
        d = c.analyze(fixture(), dict(groups=["missing"]))
        self.assertFalse(d["nodes"])
        self.assertEqual(d["summary"]["running"], 0)

    def test_user_capacity_denominator_remains_full(self):
        store = c.LogStore()
        load(store, "node_cpu", [node("00:00", alloc=48), node("00:30", alloc=48), node("01:00", alloc=0)])
        load(store, "user_cpu", [user("00:00"), user("00:30"), user("00:00", cpu=32, ids="200", owner="bob"), user("00:30", cpu=32, ids="200", owner="bob")])
        d = c.analyze(store, dict(users=["alice"]))
        self.assertEqual(d["summary"]["coreHours"], 16)
        self.assertEqual(d["summary"]["allocPct"], 25)
        self.assertEqual(d["users"][0]["contribution"], 25)

    def test_missing_user_snapshot_keeps_contribution_denominator(self):
        store = c.LogStore()
        load(store, "node_cpu", [node("00:00"), node("00:30"), node("01:00")])
        load(store, "user_cpu", [user("00:00")])
        d = c.analyze(store, dict(users=["alice"]))
        self.assertEqual(d["users"][0]["contribution"], 12.5)

    def test_long_gap_is_not_extrapolated(self):
        store = c.LogStore()
        load(store, "node_cpu", [node("00:00"), node("00:30"), node("02:00"), node("02:30")])
        load(store, "user_cpu", [user("00:30")])
        d = c.analyze(store, dict(bucket="hour"))
        self.assertEqual(d["summary"]["coreHours"], 24)
        self.assertEqual(d["summary"]["coverage"], 60)
        self.assertIsNone(d["series"][1]["allocPct"])
        self.assertIsNone(d["jobs"][0]["end"])
        self.assertEqual(c.analyze(store, dict(denominator="calendar"))["summary"]["busyPct"], 60)

    def test_midnight_split(self):
        store = c.LogStore()
        load(store, "node_cpu", [node("23:45"), dict(node("00:15"), timestamp="2026-11-11T00:15:00")])
        d = c.analyze(store)
        self.assertEqual([r["coreHours"] for r in d["series"]], [4, 4])

    def test_repeated_pending_rows_not_repeated_submissions(self):
        store = fixture()
        load(store, "queue_pending", [pending("00:00"), pending("00:30"), pending("01:00")])
        d = c.analyze(store)
        self.assertEqual(d["summary"]["submitted"], 3)
        self.assertEqual(d["summary"]["pending"], 1)
        self.assertEqual(d["series"][0]["pendingMax"], 1)
        self.assertIsNone(next(j for j in d["jobs"] if j["id"] == "300")["waitMin"])
        historical = c.analyze(store, {"to": c.parse_time(stamp("00:30"))})
        self.assertEqual(historical["summary"]["pending"], 1)

    def test_empty_pending_snapshot_uses_node_clock(self):
        store = fixture()
        load(store, "queue_pending", [pending("00:00"), pending("00:30")])
        self.assertEqual(c.analyze(store)["summary"]["pending"], 0)

    def test_start_event_does_not_fabricate_job_runtime(self):
        store = c.LogStore()
        load(store, "queue_started", [started("00:00"), started("00:30", "101", start=stamp("00:30"))])
        d = c.analyze(store, dict(startedMode="event", to=c.parse_time(stamp("01:00"))))
        self.assertEqual(d["summary"]["started"], 2)
        self.assertIsNone(d["summary"]["running"])
        self.assertIsNone(d["summary"]["jobHours"])

    def test_single_snapshot_has_no_elapsed_observation(self):
        store = c.LogStore()
        load(store, "node_cpu", [node("00:00")])
        self.assertTrue(c.analyze(store)["empty"])
        self.assertIn("error", c.analyze(store))

    def test_gpu_process_union_and_energy(self):
        store = c.LogStore()
        load(store, "gpu", [dict(timestamp=stamp(hh), node="g1", gpu_index="0", uuid="u1", gpu_name="GPU",
                                 gpu_util=0 if i == 0 else 100, mem_total_mb=10000, mem_used_mb=1500,
                                 mem_pct=15, temp_c=50 + i, power_w=100 if i == 0 else 200)
                            for i, hh in enumerate(("00:00", "00:30", "01:00"))])
        load(store, "gpu_user", [dict(timestamp=stamp("00:00"), node="g1", gpu_index="0", user="alice", job_id="1", pid=str(pid), mem_used_mb=mem)
                                 for pid, mem in ((10, 500), (11, 1000))])
        d = c.analyze(store)
        self.assertEqual(d["gpu"][0]["util"], 50)
        self.assertEqual(d["gpu"][0]["energyWh"], 150)
        self.assertEqual(d["users"][0]["gpuHours"], .5)
        self.assertEqual(d["users"][0]["gpuMemHours"], 750)
        self.assertIsNone(d["summary"]["running"])

    def test_dedup_and_last_conflicting_row_wins(self):
        store = fixture()
        load(store, "node_cpu", [node("00:00")])
        self.assertEqual(c.analyze(store)["summary"]["coreHours"], 56)
        load(store, "node_cpu", [node("00:00", alloc=20)])
        self.assertEqual(c.analyze(store)["summary"]["coreHours"], 58)
        self.assertEqual(store.conflicts, 1)

    def test_tsv_bom_quotes_repeated_headers_and_bad_rows(self):
        store = c.LogStore()
        text = "\ufeff#" + "\t".join(c.SCHEMA["user_cpu"]) + "\n"
        text += "\t".join(str(user("00:00").get(k, "")) for k in c.SCHEMA["user_cpu"]) + "\n"
        text += "\t".join(c.SCHEMA["node_cpu"]) + "\nwrong\trow\n"
        store.load_text(text)
        self.assertEqual(len(store.tables["user_cpu"]), 1)
        self.assertEqual(store.issue_total, 1)
        self.assertEqual(c.parse_list("123_1,123_[2-4],125"), ["123_1", "123_[2-4]", "125"])

    def test_session_roundtrip_and_demo_marker(self):
        store, copy = fixture(), c.LogStore()
        copy.load_text("\n" + json.dumps(store.session()), "roundtrip.json")
        self.assertEqual(c.analyze(copy)["summary"]["coreHours"], 56)
        copy = c.LogStore()
        copy.load_session(c.demo_store().session())
        self.assertTrue(copy.demo)

    def test_csv_formula_injection_and_utf8_bom(self):
        text = c.table_csv([dict(user="=1+2", value=-5)], [("user", "사용자"), ("value", "값")])
        self.assertTrue(text.startswith("\ufeff"))
        self.assertIn("'=1+2", text)
        self.assertIn("-5", text)

    def test_demo_reference_totals(self):
        store = c.demo_store()
        d = c.analyze(store)
        self.assertEqual(d["summary"]["coreHours"], 35968)
        self.assertEqual(d["summary"]["running"], 176)
        self.assertAlmostEqual(sum(r["coreHours"] for r in d["users"]), d["summary"]["coreHours"])
        self.assertEqual(sum(store.describe()["counts"].values()), 7940)
        json.dumps(d, allow_nan=False)

    @unittest.skipUnless(shutil.which("node"), "Node.js is optional; parity test skipped")
    def test_python_matches_original_html_across_filters(self):
        script = """
const fs=require('fs'),vm=require('vm');vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
const s=HPC.demo(),options=JSON.parse(process.argv[2]);const results=options.map(o=>HPC.analyze(s,o));
console.log(JSON.stringify(results));
"""
        options = [{}, dict(users=["kang"]), dict(nodes=["node06"]), dict(groups=["cpu-large"]),
                   dict(denominator="calendar"), dict(startedMode="event"), dict(nodeClock=False),
                   dict(offset=0), dict(bucket="hour"), dict(bucket="week"), dict(bucket="month"),
                   {"from": c.parse_time("2026-11-03T09:15:00"), "to": c.parse_time("2026-11-05T20:45:00")}]
        reference = Path(__file__).with_name("reference_engine.js")
        expected = json.loads(subprocess.check_output(["node", "-e", script, str(reference), json.dumps(options)], text=True))
        store = c.demo_store()
        for opt, old in zip(options, expected):
            new = c.analyze(store, opt)
            with self.subTest(options=opt):
                for key, value in old["summary"].items():
                    if isinstance(value, (float, int)):
                        self.assertAlmostEqual(new["summary"][key], value, places=6, msg=key)
                    else:
                        self.assertEqual(new["summary"][key], value, key)
                for key in ("series",):
                    self.assertEqual(len(new[key]), len(old[key]))
                    for a, b in zip(new[key], old[key]):
                        for field in b:
                            if isinstance(b[field], (int, float)):
                                self.assertAlmostEqual(a[field], b[field], places=6, msg=field)
                            else:
                                self.assertEqual(a[field], b[field])
                for table, label, fields in [
                    ("nodes", "name", ["jobs", "coreHours", "jobHours", "busyPct", "allocPct", "coverage"]),
                    ("groups", "name", ["jobs", "coreHours", "jobHours", "allocPct"]),
                    ("users", "name", ["jobs", "coreHours", "jobHours", "gpuHours", "gpuMemHours", "contribution"]),
                    ("gpu", "id", ["util", "power", "tempMax", "energyWh"]),
                ]:
                    rows = {r[label]: r for r in new[table]}
                    self.assertEqual(set(rows), {r[label] for r in old[table]})
                    for row in old[table]:
                        for field in fields:
                            value = row[field]
                            if isinstance(value, (float, int)):
                                self.assertAlmostEqual(rows[row[label]][field], value, places=6, msg=table + "." + field)
                            else:
                                self.assertEqual(rows[row[label]][field], value)


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = MonitorSession(fixture())
        cls.server = make_server(port=0, session=cls.session, max_upload_mb=2)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.server.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def post(self, message, token=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-HPC-Token"] = self.server.monitor_token
        with urlopen(Request(self.url + "/api/command", json.dumps(message).encode(), headers), timeout=15) as r:
            return json.load(r)["data"]

    def test_health_and_python_frontend(self):
        with urlopen(self.url + "/health") as r:
            self.assertEqual(json.load(r)["engine"], "python")
        with urlopen(self.url) as r:
            html = r.read().decode()
            self.assertIn("/api/import", html)
            self.assertIn("Python Web", html)
            self.assertNotIn("new Worker", html)
            self.assertNotIn("function analyze(store", html)

    def test_no_token_is_rejected(self):
        with self.assertRaises(HTTPError) as cm:
            self.post(dict(op="clear"), token=False)
        self.assertEqual(cm.exception.code, 403)

    def test_analysis_and_job_export(self):
        self.session.store = fixture()
        d = self.post(dict(op="analyze", options={}))
        self.assertEqual(d["summary"]["coreHours"], 56)
        self.assertEqual(d["jobsTotal"], 2)
        self.assertEqual(len(self.post(dict(op="jobExport"))), 2)

    def test_multipart_import_and_session_export(self):
        boundary = "HPC-test-boundary-12345"
        payload = json.dumps(fixture().session()).encode()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"options\"\r\n\r\n"
                '{"replace":true,"settings":{"offset":540}}\r\n'
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"sample.json\"\r\n"
                "Content-Type: application/json\r\n\r\n").encode() + payload + f"\r\n--{boundary}--\r\n".encode()
        req = Request(self.url + "/api/import", body, {"Content-Type": "multipart/form-data; boundary=" + boundary,
                                                     "X-HPC-Token": self.server.monitor_token})
        with urlopen(req, timeout=15) as r:
            self.assertEqual(json.load(r)["data"]["meta"]["counts"]["node_cpu"], 6)
        saved = json.loads(self.post(dict(op="session", settings={})))
        self.assertEqual(saved["format"], "hpc-monitor-session")
        self.assertEqual(len(saved["tables"]["node_cpu"]), 6)

    def test_invalid_json_does_not_crash_server(self):
        req = Request(self.url + "/api/command", b"{", {"X-HPC-Token": self.server.monitor_token})
        with self.assertRaises(HTTPError) as cm:
            urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        self.assertIn("counts", self.post(dict(op="meta")))


class PlotTests(unittest.TestCase):
    def test_every_native_chart_renders_without_x11(self):
        try:
            import matplotlib
        except ImportError:
            self.skipTest("Matplotlib is needed only for the X11 GUI")
        from hpc_monitor_plot import VIEWS, figure_for
        d = c.analyze(c.demo_store())
        for view, (_label, plots) in VIEWS.items():
            for plot in plots:
                with self.subTest(plot=plot):
                    figure = figure_for(d, view, plot, dark=plot.endswith("heat"))
                    buf = io.BytesIO()
                    figure.savefig(buf, format="png", dpi=70)
                    self.assertTrue(buf.getvalue().startswith(b"\x89PNG"))
                    figure.clear()


if __name__ == "__main__":
    unittest.main()
