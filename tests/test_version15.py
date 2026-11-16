"""Non-contiguous calendar accounting across weekends, months and midnight."""
import json
import shutil
import unittest
from datetime import datetime, timedelta

import hpc_monitor_core as c
from HPC_Monitor_Web import MonitorSession
from test_monitor import gpu_fixture, html_results, load


def weekend_store():
    store = c.LogStore()
    first = datetime(2026, 10, 30)  # Friday through Monday, across a month boundary.
    rows = {kind: [] for kind in c.TYPES}
    for hour in range(97):
        dt = first + timedelta(hours=hour)
        stamp = dt.isoformat()
        weekend = dt.weekday() >= 5
        cpu = 10 if weekend else 2
        rows["node_cpu"].append(dict(timestamp=stamp, node="n1", partitions="g1", state="MIXED",
                                     cpu_total=10, cpu_alloc=cpu, mem_total_mb=100, mem_alloc_mb=cpu))
        rows["gpu"].append(dict(timestamp=stamp, node="n1", gpu_index="0", gpu_name="Test GPU",
                                gpu_util=90 if weekend else 20, mem_pct=80 if weekend else 10,
                                mem_used_mb=80 if weekend else 10, temp_c=99 if weekend else 30,
                                power_w=100 if weekend else 10))
        if hour == 96:
            continue
        rows["user_cpu"].append(dict(timestamp=stamp, user="alice", node="n1", partition="g1",
                                     cpus_alloc=cpu, mem_alloc_mb=cpu, n_jobs=1, job_ids="100"))
        rows["gpu_user"].append(dict(timestamp=stamp, node="n1", gpu_index="0", user="alice",
                                     job_id="100", pid="1", mem_used_mb=5))
        rows["queue_pending"].append(dict(timestamp=stamp, job_id="P" + dt.date().isoformat(), user="alice",
                                          partition="g1", submit_time=dt.replace(hour=0).isoformat(),
                                          req_cpus=10, wait_sec=hour * 3600, reason="Resources"))
        if hour % 24 == 0:
            rows["queue_started"].append(dict(timestamp=stamp, job_id=str(200 + hour // 24), user="alice",
                                              partition="g1", submit_time=stamp, start_time=stamp,
                                              alloc_cpus=2, n_nodes=1, wait_sec=(1 + hour // 24) * 60))
    rows["queue_started"].append(dict(timestamp=first.isoformat(), job_id="100", user="alice", partition="g1",
                                      submit_time=(first - timedelta(hours=1)).isoformat(), start_time=first.isoformat(),
                                      alloc_cpus=2, n_nodes=1, wait_sec=3600))
    for kind, values in rows.items():
        load(store, kind, values)
    return store


def weekend_options(**updates):
    a, b = c.date_range("2026-10-30", "2026-11-02")
    return dict({"from": a, "to": b, "interval": 60, "startedMode": "event", "dateMode": "exclude",
                 "selectedDates": ["2026-10-31", "2026-11-01"]}, **updates)


class Version15Tests(unittest.TestCase):
    def test_weekends_leave_only_weekday_cpu_gpu_jobs_and_waits(self):
        data = c.analyze(weekend_store(), weekend_options(denominator="calendar"))
        self.assertFalse(data["empty"])
        self.assertEqual(data["selectedSeconds"], 48 * 3600)
        self.assertEqual(data["summary"]["coreHours"], 96)
        self.assertEqual(data["summary"]["jobHours"], 48)
        self.assertEqual(data["summary"]["allocPct"], 20)
        self.assertEqual(data["summary"]["coverage"], 100)
        self.assertEqual(data["summary"]["started"], 3)
        self.assertEqual(data["summary"]["submitted"], 4)
        self.assertEqual(data["summary"]["waitMean"], 1300)
        self.assertEqual(data["summary"]["pending"], 1)
        self.assertEqual(data["gpu"][0]["energyWh"], 480)
        self.assertEqual(data["gpu"][0]["util"], 20)
        self.assertEqual(data["gpu"][0]["tempMax"], 30)
        self.assertEqual(data["gpu"][0]["memMax"], 10)
        self.assertEqual(data["gpu"][0]["coverage"], 100)
        self.assertEqual(data["users"][0]["gpuHours"], 48)
        self.assertEqual([c.iso(x["t"])[:10] for x in data["series"]], ["2026-10-30", "2026-11-02"])
        self.assertEqual([c.iso(x["t"])[:10] for x in data["heat"]], ["2026-10-30", "2026-11-02"])
        self.assertTrue(all(x["coverage"] == 100 for x in data["heat"] + data["gpuHeat"]))
        job = next(j for j in data["jobs"] if j["id"] == "100")
        self.assertEqual(len(job["ranges"]), 2)
        self.assertEqual(job["wallHours"], 48)
        self.assertEqual(c.iso(data["lastNodeTs"]), "2026-11-02T23:00:00")

    def test_include_weekdays_equals_exclude_weekends_for_both_denominators_and_buckets(self):
        store = weekend_store()
        for denominator in ("observed", "calendar"):
            for bucket in ("hour", "day", "week", "month"):
                excluded = c.analyze(store, weekend_options(bucket=bucket, denominator=denominator))
                included = c.analyze(store, weekend_options(bucket=bucket, denominator=denominator,
                                      dateMode="include", selectedDates=["2026-10-30", "2026-11-02"]))
                with self.subTest(denominator=denominator, bucket=bucket):
                    for key in ("summary", "series", "users", "nodes", "groups", "gpu", "jobs", "heat", "gpuHeat", "latestGpu", "pendingRows"):
                        self.assertEqual(excluded[key], included[key], key)
                    self.assertTrue(all(x["coverage"] == 100 for x in included["heat"] + included["gpuHeat"]))

    def test_no_selected_or_no_remaining_days_returns_clear_empty_result(self):
        store = weekend_store()
        for opt in (weekend_options(dateMode="include", selectedDates=[]),
                    weekend_options(dateMode="include", selectedDates=["2027-01-01"]),
                    weekend_options(selectedDates=["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"])):
            with self.subTest(opt=opt):
                data = c.analyze(store, opt)
                self.assertTrue(data["empty"])
                self.assertIn("집계할 날짜가 없습니다", data["error"])
        self.assertEqual(c.analyze(store, weekend_options(selectedDates=[]))["selectedSeconds"], 96 * 3600)
        self.assertEqual(c.analyze(store, weekend_options(dateMode="all"))["summary"]["coreHours"], 576)

    def test_midnight_boundary_keeps_only_overlap_including_preceding_snapshot(self):
        store = c.LogStore()
        load(store, "node_cpu", [dict(timestamp=t, node="n1", partitions="g1", cpu_total=10, cpu_alloc=10)
                                for t in ("2026-10-31T23:45:00", "2026-11-01T00:15:00")])
        opt = dict(dateMode="include", selectedDates=["2026-11-01"], denominator="calendar",
                   **{"from": c.parse_time("2026-10-31T23:50:00"), "to": c.parse_time("2026-11-01T00:10:00")})
        data = c.analyze(store, opt)
        self.assertEqual(data["selectedSeconds"], 600)
        self.assertAlmostEqual(data["summary"]["coreHours"], 10 / 6)
        self.assertEqual(data["summary"]["coverage"], 100)
        self.assertEqual(data["summary"]["allocPct"], 100)

    def test_date_normalization_leap_day_year_boundary_and_timezone(self):
        opt = c.options_from(dict(dateMode="include", selectedDates=["2024-02-29", "2024-02-29", "2024-02-30", "oops", None]))
        self.assertEqual(opt["selectedDates"], ["2024-02-29"])
        for offset in (0, 540):
            opt["offset"] = offset
            a, b = c.date_range("2024-02-28", "2024-03-01", offset)
            self.assertEqual(c.selected_date_windows(a, b, opt), [list(c.date_range("2024-02-29", "2024-02-29", offset))])
            a, b = c.date_range("2026-12-31", "2027-01-01", offset)
            opt["selectedDates"] = ["2027-01-01"]
            self.assertEqual(c.selected_date_windows(a, b, opt), [list(c.date_range("2027-01-01", "2027-01-01", offset))])
            opt["selectedDates"] = ["2024-02-29"]

    def test_single_gpu_snapshot_respects_selected_date_without_duration(self):
        store = gpu_fixture(single=True)
        day = c.iso(store.describe()["min"])[:10]
        included = c.analyze(store, dict(dateMode="include", selectedDates=[day]))
        excluded = c.analyze(store, dict(dateMode="exclude", selectedDates=[day]))
        self.assertFalse(included["empty"])
        self.assertEqual(included["selectedSeconds"], 0)
        self.assertIsNone(included["gpu"][0]["energyWh"])
        self.assertTrue(excluded["empty"])

    def test_server_cache_csv_and_session_restore_calendar_selection(self):
        store, opt = weekend_store(), weekend_options(denominator="calendar")
        session = MonitorSession(store)
        data = session.command(dict(op="analyze", options=opt))
        original = session.last
        weekend = session.command(dict(op="analyze", options=weekend_options(dateMode="include")))
        self.assertIsNot(session.last, original)
        self.assertEqual(weekend["summary"]["coreHours"], 480)
        self.assertEqual(data["summary"]["coreHours"], 96)
        self.assertIn("96", c.table_csv(data["users"], [("name", "사용자"), ("coreHours", "core·h")]))
        restored = c.LogStore()
        restored.load_session(json.loads(json.dumps(store.session(opt))))
        self.assertEqual(restored.imported_settings["selectedDates"], opt["selectedDates"])
        self.assertEqual(c.analyze(restored, restored.imported_settings)["summary"], data["summary"])
        old_session = store.session()
        old_session["settings"] = {k: v for k, v in old_session["settings"].items() if k not in ("dateMode", "selectedDates")}
        restored.load_session(old_session)
        self.assertEqual(restored.imported_settings["dateMode"], "all")

    @unittest.skipUnless(shutil.which("node"), "Node.js is optional")
    def test_python_and_shipped_html_match_for_disjoint_dates_and_empty_selection(self):
        store = weekend_store()
        restored = c.LogStore()
        restored.load_session(store.session())
        options = [weekend_options(bucket=bucket, denominator=denominator, dateMode=mode, selectedDates=dates)
                   for bucket in ("hour", "day", "week", "month")
                   for denominator in ("observed", "calendar")
                   for mode, dates in (("exclude", ["2026-10-31", "2026-11-01"]),
                                       ("include", ["2026-10-30", "2026-11-02"]),
                                       ("include", []))]
        for opt, js in zip(options, html_results(store, options)):
            py = c.analyze(restored, opt)
            with self.subTest(opt=opt):
                self.assertEqual(py["empty"], js["empty"])
                if py["empty"]:
                    self.assertEqual(py["error"], js["error"])
                    continue
                for key in ("summary", "series", "heat", "gpuHeat", "gpu", "selectedSeconds", "lastQueue", "lastNodeTs"):
                    self.assertEqual(py[key], js[key], key)
                for table in ("nodes", "groups", "users", "latestGpu", "jobs"):
                    by_key = {str(r.get("id", r.get("name"))): r for r in js[table]}
                    for row in py[table]:
                        other = by_key[str(row.get("id", row.get("name")))]
                        for key, value in row.items():
                            if key not in ("key", "nodes", "source", "groups"):
                                self.assertEqual(value, other[key], (table, key))


if __name__ == "__main__":
    unittest.main()
