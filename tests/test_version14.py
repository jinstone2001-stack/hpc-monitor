"""Calendar boundaries, chart scaling, large-input transport and cache regressions."""
import gzip
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from urllib.request import Request, urlopen

import hpc_monitor_core as c
from HPC_Monitor_Web import MonitorSession, make_server, multipart_files
from test_monitor import fixture, html_results, load, node


def calendar_store(nodes=1):
    store = c.LogStore()
    base = datetime(2026, 11, 11)
    rows = [dict(timestamp=(base + timedelta(minutes=30 * i)).isoformat(), node=f"g4-{n:02}",
                 partitions="g4", state="MIXED", cpu_total=100, cpu_alloc=80)
            for i in range(97) for n in range(nodes)]
    load(store, "node_cpu", rows)
    return store


class Version14Tests(unittest.TestCase):
    def test_calendar_range_includes_the_entire_end_date_and_leap_day(self):
        first, last = c.date_range("2026-11-11", "2026-11-12")
        self.assertEqual(first, c.parse_time("2026-11-11T00:00:00"))
        self.assertEqual(last, c.parse_time("2026-11-13T00:00:00"))
        self.assertEqual(last - first, 2 * c.DAY)
        self.assertEqual(c.date_range("2024-02-29", "2024-02-29", 0)[1], c.parse_time("2024-03-01T00:00:00Z"))
        self.assertEqual(c.date_range("2026-12-31", "2026-12-31")[1], c.parse_time("2027-01-01T00:00:00"))
        for dates in (("2026-02-29", "2026-03-01"), ("2026-11-12", "2026-11-11"), ("", "")):
            with self.subTest(dates=dates), self.assertRaises(ValueError):
                c.date_range(*dates)

    def test_one_day_filter_keeps_cross_boundary_intervals_and_last_day_jobs(self):
        store = c.LogStore()
        load(store, "node_cpu", [dict(node("23:45"), timestamp="2026-11-10T23:45:00"),
                                dict(node("00:15"), timestamp="2026-11-11T00:15:00"),
                                dict(node("00:45"), timestamp="2026-11-11T00:45:00")])
        data = c.analyze(store, dict(zip(("from", "to"), c.date_range("2026-11-11", "2026-11-11"))))
        self.assertEqual(data["summary"]["coreHours"], 12)
        self.assertEqual(data["summary"]["allocPct"], 25)
        self.assertEqual(data["summary"]["coverage"], 45 / 1440 * 100)
        a, b = c.date_range("2026-11-11", "2026-11-12")
        data = c.analyze(calendar_store(), {"from": a, "to": b})
        self.assertEqual(data["summary"]["coreHours"], 80 * 48)
        self.assertEqual(data["summary"]["coverage"], 100)

    def test_scaled_pies_fill_at_maximum_but_keep_actual_labels_and_csv(self):
        try:
            from hpc_monitor_plot import figure_for
            import matplotlib
        except ImportError:
            self.skipTest("Matplotlib is optional")
        data = c.analyze(calendar_store(), {"pieMax": 80})
        for view, plot in (("overview", "usage_pie"), ("nodes", "node_pies"), ("groups", "group_pies")):
            for maximum, expected_angle in ((100, 288), (80, 360), (70, 360)):
                with self.subTest(view=view, maximum=maximum):
                    fig = figure_for(data, view, plot, pie_max=maximum)
                    ax = fig.axes[0]
                    self.assertAlmostEqual(ax.patches[0].theta2 - ax.patches[0].theta1, expected_angle, places=4)
                    self.assertIn("80.0%", [t.get_text() for t in ax.texts])
                    fig.clear()
        self.assertEqual(data["groups"][0]["allocPct"], 80)
        self.assertIn("80", c.table_csv(data["groups"], [("allocPct", "Actual %")]))

    def test_many_pies_are_pageable_and_all_nodes_remain_in_statistics(self):
        try:
            from hpc_monitor_plot import figure_for
        except ImportError:
            self.skipTest("Matplotlib is optional")
        data = c.analyze(calendar_store(nodes=25))
        first = figure_for(data, "nodes", "node_pies")
        last = figure_for(data, "nodes", "node_pies", pie_page=1)
        self.assertEqual((len(first.axes), len(last.axes)), (24, 1))
        self.assertIn("g4-24", last.axes[0].get_title())
        self.assertEqual(len(data["nodes"]), 25)
        first.clear()
        last.clear()

    def test_cached_lists_are_not_mutable_shared_results(self):
        c.parse_list("1,2").append("3")
        self.assertEqual(c.parse_list("1,2"), ["1", "2"])
        self.assertNotEqual(c.parse_time("2026-11-11T00:00:00", 0), c.parse_time("2026-11-11T00:00:00", 540))

    def test_duplicate_input_reuses_time_index_and_conflicts_invalidate_it(self):
        store = fixture()
        index = store.normalized()
        load(store, "node_cpu", [node("00:00")])
        self.assertIs(store.normalized(), index)
        load(store, "node_cpu", [node("00:00", alloc=24)])
        self.assertIsNot(store.normalized(), index)
        self.assertEqual(c.analyze(store)["summary"]["coreHours"], 60)

    def test_session_cache_tracks_data_identity_and_ignores_only_presentation(self):
        session = MonitorSession(fixture())
        with patch("HPC_Monitor_Web.analyze", wraps=c.analyze) as analyze:
            first = session.command(dict(op="analyze", options={}))
            scaled = session.command(dict(op="analyze", options={"pieMax": 80}))
            self.assertEqual(analyze.call_count, 1)
            self.assertEqual(first["summary"], scaled["summary"])
            self.assertEqual(scaled["settings"]["pieMax"], 80)
            load(session.store, "node_cpu", [node("00:00", alloc=24)])
            session.command(dict(op="analyze", options={}))
            self.assertEqual(analyze.call_count, 2)
            session.store = calendar_store()
            session.command(dict(op="analyze", options={}))
            self.assertEqual(analyze.call_count, 3)
        copied = c.LogStore()
        copied.load_session(session.store.session({"pieMax": 70, "rangeMode": "times"}))
        self.assertEqual(copied.imported_settings["pieMax"], 70)
        self.assertEqual(copied.imported_settings["rangeMode"], "times")

    def test_multipart_preserves_payload_newlines_quotes_and_boundary_like_text(self):
        payload = b'a\tb\r\n"quoted"\tdata\r\n--test-boundary-suffix\r\n'
        body = (b'--test-boundary\r\nContent-Disposition: form-data; name="files"; filename="test.tsv"\r\n\r\n'
                + payload + b'\r\n--test-boundary\r\nContent-Disposition: form-data; name="options"\r\n\r\n'
                + b'{"replace":true}\r\n--test-boundary--\r\n')
        files, options = multipart_files(body, 'multipart/form-data; boundary="test-boundary"')
        self.assertEqual(bytes(files[0][1]), payload)
        self.assertIsInstance(files[0][1], memoryview)
        self.assertEqual(options, dict(replace=True))
        for damaged in (body[:-30], body.replace(b"\r\n\r\n", b"\n", 1)):
            with self.assertRaises(ValueError):
                multipart_files(damaged, "multipart/form-data; boundary=test-boundary")

    def test_large_web_responses_are_compressed_for_supported_clients(self):
        server = make_server(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:" + str(server.server_address[1])
            with urlopen(Request(url, headers={"Accept-Encoding": "gzip"})) as response:
                encoded = response.read()
                self.assertEqual(response.headers["Content-Encoding"], "gzip")
                decoded = gzip.decompress(encoded)
                self.assertIn(b"Python Web", decoded)
                self.assertLess(len(encoded), len(decoded) / 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_current_html_calendar_filters_match_python_after_index_optimization(self):
        import shutil
        if not shutil.which("node"):
            self.skipTest("Node.js is optional")
        store = calendar_store(nodes=2)
        # Session JSON explicitly declares its empty tables; compare the same
        # imported session in both engines (known empty differs from missing).
        restored = c.LogStore()
        restored.load_session(store.session())
        options = [{"from": a, "to": b, "groups": ["g4"], "pieMax": 70}
                   for a, b in (c.date_range("2026-11-11", "2026-11-11"),
                                c.date_range("2026-11-12", "2026-11-12"))]
        for opt, js in zip(options, html_results(store, options)):
            py = c.analyze(restored, opt)
            self.assertEqual(py["summary"], js["summary"])
            self.assertEqual(py["series"], js["series"])
            self.assertEqual(py["groups"], js["groups"])


if __name__ == "__main__":
    unittest.main()
