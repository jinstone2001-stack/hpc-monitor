"""Real Tk widget checks. Run with DISPLAY set; optional on headless systems."""
import os
import gc
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import hpc_monitor_core as c
from HPC_Monitor_X11 import CalendarPicker, DATE_MODES, LogFilePicker, MonitorGUI, MultiDatePicker
from test_version14 import calendar_store
from test_version15 import weekend_store
from datetime import date


@unittest.skipUnless(os.environ.get("DISPLAY"), "A desktop or X11 display is needed for Tk widget checks")
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        # One interpreter keeps Tk/Matplotlib callbacks and X focus stable when
        # the complete suite creates and closes several native applications.
        cls.desktop = tk.Tk()

    @classmethod
    def tearDownClass(cls):
        cls.desktop.destroy()

    def setUp(self):
        self.root = self.desktop
        self.callback_errors = []
        self.root.report_callback_exception = lambda _kind, value, _trace: self.callback_errors.append(str(value))
        self.root.geometry("1280x880+0+0")

    def tearDown(self):
        try:
            for event in self.root.tk.call("after", "info"):
                self.root.after_cancel(event)
            for child in self.root.winfo_children():
                child.destroy()
            gc.collect()
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
            self.wait(lambda: self.root.focus_get() == picker.tree)
            picker.tree.event_generate("<Control-a>")
            self.root.update()
            selected = [picker.entries[key] for key in picker.tree.selection()]
            self.assertEqual(len(selected), 3)
            self.assertFalse(any(row["directory"] for row in selected))
            picker.entry.focus_force()
            self.wait(lambda: self.root.focus_get() == picker.entry)
            picker.entry.event_generate("<Control-a>")
            self.root.update()
            self.assertTrue(picker.entry.selection_present())
            self.assertEqual(picker.entry.index("sel.first"), 0)
            self.assertEqual(picker.entry.index("sel.last"), len(temporary))
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

    def test_multiple_dates_toggle_across_months_and_commit_or_cancel(self):
        committed = []
        first, last = date(2026, 10, 30), date(2026, 11, 2)
        picker = MultiDatePicker(self.root, [], "exclude", first, last, lambda *args: committed.append(args))
        picker.day_buttons[30].invoke()
        picker.move(1)
        picker.day_buttons[1].invoke()
        self.assertEqual(picker.chosen, {"2026-10-30", "2026-11-01"})
        picker.day_buttons[1].invoke()
        picker.clear()
        picker.weekends()
        self.assertEqual(picker.chosen, {"2026-10-31", "2026-11-01"})
        picker.dialog.destroy()
        self.assertEqual(committed, [])
        picker = MultiDatePicker(self.root, ["2026-10-31"], "include", first, last, lambda *args: committed.append(args))
        picker.commit()
        self.assertEqual(committed, [("include", ["2026-10-31"])])

    def test_multiple_date_filter_applies_to_native_statistics_and_resets(self):
        app = MonitorGUI(self.root, weekend_store(), SimpleNamespace(font_size=10))
        try:
            self.wait(lambda: not app.busy)
            app.options["interval"] = 60
            app.date_from.set("2026-10-30")
            app.date_to.set("2026-11-02")
            app.choose_dates()
            app.date_picker.weekends()
            app.date_picker.commit()
            self.wait(lambda: not app.busy)
            self.assertEqual(app.data["summary"]["coreHours"], 96)
            self.assertEqual(app.data["selectedSeconds"], 48 * 3600)
            self.assertIn("클릭한 날짜 제외", app.caption.cget("text"))
            self.assertEqual(app.date_mode.get(), DATE_MODES["exclude"])
            app.date_mode.set(DATE_MODES["include"])
            app.apply_filters()
            self.wait(lambda: not app.busy)
            self.assertEqual(app.data["summary"]["coreHours"], 480)
            app.reset_filters()
            self.wait(lambda: not app.busy)
            self.assertEqual(app.options["selectedDates"], [])
            self.assertEqual(app.data["summary"]["coreHours"], 576)
        finally:
            app.closed = True
            app.executor.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    unittest.main()
