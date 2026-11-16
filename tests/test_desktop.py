"""Real Tk widget checks. Run with DISPLAY set; optional on headless systems."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import hpc_monitor_core as c
from HPC_Monitor_X11 import CalendarPicker, LogFilePicker, MonitorGUI
from test_version14 import calendar_store


@unittest.skipUnless(os.environ.get("DISPLAY"), "A desktop or X11 display is needed for Tk widget checks")
class DesktopTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.callback_errors = []
        self.root.report_callback_exception = lambda _kind, value, _trace: self.callback_errors.append(str(value))
        self.root.geometry("1280x880+0+0")

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass
        self.assertEqual(self.callback_errors, [], "Tk callback errors")

    def wait(self, condition):
        limit = time.monotonic() + 15
        while not condition() and time.monotonic() < limit:
            self.root.update()
            time.sleep(.02)
        self.assertTrue(condition(), "The GUI did not complete its background task")
        self.root.update()

    def test_file_picker_ctrl_a_is_local_to_file_list_or_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "subfolder").mkdir()
            for name in ("a.tsv", "b.tsv", "c.json"):
                (root / name).touch()
            picker = LogFilePicker(self.root, temporary)
            self.root.update()
            picker.tree.focus_force()
            picker.tree.event_generate("<Control-a>")
            self.root.update()
            selected = [picker.entries[key] for key in picker.tree.selection()]
            self.assertEqual(len(selected), 3)
            self.assertFalse(any(row["directory"] for row in selected))
            picker.entry.focus_force()
            picker.entry.event_generate("<Control-a>")
            self.root.update()
            self.assertEqual(picker.entry.selection_get(), temporary)
            self.assertEqual(len(picker.tree.selection()), 3)
            picker.dialog.destroy()

    def test_calendar_selection_applies_inclusive_dates_to_graphs_and_statistics(self):
        app = MonitorGUI(self.root, calendar_store(), SimpleNamespace(font_size=10))
        try:
            self.wait(lambda: not app.busy)
            picker = CalendarPicker(self.root, app.date_from)
            picker.choose(11)
            picker = CalendarPicker(self.root, app.date_to)
            picker.choose(12)
            app.apply_filters()
            self.wait(lambda: not app.busy)
            self.assertEqual((app.data["from"], app.data["to"]), c.date_range("2026-11-11", "2026-11-12"))
            self.assertEqual(app.data["summary"]["coreHours"], 3840)
            app.range_mode.set("시각 · 종료 미포함")
            app.change_range_mode()
            self.assertEqual(app.to_var.get(), "2026-11-13T00:00:00")
            app.range_mode.set("날짜 · 종료일 포함")
            app.change_range_mode()
            self.assertEqual(app.date_to.get(), "2026-11-12")
            app.switch_view("groups")
            app.pie_max.set("80")
            app.apply_pie_max()
            self.root.update()
            ax = app.figure.axes[0]
            self.assertAlmostEqual(ax.patches[0].theta2 - ax.patches[0].theta1, 360)
            self.assertIn("80.0%", [text.get_text() for text in ax.texts])
            self.assertEqual(app.data["groups"][0]["allocPct"], 80)
            # A theme or chart switch must retain the selected dates and scale.
            app.dark_var.set(True)
            app.toggle_theme()
            self.assertEqual(app.data["settings"]["pieMax"], 80)
        finally:
            app.closed = True
            app.executor.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    unittest.main()
