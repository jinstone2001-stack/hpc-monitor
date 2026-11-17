"""GPU ingestion, independent device clocks and portable presentation settings."""
import io
import json
import shutil
import unittest
from unittest.mock import patch

import hpc_monitor_core as c
from HPC_Monitor_Web import MonitorSession
from HPC_Monitor_X11 import parse_mapping
from test_monitor import fixture, gpu_fixture, html_results, load, stamp


def staggered_gpus():
    store = c.LogStore()
    for index, shift, util in (("0", 0, 20), ("1", 5, 80)):
        load(store, "gpu", [dict(timestamp=f"2026-11-10T{hour}:00:{shift:02d}", node="g1", gpu_index=index,
                                 gpu_util=util, mem_used_mb=25, mem_total_mb=100, power_w=100)
                            for hour in ("00", "01")])
    return store


class Version16Tests(unittest.TestCase):
    def test_gpu_header_does_not_require_optional_model_or_utilization(self):
        for metric, value in (("gpu_util", 75), ("temp_c", 55), ("power_w", 120), ("mem_used_mb", 25)):
            with self.subTest(metric=metric):
                store = c.LogStore()
                store.load_text(f"timestamp\tnode\tgpu_index\t{metric}\n2026-11-10T00:00:00\tg1\t0\t{value}\n")
                data = c.analyze(store)
                self.assertFalse(data["empty"])
                self.assertEqual(len(data["latestGpu"]), 1)
                self.assertEqual(store.issue_total, 0)
        self.assertIsNone(c.detect_header(["timestamp", "gpu_index", "gpu_util"]))

    def test_gpu_memory_percent_is_derived_only_from_known_positive_capacity(self):
        store = c.LogStore()
        load(store, "gpu", [dict(timestamp=stamp("00:00"), node="g1", gpu_index=str(i),
                                 mem_used_mb=25, mem_total_mb=total) for i, total in enumerate((100, 0, None))])
        data = c.analyze(store)
        self.assertEqual([r["memPct"] for r in data["latestGpu"]], [25, None, None])
        self.assertTrue(all(r["util"] is None for r in data["latestGpu"]))

    def test_process_only_snapshot_is_visible_without_inventing_gpu_hours(self):
        store = c.LogStore()
        load(store, "gpu_user", [dict(timestamp=stamp("00:30"), node="g1", gpu_index="0", user="alice",
                                      pid=str(pid), mem_used_mb=mem) for pid, mem in ((1, 200), (2, 300))])
        data = c.analyze(store)
        self.assertFalse(data["empty"])
        self.assertTrue(data["snapshotOnly"])
        self.assertEqual(data["latestGpuUsers"], [dict(name="alice", processes=2, memUsedMb=500, t=c.parse_time(stamp("00:30")))])
        self.assertIsNone(data["users"][0]["gpuHours"])
        self.assertIsNone(data["users"][0]["gpuMemHours"])
        self.assertEqual(data["gpu"], [])

    def test_device_clocks_are_independent_and_final_records_are_not_extended(self):
        data = c.analyze(staggered_gpus(), dict(interval=60))
        self.assertEqual([r["util"] for r in data["gpu"]], [20, 80])
        self.assertEqual([r["energyWh"] for r in data["gpu"]], [100, 100])
        clipped = c.analyze(staggered_gpus(), {"from": c.parse_time(stamp("00:15")), "to": c.parse_time(stamp("00:45")), "interval": 60})
        self.assertEqual([r["energyWh"] for r in clipped["gpu"]], [50, 50])
        self.assertTrue(all(r["coverage"] == 100 for r in clipped["gpu"]))

    def test_last_process_snapshot_keeps_distinct_ids(self):
        store = c.LogStore()
        load(store, "gpu_user", [dict(timestamp=stamp(hh), node="g1", gpu_index="0", user=user,
                                      pid=pid, mem_used_mb=mem)
                                for hh, user, pid, mem in (("00:00", "alice", "1", 999), ("00:30", "bob", "2", 0))])
        data = c.analyze(store, dict(userNames=dict(alice="Same", bob="Same")))
        self.assertEqual(data["latestGpuUsers"][0]["name"], "bob")
        self.assertEqual(data["latestGpuUsers"][0]["memUsedMb"], 0)
        self.assertEqual({r["name"] for r in data["users"]}, {"alice", "bob"})

    def test_presentation_validation_rejects_bad_types_and_colors(self):
        raw = dict(chartStyle=dict(pieColumns=0, piesPerPage=9999, exportScale=True, pieSize="180",
                                   fontSize=float("inf"), chartColor="url(https://bad.invalid)"),
                   userNames=dict(alice="  김연구  ", bob=123, empty=" "), userColors=dict(alice="#123abc", bob="red"))
        opt = c.options_from(raw)
        self.assertEqual(opt["userNames"], dict(alice="김연구"))
        self.assertEqual(opt["userColors"], dict(alice="#123abc"))
        self.assertEqual(opt["chartStyle"]["pieSize"], 180)
        for key in ("pieColumns", "piesPerPage", "exportScale", "fontSize", "chartColor"):
            self.assertEqual(opt["chartStyle"][key], c.appearance_options()["chartStyle"][key])
        self.assertEqual(c.options_from([])["userNames"], {})

    def test_mapping_text_has_clear_validation_and_keeps_equals_in_names(self):
        self.assertEqual(parse_mapping("alice=Kim=Lab\n\nbob=이름"), dict(alice="Kim=Lab", bob="이름"))
        for text in ("missing separator", "=name", "a=one\na=two"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_mapping(text)
        with self.assertRaises(ValueError):
            parse_mapping("alice=red", colors=True)

    def test_session_preserves_names_colors_layout_and_original_ids(self):
        settings = dict(userNames=dict(alice="김연구"), userColors=dict(alice="#123456"),
                        chartStyle=dict(pieColumns=2, pieSize=200, piesPerPage=12, exportScale=3))
        store, copied = fixture(), c.LogStore()
        copied.load_session(json.loads(json.dumps(store.session(settings))))
        self.assertEqual(copied.imported_settings, c.options_from(settings))
        self.assertEqual(copied.tables, store.tables)
        original = c.analyze(store)
        renamed = c.analyze(copied, copied.imported_settings)
        for key in ("summary", "users", "jobs", "gpu"):
            self.assertEqual(original[key], renamed[key])

    def test_presentation_changes_reuse_web_accounting(self):
        session = MonitorSession(fixture())
        with patch("HPC_Monitor_Web.analyze", wraps=c.analyze) as analyze:
            first = session.command(dict(op="analyze", options={}))
            changed = session.command(dict(op="analyze", options=dict(userNames=dict(alice="Kim"),
                                      userColors=dict(alice="#123456"), chartStyle=dict(pieColumns=2))))
            self.assertEqual(analyze.call_count, 1)
            self.assertEqual(first["summary"], changed["summary"])
            self.assertEqual(changed["settings"]["userNames"], dict(alice="Kim"))

    def test_json_roundtrip_preserves_missing_and_header_only_log_types(self):
        store = fixture()
        restored = c.LogStore()
        restored.load_session(store.session())
        self.assertEqual(c.analyze(restored)["summary"]["pending"], None)
        store.load_text("\t".join(c.SCHEMA["queue_pending"]) + "\n")
        restored = c.LogStore()
        restored.load_session(store.session())
        self.assertEqual(c.analyze(restored)["summary"]["pending"], 0)

    @unittest.skipUnless(shutil.which("node"), "Node.js is optional")
    def test_html_and_python_gpu_and_settings_match(self):
        stores = [staggered_gpus(), gpu_fixture(single=True)]
        process = c.LogStore()
        load(process, "gpu_user", [dict(timestamp=stamp("00:30"), node="g1", gpu_index="0", user="alice", pid="1", mem_used_mb=0)])
        stores.append(process)
        options = [dict(interval=60), dict(users=["alice"]), dict(userNames=dict(alice="김연구"),
                   userColors=dict(alice="#123456"), chartStyle=dict(pieColumns=2, pieSize=180, exportScale=True))]
        for store in stores:
            for opt, html in zip(options, html_results(store, options)):
                data = c.analyze(store, opt)
                with self.subTest(counts=store.describe()["counts"], options=opt):
                    for key in ("chartStyle", "userNames", "userColors"):
                        self.assertEqual(data["settings"][key], html["settings"][key], key)
                    for key in ("gpu", "latestGpu", "latestGpuUsers", "gpuHeat", "users", "summary"):
                        self.assertEqual(data[key], html[key], key)

    def test_native_labels_colors_fonts_and_duplicate_names_are_independent(self):
        from hpc_monitor_plot import figure_for
        from matplotlib.colors import to_hex
        data = c.analyze(c.demo_store(), dict(userNames=dict(kim="Same", lee="Same"),
                         userColors=dict(kim="#123456"), chartStyle=dict(chartWidth=900, chartHeight=500, fontSize=14, titleSize=20)))
        figure = figure_for(data, "users", "user_core")
        axis = figure.axes[0]
        self.assertEqual(tuple(figure.get_size_inches()), (9, 5))
        self.assertEqual([t.get_text() for t in axis.get_yticklabels()].count("Same"), 2)
        self.assertEqual(len({bar.get_y() for bar in axis.patches}), len(data["users"]))
        self.assertEqual(to_hex(next(p.get_facecolor() for p in axis.patches if p.hpc_user == "kim")), "#123456")
        self.assertTrue(all(t.get_fontsize() == 14 for t in axis.get_yticklabels()))
        figure.clear()

    def test_native_pie_columns_holes_pagination_and_png_dimensions(self):
        from hpc_monitor_plot import figure_for
        from test_version14 import calendar_store
        data = c.analyze(calendar_store(nodes=5))
        settings = dict(chartStyle=dict(pieColumns=2, piesPerPage=3, pieHole=40, chartWidth=800, chartHeight=500))
        figure = figure_for(data, "nodes", "node_pies", settings=settings, pie_page=0)
        self.assertEqual(len(figure.axes), 3)
        self.assertAlmostEqual(figure.axes[0].patches[0].width, .6)
        buf = io.BytesIO()
        figure.savefig(buf, format="png", dpi=100)
        self.assertTrue(buf.getvalue().startswith(b"\x89PNG"))
        self.assertEqual(figure.axes[2].get_subplotspec().colspan.start, 0)
        figure.clear()
        large = figure_for(c.analyze(calendar_store(nodes=12)), "nodes", "node_pies", settings=dict(chartStyle=dict(
                           chartWidth=4000, chartHeight=4000, pieColumns=1, piesPerPage=120, pieSize=400, pieGap=200, titleSize=48, fontSize=32)))
        self.assertLess(large.dpi, 100)
        self.assertLessEqual(large.bbox.width * large.bbox.height, 32_000_001)
        self.assertLessEqual(max(large.bbox.width, large.bbox.height), 16384)
        large.clear()


if __name__ == "__main__":
    unittest.main()
