#!/usr/bin/env python3
"""Native Tkinter/Matplotlib X11 GUI using the shared Python engine."""
from __future__ import annotations

import argparse
import calendar
import io
import json
import math
import queue
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import date, timedelta

from hpc_monitor_core import (DEFAULTS, PRESENTATION_KEYS, STYLE_NUMBERS, STYLE_COLORS, VERSION,
                              LogStore, analyze, appearance_options, options_from, date_range, demo_store, iso, parse_time, table_csv)
from hpc_monitor_plot import PLOT_NAMES, VIEWS, figure_for
from hpc_monitor_files import directory_listing
from hpc_monitor_help import help_text

PREFERENCES = Path.home() / ".hpc-monitor-settings.json"
STYLE_LABELS = dict(chartWidth="캔버스 너비 (px)", chartHeight="캔버스 높이 (px)", pieColumns="한 줄의 원 개수",
                    piesPerPage="페이지당 원 개수", pieSize="원 지름 (px)", pieGap="원 사이 여백 (px)",
                    pieHole="원 내부 구멍 (%)", fontSize="일반 글씨 크기", titleSize="제목 글씨 크기",
                    valueSize="원 수치 글씨 크기", exportScale="PNG 출력 배율", chartColor="주 그래프 색상",
                    secondaryColor="보조 그래프 색상", warningColor="최대·대기 색상", pieLowColor="원·지도 낮은 사용률 색",
                    pieHighColor="원·지도 높은 사용률 색", pieEmptyColor="원 빈 영역 색")


def parse_mapping(text, colors=False):
    result = {}
    for index, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        uid, separator, value = line.partition("=")
        uid, value = uid.strip(), value.strip()
        if not separator or not uid or uid in result:
            raise ValueError(f"{index}행을 ID=값 형식으로 입력하세요. 중복 ID는 한 번만 입력하세요.")
        if colors and appearance_options(dict(userColors={uid: value}))["userColors"].get(uid) != value:
            raise ValueError(f"{index}행의 색상을 #RRGGBB로 입력하세요.")
        if value:
            result[uid] = value
    return result

TABLES = {
    "overview": [("t", "구간 시작", "time"), ("submitted", "제출", "num"), ("started", "시작", "num"), ("running", "실행", "num"), ("jobHours", "작업 시간 h", "num"), ("coreHours", "코어 시간 core·h", "num"), ("allocPct", "코어 할당률", "pct"), ("busyPct", "작업 점유율", "pct"), ("gpuUtil", "GPU 사용률", "pct")],
    "nodes": [("name", "노드"), ("groups", "partition"), ("state", "마지막 state"), ("cores", "전체 코어", "num"), ("jobs", "작업 수", "num"), ("jobHours", "작업 시간 h", "num"), ("coreHours", "코어 시간 core·h", "num"), ("busyHours", "점유 시간 h", "num"), ("busyPct", "작업 점유율", "pct"), ("allocPct", "코어 할당률", "pct"), ("coverage", "관측률", "pct"), ("memPct", "메모리 할당률", "pct"), ("loadPct", "CPU load 비율", "pct")],
    "groups": [("name", "partition"), ("nodeCount", "노드 수", "num"), ("jobs", "작업 수", "num"), ("jobHours", "작업 시간 h", "num"), ("coreHours", "코어 시간 core·h", "num"), ("busyPct", "작업 점유율", "pct"), ("allocPct", "코어 할당률", "pct"), ("coverage", "관측률", "pct")],
    "users": [("name", "사용자"), ("jobs", "작업 수", "num"), ("submitted", "제출", "num"), ("started", "시작", "num"), ("jobHours", "작업 시간 h", "num"), ("coreHours", "코어 시간 core·h", "num"), ("contribution", "전체 코어 중 기여율", "pct"), ("activityPct", "활동시간 비율", "pct"), ("gpuHours", "GPU 점유 GPU·h", "num"), ("gpuMemHours", "GPU 메모리 MB·h", "num")],
    "queue": [("id", "작업 ID"), ("user", "사용자"), ("partition", "partition"), ("nodes", "노드"), ("submit", "제출 시각", "time"), ("start", "시작 시각", "time"), ("end", "종료 추정", "time"), ("wallHours", "작업 시간 h", "num"), ("waitMin", "시작 전 대기 min", "num"), ("status", "관측 상태"), ("state", "원본 state")],
    "gpu": [("id", "GPU"), ("name", "모델"), ("util", "평균 사용률", "pct"), ("memPct", "평균 메모리 점유율", "pct"), ("tempMax", "최대 온도 °C", "num"), ("power", "평균 전력 W", "num"), ("energyWh", "추정 에너지 Wh", "num"), ("coverage", "관측률", "pct"), ("users", "관측 사용자")],
    "data": [("name", "파일명"), ("types", "로그 종류"), ("rows", "새 기록", "num"), ("duplicates", "중복", "num"), ("invalid", "제외한 행", "num")],
}
DATA_KEYS = dict(overview="series", nodes="nodes", groups="groups", users="users", queue="jobs", gpu="gpu", gpu_latest="latestGpu")
DATA_KEYS["gpu_users_latest"] = "latestGpuUsers"
TABLES["gpu_users_latest"] = [("name", "사용자"), ("t", "마지막 프로세스 관측", "time"),
                              ("memUsedMb", "관측 메모리 MB", "num"), ("processes", "관측 프로세스", "num")]
TABLES["gpu_latest"] = [("id", "GPU"), ("node", "노드"), ("purpose", "계산 용도"), ("t", "마지막 관측 시각", "time"),
                        ("util", "관측 사용률", "pct"), ("memPct", "관측 메모리 비율", "pct"),
                        ("memUsedMb", "관측 사용 메모리 MB", "num"),
                        ("temp", "관측 온도 °C", "num"), ("power", "관측 전력 W", "num"), ("users", "관측 사용자")]


def fmt(value, kind="", offset=540):
    if value is None:
        return "—"
    if kind == "time":
        return iso(value, offset).replace("T", " ")
    if kind == "minutes":
        value /= 60
    if isinstance(value, (float, int)):
        text = f"{value:,.2f}".rstrip("0").rstrip(".") if not float(value).is_integer() else f"{value:,.0f}"
        return text + ("%" if kind == "pct" else "")
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


for _table in ("overview", "nodes", "groups"):
    TABLES[_table] += [("waitCount", "대기 표본 작업 수", "num"),
                       ("waitMean", "평균 시작 대기 min", "minutes"),
                       ("waitMax", "최대 시작 대기 min", "minutes")]
for _table in ("nodes", "groups", "gpu"):
    TABLES[_table].insert(2, ("purpose", "계산 용도"))


class LogFilePicker:
    def __init__(self, parent, initialdir="."):
        import tkinter as tk
        from tkinter import ttk, messagebox
        self.messagebox = messagebox
        self.result, self.current, self.entries = (), None, {}
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("HPC 로그 파일 선택")
        self.dialog.geometry("820x560")
        self.dialog.minsize(570, 350)
        self.dialog.transient(parent)
        top = ttk.Frame(self.dialog, padding=10)
        top.pack(fill="x")
        self.up = ttk.Button(top, text="↑ 상위 폴더", command=self.go_up)
        self.up.pack(side="left", padx=(0, 8))
        self.path = tk.StringVar(value=str(initialdir))
        self.entry = ttk.Entry(top, textvariable=self.path)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _event: self.navigate(self.path.get()))
        for event in ("<Control-a>", "<Control-A>"):
            self.entry.bind(event, self.focus_path)
        ttk.Button(top, text="이동", command=lambda: self.navigate(self.path.get())).pack(side="left", padx=(8, 0))
        body = ttk.Frame(self.dialog, padding=(10, 0))
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=("kind",), selectmode="extended", show="tree headings")
        self.tree.heading("#0", text="이름 · 폴더를 더블클릭하면 이동")
        self.tree.heading("kind", text="종류")
        self.tree.column("#0", width=570, minwidth=200)
        self.tree.column("kind", width=110, minwidth=90, stretch=False)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for kind, color in (("folder", "#b77917"), ("log", "#4d92d2"), ("session", "#319c84")):
            self.tree.tag_configure(kind, foreground=color)
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.tree.bind(event, self.scroll)
        self.tree.bind("<Double-1>", self.open_selection)
        self.tree.bind("<Return>", self.open_selection)
        self.tree.bind("<BackSpace>", self.go_up)
        for event in ("<Control-a>", "<Control-A>"):
            self.tree.bind(event, self.select_all_files)
        self.dialog.bind("<Alt-Up>", self.go_up)
        self.dialog.bind("<Alt-Left>", self.go_up)
        self.dialog.bind("<Control-l>", self.focus_path)
        self.dialog.bind("<Escape>", lambda _event: self.dialog.destroy())
        bottom = ttk.Frame(self.dialog, padding=10)
        bottom.pack(fill="x")
        ttk.Label(bottom, text="Ctrl+A: 파일 전체 / 주소 전체 선택 · Ctrl/Shift: 여러 파일 선택").pack(side="left")
        ttk.Button(bottom, text="불러오기", command=self.accept).pack(side="right")
        ttk.Button(bottom, text="취소", command=self.dialog.destroy).pack(side="right", padx=8)
        self.navigate(str(initialdir))

    def focus_path(self, _event=None):
        self.entry.focus_set()
        self.entry.selection_range(0, "end")
        return "break"

    def select_all_files(self, _event=None):
        keys = [key for key, row in self.entries.items() if not row["directory"]]
        self.tree.selection_set(keys)
        if keys:
            self.tree.focus(keys[0])
        return "break"

    def navigate(self, path):
        try:
            listing = directory_listing(path, self.current)
        except (OSError, ValueError) as exc:
            self.messagebox.showerror("폴더를 열 수 없습니다", str(exc), parent=self.dialog)
            if self.current:
                self.path.set(self.current)
            return
        self.current, self.parent = listing["path"], listing["parent"]
        self.path.set(self.current)
        self.up.configure(state="disabled" if self.current == self.parent else "normal")
        self.tree.delete(*self.tree.get_children())
        self.entries = {}
        labels = dict(folder="폴더", log="로그 파일", session="통합 JSON", other="파일")
        for index, row in enumerate(listing["entries"]):
            key = str(index)
            self.entries[key] = row
            self.tree.insert("", "end", iid=key, text=("▸ " if row["directory"] else "≡ ") + row["name"],
                             values=(labels[row["kind"]],), tags=(row["kind"],))
        self.tree.yview_moveto(0)

    def go_up(self, _event=None):
        if self.current:
            self.navigate(self.parent)
        return "break"

    def scroll(self, event):
        direction = -1 if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0 else 1
        self.tree.yview_scroll(direction * 3, "units")
        return "break"

    def open_selection(self, _event=None):
        item = self.entries.get(self.tree.focus())
        if item and item["directory"]:
            self.navigate(item["path"])
        elif item:
            self.accept()
        return "break"

    def accept(self):
        selected = [self.entries[key] for key in self.tree.selection()]
        if len(selected) == 1 and selected[0]["directory"]:
            self.navigate(selected[0]["path"])
            return
        if not selected or any(row["directory"] for row in selected):
            self.messagebox.showinfo("로그 파일 선택", "불러올 파일을 선택하세요. 폴더는 더블클릭해서 여세요.", parent=self.dialog)
            return
        self.result = tuple(row["path"] for row in selected)
        self.dialog.destroy()

    def show(self):
        self.dialog.grab_set()
        self.dialog.wait_window()
        return self.result


class CalendarPicker:
    """Small dependency-free calendar; choosing a day updates the date field."""

    def __init__(self, parent, variable):
        import tkinter as tk
        from tkinter import ttk
        self.variable = variable
        try:
            selected = date.fromisoformat(variable.get())
        except ValueError:
            selected = date.today()
        self.year, self.month, self.selected = selected.year, selected.month, selected
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("날짜 선택")
        self.dialog.transient(parent)
        self.dialog.resizable(False, False)
        top = ttk.Frame(self.dialog, padding=10)
        top.pack(fill="x")
        ttk.Button(top, text="◀", width=4, command=lambda: self.move(-1)).pack(side="left")
        self.title = ttk.Label(top, anchor="center", width=20)
        self.title.pack(side="left")
        ttk.Button(top, text="▶", width=4, command=lambda: self.move(1)).pack(side="left")
        self.days = ttk.Frame(self.dialog, padding=(10, 0, 10, 10))
        self.days.pack()
        self.dialog.bind("<Escape>", lambda _e: self.dialog.destroy())
        self.draw()
        self.dialog.grab_set()

    def move(self, delta):
        value = self.year * 12 + self.month - 1 + delta
        year, month = divmod(value, 12)
        if 1 <= year <= 9999:
            self.year, self.month = year, month + 1
            self.draw()

    def draw(self):
        from tkinter import ttk
        self.title.configure(text=f"{self.year}년 {self.month}월")
        for widget in self.days.winfo_children():
            widget.destroy()
        for column, label in enumerate(("월", "화", "수", "목", "금", "토", "일")):
            ttk.Label(self.days, text=label, anchor="center", width=5).grid(row=0, column=column, pady=5)
        for row, week in enumerate(calendar.monthcalendar(self.year, self.month), 1):
            for column, day in enumerate(week):
                if day:
                    text = f"[{day}]" if date(self.year, self.month, day) == self.selected else str(day)
                    ttk.Button(self.days, text=text, width=5, command=lambda d=day: self.choose(d)).grid(row=row, column=column, padx=1, pady=2)

    def choose(self, day):
        self.variable.set(date(self.year, self.month, day).isoformat())
        self.dialog.destroy()


DATE_MODES = {"all": "전체 날짜", "include": "클릭한 날짜만 포함", "exclude": "클릭한 날짜 제외"}


class MultiDatePicker(CalendarPicker):
    """Multiple dates across months, committed only by the Apply button."""

    def __init__(self, parent, selected_dates, mode, first, last, apply):
        import tkinter as tk
        from tkinter import ttk
        self.chosen = set(selected_dates)
        self.first, self.last, self.apply = first, last, apply
        self.mode = tk.StringVar(value="include" if mode == "include" else "exclude")
        super().__init__(parent, tk.StringVar(value=first.isoformat()))
        self.dialog.title("달력에서 여러 날짜 선택")
        directions = ttk.Frame(self.dialog, padding=(10, 0, 10, 8))
        directions.pack(fill="x", before=self.days)
        ttk.Label(directions, text="날짜를 클릭해 선택/해제 · 월을 옮겨도 유지\n주말·연휴를 빼려면 '클릭한 날짜 제외' 선택").pack(anchor="w")
        for value in ("include", "exclude"):
            ttk.Radiobutton(directions, text=DATE_MODES[value], value=value, variable=self.mode, command=self.draw).pack(anchor="w", pady=2)
        tools = ttk.Frame(self.dialog, padding=(10, 0, 10, 6))
        tools.pack(fill="x")
        ttk.Button(tools, text="조회기간 주말 선택", command=self.weekends).pack(side="left")
        ttk.Button(tools, text="선택 모두 해제", command=self.clear).pack(side="left", padx=5)
        self.count = ttk.Label(self.dialog, padding=(10, 0, 10, 5))
        self.count.pack(anchor="w")
        selected = ttk.Frame(self.dialog, padding=(10, 0, 10, 5))
        selected.pack(fill="x")
        self.listbox = tk.Listbox(selected, height=4, selectmode="extended", exportselection=False)
        self.listbox.pack(side="left", fill="x", expand=True)
        scrollbar = ttk.Scrollbar(selected, orient="vertical", command=self.listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=scrollbar.set)
        ttk.Button(self.dialog, text="목록에서 선택한 날짜 해제", command=self.remove).pack(anchor="w", padx=10)
        actions = ttk.Frame(self.dialog, padding=10)
        actions.pack(fill="x")
        ttk.Button(actions, text="날짜 적용·조회", command=self.commit).pack(side="right")
        ttk.Button(actions, text="취소", command=self.dialog.destroy).pack(side="right", padx=5)
        self.draw()

    def draw(self):
        from tkinter import ttk
        self.title.configure(text=f"{self.year}년 {self.month}월")
        for widget in self.days.winfo_children():
            widget.destroy()
        self.day_buttons = {}
        style = "ExcludedDay.TButton" if self.mode.get() == "exclude" else "IncludedDay.TButton"
        ttk.Style(self.dialog).configure(style, foreground="#ba4a4a" if self.mode.get() == "exclude" else "#087f8c")
        for column, label in enumerate(("월", "화", "수", "목", "금", "토", "일")):
            ttk.Label(self.days, text=label, anchor="center", width=5).grid(row=0, column=column, pady=5)
        for row, week in enumerate(calendar.monthcalendar(self.year, self.month), 1):
            for column, day in enumerate(week):
                if not day:
                    continue
                value = date(self.year, self.month, day)
                chosen = value.isoformat() in self.chosen
                text = f"[{day}]" if chosen else str(day)
                button = ttk.Button(self.days, text=text, width=5, style=style if chosen else "TButton", command=lambda d=day: self.choose(d))
                button.grid(row=row, column=column, padx=1, pady=2)
                if not self.first <= value <= self.last:
                    button.state(["disabled"])
                self.day_buttons[day] = button
        if hasattr(self, "count"):
            values = sorted(self.chosen)
            within = sum(self.first.isoformat() <= d <= self.last.isoformat() for d in values)
            self.count.configure(text=f"{DATE_MODES[self.mode.get()]} · {len(values)}일 선택 (기간 내 {within}일)\n[숫자] 표시가 선택된 날짜입니다.")
            self.listbox.delete(0, "end")
            for value in values:
                self.listbox.insert("end", value)

    def choose(self, day):
        value = date(self.year, self.month, day)
        if not self.first <= value <= self.last:
            return
        key = value.isoformat()
        if key in self.chosen:
            self.chosen.remove(key)
        else:
            self.chosen.add(key)
        self.draw()
        self.day_buttons[day].focus_set()

    def weekends(self):
        current = self.first
        while current <= self.last:
            if current.weekday() >= 5:
                self.chosen.add(current.isoformat())
            current += timedelta(days=1)
        self.draw()

    def clear(self):
        self.chosen.clear()
        self.draw()

    def remove(self):
        for index in self.listbox.curselection():
            self.chosen.discard(self.listbox.get(index))
        self.draw()

    def commit(self):
        mode, dates = self.mode.get(), sorted(self.chosen)
        self.dialog.destroy()
        self.apply(mode, dates)


class MonitorGUI:
    def __init__(self, root, store, args):
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox, font
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        self.tk, self.ttk, self.filedialog, self.messagebox = tk, ttk, filedialog, messagebox
        self.Canvas, self.Toolbar = FigureCanvasTkAgg, NavigationToolbar2Tk
        self.root, self.store, self.data = root, store, None
        self.executor, self.events = ThreadPoolExecutor(max_workers=1), queue.Queue()
        self.generation, self.busy = 0, False
        self.view, self.plot = "overview", "overview"
        self.options, self.replace, self.dark = options_from(), False, False
        try:
            self.options.update(appearance_options(json.loads(PREFERENCES.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            pass
        self.filter_selection = {key: [] for key in ("groups", "nodes", "users")}
        self.page, self.sort_key, self.sort_reverse = 0, None, False
        self.rows, self.visible_rows, self.figure, self.canvas, self.toolbar = [], [], None, None, None
        self.closed = False
        self.pie_page, self.figure_key = 0, None
        self.guide = help_text()
        self.log_directory = str(Path.cwd())
        root.title("HPC 사용량 모니터 · Python X11 " + VERSION)
        root.geometry("1280x880")
        root.minsize(1000, 660)
        available = set(font.families(root))
        family = next((f for f in ("Malgun Gothic", "맑은 고딕", "NanumGothic", "Noto Sans CJK KR", "Arial") if f in available), "TkDefaultFont")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            font.nametofont(name).configure(family=family, size=args.font_size)
        style = self.style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=27)
        style.configure("Title.TLabel", font=(family, 19, "bold"))
        style.configure("Metric.TLabel", font=(family, 18, "bold"), foreground="#087f8c")
        sidebar = ttk.Frame(root, padding=15)
        sidebar.pack(side="left", fill="y")
        ttk.Label(sidebar, text="HPC Monitor", font=("Arial", 17, "bold")).pack(anchor="w", pady=(7, 0))
        ttk.Label(sidebar, text="Python · X11").pack(anchor="w", pady=(0, 25))
        for key, (label, _) in VIEWS.items():
            ttk.Button(sidebar, text=label, command=lambda v=key: self.switch_view(v)).pack(fill="x", pady=4)
        ttk.Label(sidebar, text="30분 스냅샷\n종료·작업 시간 추정").pack(side="bottom", anchor="w", pady=15)
        body = ttk.Frame(root, padding=(8, 15, 18, 9))
        body.pack(fill="both", expand=True)
        top = ttk.Frame(body)
        top.pack(fill="x")
        self.title = ttk.Label(top, text="사용량 현황", style="Title.TLabel")
        self.title.pack(side="left")
        self.controls = []
        for label, command in [("로그 불러오기", self.load_files), ("예제", self.demo), ("설정", self.settings), ("도움말", lambda: self.notebook.select(self.help_frame)), ("통합 JSON", self.save_session), ("집계 CSV", self.save_csv), ("그래프 PNG", self.save_png)]:
            button = ttk.Button(top, text=label, command=command)
            button.pack(side="right", padx=3)
            self.controls.append(button)
        filters = ttk.LabelFrame(body, text="조회 조건", padding=10)
        filters.pack(fill="x", pady=(17, 8))
        self.from_var, self.to_var = tk.StringVar(), tk.StringVar()
        self.date_from, self.date_to = tk.StringVar(), tk.StringVar()
        self.range_mode = tk.StringVar(value="날짜 · 종료일 포함")
        self.range_fields, self.range_labels = [], []
        for column, (label, variable, day_var) in enumerate((("시작일", self.from_var, self.date_from), ("종료일 · 포함", self.to_var, self.date_to))):
            heading = ttk.Label(filters, text=label)
            heading.grid(row=0, column=column, sticky="w")
            self.range_labels.append(heading)
            frame = ttk.Frame(filters)
            frame.grid(row=1, column=column, padx=(0, 9), sticky="ew")
            dates = ttk.Frame(frame)
            dates.pack(fill="x")
            entry = ttk.Entry(dates, textvariable=day_var, width=12)
            entry.pack(side="left", fill="x", expand=True)
            ttk.Button(dates, text="달력", width=5, command=lambda v=day_var: CalendarPicker(self.root, v)).pack(side="left", padx=(4, 0))
            entry.bind("<Return>", lambda _e: self.apply_filters())
            precise = ttk.Entry(frame, textvariable=variable, width=21)
            precise.bind("<Return>", lambda _e: self.apply_filters())
            self.range_fields.append((dates, precise))
        self.filter_buttons = {}
        for column, (kind, label) in enumerate((("groups", "노드 그룹"), ("nodes", "노드"), ("users", "사용자")), 2):
            ttk.Label(filters, text=label).grid(row=0, column=column, sticky="w")
            button = ttk.Button(filters, text="전체", command=lambda k=kind: self.choose_filter(k))
            button.grid(row=1, column=column, padx=(0, 9), sticky="ew")
            self.filter_buttons[kind] = button
        ttk.Label(filters, text="표시 간격").grid(row=0, column=5, sticky="w")
        self.bucket_var = tk.StringVar(value="일별")
        self.bucket = ttk.Combobox(filters, textvariable=self.bucket_var, values=["시간별", "일별", "주별", "월별"], state="readonly", width=8)
        self.bucket.grid(row=1, column=5, padx=(0, 9))
        ttk.Button(filters, text="조회", command=self.apply_filters).grid(row=1, column=6, padx=(0, 5))
        ttk.Button(filters, text="초기화", command=self.reset_filters).grid(row=1, column=7)
        mode = ttk.Combobox(filters, textvariable=self.range_mode, values=["날짜 · 종료일 포함", "시각 · 종료 미포함"], state="readonly", width=22)
        mode.grid(row=2, column=0, columnspan=2, sticky="w", pady=(9, 0))
        mode.bind("<<ComboboxSelected>>", self.change_range_mode)
        ttk.Label(filters, text="날짜를 고른 뒤 조회 · 종료일 전체 포함").grid(row=2, column=2, columnspan=6, sticky="w", pady=(9, 0))
        date_tools = ttk.Frame(filters)
        date_tools.grid(row=3, column=0, columnspan=8, sticky="ew", pady=(10, 0))
        ttk.Label(date_tools, text="날짜 포함·제외").pack(side="left", padx=(0, 8))
        self.date_mode = tk.StringVar(value=DATE_MODES["all"])
        date_mode = ttk.Combobox(date_tools, textvariable=self.date_mode, values=list(DATE_MODES.values()), state="readonly", width=23)
        date_mode.pack(side="left", padx=(0, 8))
        date_mode.bind("<<ComboboxSelected>>", lambda _e: self.sync_date_filter())
        ttk.Button(date_tools, text="달력에서 여러 날짜 선택", command=self.choose_dates).pack(side="left", padx=(0, 8))
        self.date_summary = ttk.Label(date_tools, text="주말·연휴를 골라 제외할 수 있습니다.")
        self.date_summary.pack(side="left")
        for column in range(5):
            filters.columnconfigure(column, weight=1)
        self.caption = ttk.Label(body, text="탭 구분 파일을 선택하거나 예제로 시작하세요.")
        self.caption.pack(fill="x", pady=(0, 8))
        metrics = ttk.Frame(body)
        metrics.pack(fill="x", pady=(0, 12))
        self.metric_labels = {}
        for column, (key, label) in enumerate((("running", "관측된 실행 작업"), ("coreHours", "코어 시간 · core·h"),
                                                ("busyPct", "작업 점유율 · 추정"), ("coverage", "기록 관측률"),
                                                ("waitMean", "평균 시작 대기 · min"), ("waitMax", "최대 시작 대기 · min"))):
            box = ttk.LabelFrame(metrics, text=label, padding=9)
            box.grid(row=0, column=column, sticky="ew", padx=(0, 7))
            metrics.columnconfigure(column, weight=1)
            value = ttk.Label(box, text="—", style="Metric.TLabel")
            value.pack(anchor="w")
            self.metric_labels[key] = value
        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill="both", expand=True)
        self.chart_frame, self.table_frame, self.help_frame = ttk.Frame(self.notebook), ttk.Frame(self.notebook), ttk.Frame(self.notebook)
        self.notebook.add(self.chart_frame, text="그래프")
        self.notebook.add(self.table_frame, text="상세 표")
        self.notebook.add(self.help_frame, text="도움말·검증")
        chart_tools = ttk.Frame(self.chart_frame, padding=5)
        chart_tools.pack(fill="x")
        self.plot_var = tk.StringVar(value="overview")
        self.plot_buttons = ttk.Frame(chart_tools)
        self.plot_buttons.pack(side="left", fill="x", expand=True)
        self.dark_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(chart_tools, text="다크 모드", variable=self.dark_var, command=self.toggle_theme).pack(side="right")
        ttk.Button(chart_tools, text="이름·그래프", command=self.appearance_settings).pack(side="right", padx=6)
        pie_tools = ttk.Frame(self.chart_frame, padding=(7, 3))
        pie_tools.pack(fill="x")
        ttk.Label(pie_tools, text="원그래프 최대 가동률 (%)").pack(side="left")
        self.pie_max = tk.StringVar(value="100")
        maximum = ttk.Entry(pie_tools, textvariable=self.pie_max, width=7)
        maximum.pack(side="left", padx=6)
        maximum.bind("<Return>", lambda _e: self.apply_pie_max())
        ttk.Button(pie_tools, text="적용", command=self.apply_pie_max).pack(side="left")
        ttk.Label(pie_tools, text="채움 기준만 변경 · 숫자는 실제 가동률").pack(side="left", padx=8)
        self.pie_caption = ttk.Label(pie_tools, text="")
        self.pie_caption.pack(side="right", padx=6)
        self.pie_next = ttk.Button(pie_tools, text="다음", command=lambda: self.move_pies(1))
        self.pie_next.pack(side="right")
        self.pie_prev = ttk.Button(pie_tools, text="이전", command=lambda: self.move_pies(-1))
        self.pie_prev.pack(side="right", padx=4)
        self.canvas_frame = ttk.Frame(self.chart_frame)
        self.canvas_frame.pack(fill="both", expand=True)
        self.chart_scroll = tk.Canvas(self.canvas_frame, highlightthickness=0)
        self.chart_scrollbar = ttk.Scrollbar(self.canvas_frame, orient="vertical", command=self.chart_scroll.yview)
        self.chart_scroll.configure(yscrollcommand=self.chart_scrollbar.set)
        self.chart_scrollbar.pack(side="right", fill="y")
        chart_xscroll = ttk.Scrollbar(self.canvas_frame, orient="horizontal", command=self.chart_scroll.xview)
        chart_xscroll.pack(side="bottom", fill="x")
        self.chart_scroll.configure(xscrollcommand=chart_xscroll.set)
        self.chart_scroll.pack(fill="both", expand=True)
        self.chart_scroll.bind("<Configure>", self.size_chart)
        self.chart_window = None
        table_tools = ttk.Frame(self.table_frame, padding=6)
        table_tools.pack(fill="x")
        ttk.Label(table_tools, text="검색").pack(side="left")
        self.search_var = tk.StringVar()
        search = ttk.Entry(table_tools, textvariable=self.search_var, width=27)
        search.pack(side="left", padx=8)
        search.bind("<KeyRelease>", lambda _e: self.search_later())
        self.table_message = ttk.Label(table_tools, text="")
        self.table_message.pack(side="left", padx=8)
        for delta, label in ((1, "다음"), (-1, "이전")):
            ttk.Button(table_tools, text=label, command=lambda x=delta: self.move_page(x)).pack(side="right", padx=3)
        table_box = ttk.Frame(self.table_frame)
        table_box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_box, show="headings", selectmode="extended")
        yscroll = ttk.Scrollbar(table_box, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_box, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_box.rowconfigure(0, weight=1)
        table_box.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self.row_details)
        self.help_text = tk.Text(self.help_frame, wrap="word", padx=17, pady=15, state="disabled")
        help_scroll = ttk.Scrollbar(self.help_frame, orient="vertical", command=self.help_text.yview)
        self.help_text.configure(yscrollcommand=help_scroll.set)
        help_scroll.pack(side="right", fill="y")
        self.help_text.pack(side="left", fill="both", expand=True)
        bottom = ttk.Frame(body)
        bottom.pack(fill="x", pady=(7, 0))
        self.status = tk.StringVar(value="준비")
        ttk.Label(bottom, textvariable=self.status).pack(side="left")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=160)
        self.progress.pack(side="right")
        self.root.after(80, self.poll)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.update_range()
        self.switch_view("overview", draw=False)
        if self.store.describe()["min"] is not None and (self.store.describe()["min"] != self.store.describe()["max"] or (self.store.describe()["counts"]["gpu"] or self.store.describe()["counts"]["gpu_user"])):
            if self.store.describe()["min"] == self.store.describe()["max"] or self.gpu_only():
                self.switch_view("gpu", draw=False)
            self.apply_filters()
        elif self.store.describe()["min"] is not None:
            self.data = dict(empty=True, meta=self.store.describe(self.options), settings=dict(self.options),
                             error="기록 시각이 하나뿐입니다. 다음 시각의 로그를 추가하세요.")
            self.switch_view("data")
        else:
            self.render()

    def work(self, function, done):
        self.generation += 1
        ticket = self.generation
        self.busy = True
        for control in self.controls:
            control.configure(state="disabled")
        self.progress.start(12)
        self.status.set("로그를 읽고 사용량을 집계하고 있습니다…")
        future = self.executor.submit(function)
        def completed(f):
            try:
                self.events.put((ticket, done, f.result(), None))
            except Exception as exc:
                self.events.put((ticket, done, None, exc))
        future.add_done_callback(completed)

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                ticket, done, value, error = self.events.get_nowait()
                if ticket != self.generation:
                    continue
                if done is None:
                    self.status.set(value)
                    continue
                self.busy = False
                self.progress.stop()
                for control in self.controls:
                    control.configure(state="normal")
                if error:
                    self.status.set("처리 실패: " + str(error))
                    self.messagebox.showerror("로그 처리 오류", str(error), parent=self.root)
                else:
                    done(value)
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def update_range(self):
        meta = self.store.describe(self.options)
        if meta["min"] is not None:
            self.from_var.set(iso(meta["min"], self.options["offset"]))
            self.to_var.set(iso(meta["max"], self.options["offset"]))
            self.date_from.set(iso(meta["min"], self.options["offset"])[:10])
            self.date_to.set(iso(meta["max"], self.options["offset"])[:10])

    def change_range_mode(self, _event=None):
        dates = self.range_mode.get().startswith("날짜")
        if (self.options["rangeMode"] == "dates") != dates:
            try:
                if dates:
                    a = parse_time(self.from_var.get(), self.options["offset"])
                    b = parse_time(self.to_var.get(), self.options["offset"])
                    if a is not None and b is not None:
                        self.date_from.set(iso(a, self.options["offset"])[:10])
                        self.date_to.set(iso(b - (1 if b > a else 0), self.options["offset"])[:10])
                else:
                    a, b = date_range(self.date_from.get(), self.date_to.get(), self.options["offset"])
                    self.from_var.set(iso(a, self.options["offset"]))
                    self.to_var.set(iso(b, self.options["offset"]))
            except ValueError:
                pass
        for index, (day_fields, time_field) in enumerate(self.range_fields):
            day_fields.pack_forget()
            time_field.pack_forget()
            (day_fields if dates else time_field).pack(fill="x")
            self.range_labels[index].configure(text=("시작일" if index == 0 else "종료일 · 포함") if dates else ("조회 시작 시각" if index == 0 else "종료 시각 · 미포함"))
        self.options["rangeMode"] = "dates" if dates else "times"

    def sync_date_filter(self):
        mode = next((key for key, value in DATE_MODES.items() if value == self.date_mode.get()), "all")
        self.date_summary.configure(text="전체 날짜 집계" if mode == "all" else f"{len(self.options['selectedDates'])}일 선택 · 조회로 적용")

    def choose_dates(self):
        if self.busy:
            return
        try:
            if self.range_mode.get().startswith("날짜"):
                a, b = date_range(self.date_from.get(), self.date_to.get(), self.options["offset"])
            else:
                a, b = parse_time(self.from_var.get(), self.options["offset"]), parse_time(self.to_var.get(), self.options["offset"])
            if a is None or b is None or b < a or b - a > 3653 * 86400000:
                raise ValueError("시작일·종료일을 확인하세요. 최대 10년까지 선택할 수 있습니다.")
        except ValueError as exc:
            self.messagebox.showinfo("날짜 선택", str(exc), parent=self.root)
            return
        mode = next((key for key, value in DATE_MODES.items() if value == self.date_mode.get()), "all")
        def apply(mode, dates):
            self.options["dateMode"], self.options["selectedDates"] = mode, dates
            self.date_mode.set(DATE_MODES[mode])
            self.sync_date_filter()
            self.apply_filters()
        self.date_picker = MultiDatePicker(self.root, self.options["selectedDates"], mode,
                                          date.fromisoformat(iso(a, self.options["offset"])[:10]),
                                          date.fromisoformat(iso(b - (1 if b > a else 0), self.options["offset"])[:10]), apply)

    def apply_filters(self):
        if self.busy:
            return
        dates = self.range_mode.get().startswith("날짜")
        try:
            if dates:
                a, b = date_range(self.date_from.get(), self.date_to.get(), self.options["offset"])
            else:
                a = parse_time(self.from_var.get(), self.options["offset"])
                b = parse_time(self.to_var.get(), self.options["offset"])
        except ValueError as exc:
            self.messagebox.showinfo("조회 기간", str(exc), parent=self.root)
            return
        meta = self.store.describe(self.options)
        snapshot_only = bool((meta["counts"]["gpu"] or meta["counts"]["gpu_user"]) and meta["min"] == meta["max"] and a is not None and b is not None and a <= meta["min"] <= b)
        if snapshot_only:
            a = b = meta["min"]
        if a is None or b is None or b <= a and not snapshot_only:
            self.messagebox.showinfo("조회 기간", "서로 다른 시각의 기록을 두 개 이상 불러오고 시작·종료 시각을 확인하세요.", parent=self.root)
            return
        self.options.update(self.filter_selection)
        self.options["dateMode"] = next((key for key, value in DATE_MODES.items() if value == self.date_mode.get()), "all")
        self.options.update({"from": a, "to": b, "rangeMode": "dates" if dates else "times", "bucket": {"시간별": "hour", "일별": "day", "주별": "week", "월별": "month"}[self.bucket_var.get()]})
        options = dict(self.options)
        self.work(lambda: analyze(self.store, options), self.accept)

    def gpu_only(self):
        counts = self.store.describe()["counts"]
        return bool(counts["gpu"] or counts["gpu_user"]) and not any(counts[k] for k in ("node_cpu", "user_cpu", "queue_started", "queue_pending"))

    def accept(self, data):
        self.data = data
        if self.view == "gpu" and self.plot == "gpu_latest" and not data.get("empty") and not any(r["util"] is not None for r in data["latestGpu"]):
            self.plot = next((plot for key, plot in (("memPct", "gpu_memory"), ("memUsedMb", "gpu_memory_used"), ("power", "gpu_power_latest"), ("temp", "gpu_temp_latest"))
                              if any(r[key] is not None for r in data["latestGpu"])),
                             "gpu_user_memory" if any(r["memUsedMb"] is not None for r in data["latestGpuUsers"]) else "gpu_processes")
            self.plot_var.set(self.plot)
        self.options["bucket"] = data["settings"]["bucket"]
        self.bucket_var.set({"hour": "시간별", "day": "일별", "week": "주별", "month": "월별"}[self.options["bucket"]])
        self.status.set("집계 완료 · 표 행을 더블클릭하면 전체 내용을 확인할 수 있습니다.")
        self.render()

    def load_files(self):
        if self.busy:
            return
        paths = LogFilePicker(self.root, self.log_directory).show()
        if not paths:
            return
        self.log_directory = str(Path(paths[0]).parent)
        old, replace = self.store, self.replace or self.store.demo
        def task():
            store = LogStore() if replace else old
            store.imported_settings = None
            for index, path in enumerate(paths, 1):
                self.events.put((self.generation, None, f"{index}/{len(paths)} 파일 읽는 중 · {Path(path).name}", None))
                try:
                    store.load_path(path)
                except (ValueError, OSError, UnicodeError) as exc:
                    store.issue(Path(path).name, 0, str(exc))
                    store.files.append(dict(name=Path(path).name, rows=0, duplicates=0, invalid=1, types=[]))
            self.events.put((self.generation, None, "로그 시간 인덱스를 준비하고 있습니다…", None))
            store.describe(store.imported_settings or self.options)  # Build the time index off the GUI thread.
            return store
        def done(store):
            self.store = store
            if store.imported_settings:
                self.options.update({k: v for k, v in store.imported_settings.items() if k in DEFAULTS or k in PRESENTATION_KEYS})
                self.pie_max.set(str(self.options["pieMax"]))
                self.range_mode.set("날짜 · 종료일 포함" if self.options["rangeMode"] == "dates" else "시각 · 종료 미포함")
                self.date_mode.set(DATE_MODES[self.options["dateMode"]])
                self.sync_date_filter()
                self.change_range_mode()
            self.update_range()
            self.filter_selection = {k: [] for k in self.filter_selection}
            self.update_filter_labels()
            if store.describe()["min"] == store.describe()["max"] and not (store.describe()["counts"]["gpu"] or store.describe()["counts"]["gpu_user"]):
                self.data = dict(empty=True, meta=store.describe(self.options), settings=dict(self.options),
                                 error="기록 시각이 하나뿐입니다. 다음 시각의 로그를 추가하세요.")
                self.switch_view("data")
                self.render()
            else:
                if store.describe()["min"] == store.describe()["max"] or self.gpu_only():
                    self.switch_view("gpu", draw=False)
                self.apply_filters()
        self.work(task, done)

    def demo(self):
        if not self.busy:
            def done(store):
                self.store = store
                self.update_range()
                self.reset_filters()
            self.work(demo_store, done)

    def reset_filters(self):
        self.options["dateMode"], self.options["selectedDates"] = "all", []
        self.date_mode.set(DATE_MODES["all"])
        self.sync_date_filter()
        self.filter_selection = {k: [] for k in self.filter_selection}
        self.update_filter_labels()
        self.update_range()
        if not self.busy and self.store.describe()["min"] is not None:
            self.apply_filters()

    def update_filter_labels(self):
        for kind, button in self.filter_buttons.items():
            values = self.filter_selection[kind]
            button.configure(text="전체" if not values else values[0] if len(values) == 1 else f"{len(values)}개 선택")

    def choose_filter(self, kind):
        if self.busy:
            return
        tk, ttk = self.tk, self.ttk
        dialog = tk.Toplevel(self.root)
        dialog.title({"groups": "노드 그룹", "nodes": "노드", "users": "사용자"}[kind] + " 선택")
        dialog.transient(self.root)
        dialog.geometry("390x480")
        ttk.Label(dialog, text="여러 항목은 Ctrl/Shift로 선택 · 선택 없음 = 전체", padding=10).pack(fill="x")
        box = tk.Listbox(dialog, selectmode="extended", exportselection=False)
        box.pack(fill="both", expand=True, padx=12, pady=5)
        values = self.store.describe(self.options)[kind]
        for i, value in enumerate(values):
            box.insert("end", (self.options["userNames"].get(value, value) + " (" + value + ")") if kind == "users" else value)
            if value in self.filter_selection[kind]:
                box.selection_set(i)
        controls = ttk.Frame(dialog, padding=12)
        controls.pack(fill="x")
        ttk.Button(controls, text="전체", command=lambda: box.selection_clear(0, "end")).pack(side="left")
        def apply():
            self.filter_selection[kind] = [values[i] for i in box.curselection()]
            self.update_filter_labels()
            dialog.destroy()
            if self.data:
                self.apply_filters()
        ttk.Button(controls, text="적용", command=apply).pack(side="right")
        dialog.grab_set()

    def switch_view(self, view, draw=True):
        self.view, self.plot, self.page = view, VIEWS[view][1][0], 0
        self.pie_page = 0
        self.title.configure(text=VIEWS[view][0] + (" · 예제 데이터" if self.store.demo else ""))
        for button in self.plot_buttons.winfo_children():
            button.destroy()
        for index, plot in enumerate(VIEWS[view][1]):
            self.ttk.Radiobutton(self.plot_buttons, text=PLOT_NAMES[plot], variable=self.plot_var, value=plot,
                                 command=lambda p=plot: self.select_plot(p)).grid(row=index // 3, column=index % 3, padx=5, pady=4, sticky="w")
        self.plot_var.set(self.plot)
        self.search_var.set("")
        self.sort_key = None
        if draw:
            self.notebook.select(self.chart_frame)
            self.render()

    def select_plot(self, plot):
        self.plot = plot
        self.pie_page = 0
        self.plot_var.set(plot)
        self.notebook.select(self.chart_frame)
        if self.view == "gpu":
            self.render()
        else:
            self.render_figure()

    def apply_pie_max(self):
        try:
            maximum = float(self.pie_max.get())
            if not 0 < maximum <= 100:
                raise ValueError()
        except ValueError:
            self.messagebox.showinfo("원그래프 최대 가동률", "0보다 크고 100 이하인 숫자를 입력하세요. 예: 80, 70", parent=self.root)
            return
        self.options["pieMax"] = maximum
        if self.data:
            self.data["settings"]["pieMax"] = maximum
        self.render_figure()

    def move_pies(self, delta):
        self.pie_page += delta
        self.render_figure()

    def render(self):
        meta = self.store.describe(self.options)
        data = self.data
        if data and not data.get("empty"):
            for key, label in self.metric_labels.items():
                label.configure(text=fmt(data["summary"][key], "minutes" if key.startswith("wait") else "pct" if key.endswith("Pct") or key == "coverage" else "num"))
            tz = "한국시간" if self.options["offset"] == 540 else "UTC"
            first = iso(data["from"], self.options["offset"])
            last = iso(data["to"] - (0 if data.get("snapshotOnly") else 1), self.options["offset"])
            period = f"{first[:10]} — {last[:10]} (종료일 포함)" if data["settings"]["rangeMode"] == "dates" else f"{first} — {iso(data['to'], self.options['offset'])}"
            if data["settings"]["dateMode"] != "all":
                period += f" · {DATE_MODES[data['settings']['dateMode']]} {len(data['settings']['selectedDates'])}일 · 집계 {data['selectedSeconds'] / 3600:g}시간"
            self.caption.configure(text=f"{period} · {tz} · {sum(meta['counts'].values()):,}개 기록 · 확인 사항 {meta['issueTotal']:,}개")
        else:
            self.caption.configure(text=data.get("error", "로그를 불러오세요.") if data else "탭 구분 로그 파일을 선택하거나 예제로 시작하세요.")
            for label in self.metric_labels.values():
                label.configure(text="—")
        table_view = "gpu_latest" if self.view == "gpu" and self.plot == "gpu_latest" else self.view
        if self.view == "gpu" and self.plot in ("gpu_memory", "gpu_memory_used", "gpu_power_latest", "gpu_temp_latest"):
            table_view = "gpu_latest"
        if self.view == "gpu" and self.plot in ("gpu_user_memory", "gpu_processes"):
            table_view = "gpu_users_latest"
        self.rows = meta["files"] if table_view == "data" else data.get(DATA_KEYS[table_view], []) if data and not data.get("empty") else []
        self.columns = TABLES[table_view]
        self.page = 0
        self.update_table()
        notices = data.get("notices", []) if data else []
        help_text = self.guide + "\n\n현재 조회의 확인 사항\n" + "\n\n".join(notices)
        help_text += "\n\n입력 파일 확인 사항\n" + "\n".join(f"{r['file']}:{r['line']}  {r['message']}" for r in meta["issues"])
        self.help_text.configure(state="normal")
        self.help_text.delete("1.0", "end")
        self.help_text.insert("1.0", help_text)
        self.help_text.configure(state="disabled")
        self.render_figure()

    def render_figure(self):
        table = "nodes" if self.plot == "node_pies" else "groups" if self.plot == "group_pies" else None
        count = len((self.data or {}).get(table, [])) if table else 0
        pages = max(1, math.ceil(count / self.options["chartStyle"]["piesPerPage"]))
        self.pie_page = max(0, min(self.pie_page, pages - 1))
        self.pie_prev.configure(state="normal" if table and self.pie_page > 0 else "disabled")
        self.pie_next.configure(state="normal" if table and self.pie_page + 1 < pages else "disabled")
        self.pie_caption.configure(text=f"{self.pie_page + 1}/{pages} · {count}개" if table else "")
        key = (id(self.data), self.view, self.plot, self.dark_var.get(), self.options["pieMax"], self.pie_page,
               json.dumps(appearance_options(self.options), sort_keys=True))
        if self.figure_key == key:
            return
        if self.canvas:
            self.canvas.get_tk_widget().destroy()
        if self.toolbar:
            self.toolbar.destroy()
        if self.chart_window is not None:
            self.chart_scroll.delete(self.chart_window)
        if self.figure:
            self.figure.canvas.toolbar = None
            self.figure.clear()
        self.figure = figure_for(self.data, self.view, self.plot, self.dark_var.get(), pie_max=self.options["pieMax"], pie_page=self.pie_page, settings=self.options)
        self.chart_scroll.configure(background="#172b39" if self.dark_var.get() else "#ffffff")
        self.canvas = self.Canvas(self.figure, master=self.chart_scroll)
        self.canvas.mpl_connect("pick_event", self.rename_user)
        self.toolbar = self.Toolbar(self.canvas, self.canvas_frame, pack_toolbar=False)
        self.toolbar.update()
        self.toolbar.pack(side="bottom", fill="x")
        self.chart_window = self.chart_scroll.create_window(0, 0, anchor="nw", window=self.canvas.get_tk_widget())
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.get_tk_widget().bind(event, self.scroll_chart, add="+")
        self.chart_scroll.yview_moveto(0)
        self.size_chart()
        self.canvas.draw_idle()
        self.figure_key = key

    def size_chart(self, _event=None):
        if not self.canvas or self.chart_window is None:
            return
        style = self.options["chartStyle"]
        width, height = style["chartWidth"], style["chartHeight"]
        if self.plot in ("node_pies", "group_pies", "usage_pie") and self.data and not self.data.get("empty"):
            table = "nodes" if self.plot == "node_pies" else "groups"
            per_page, columns = style["piesPerPage"], style["pieColumns"]
            count = min(per_page, max(0, len(self.data.get(table, [])) - self.pie_page * per_page))
            if self.plot == "usage_pie":
                count = 1
            width = max(width, min(columns, count) * max(style["pieSize"] + 2 * style["pieGap"], style["titleSize"] * 10))
            height = max(height, math.ceil(count / columns) * (style["pieSize"] + 2 * style["pieGap"] + style["titleSize"] + 3 * style["fontSize"]) + 2 * style["titleSize"] + 32)
        scale = min(1, math.sqrt(32_000_000 / (width * height)), 16384 / max(width, height))
        self.figure.set_dpi(100 * scale)
        width, height = max(1, int(width * scale)), max(1, int(height * scale))
        self.chart_scroll.itemconfigure(self.chart_window, width=width, height=height)
        self.chart_scroll.configure(scrollregion=(0, 0, width, height))

    def scroll_chart(self, event):
        if self.canvas:
            direction = -1 if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0 else 1
            self.chart_scroll.yview_scroll(direction * 3, "units")
            return "break"

    def toggle_theme(self):
        dark = self.dark_var.get()
        bg, panel, fg, field = ("#101e29", "#172b39", "#dfeaf1", "#1b3040") if dark else ("#f4f6f8", "#ffffff", "#172a3a", "#ffffff")
        self.root.configure(background=bg)
        for widget in (".", "TFrame", "TLabel", "TLabelframe", "TLabelframe.Label", "TCheckbutton", "TRadiobutton", "TNotebook"):
            self.style.configure(widget, background=bg, foreground=fg)
        self.style.configure("TButton", background=panel, foreground=fg)
        self.style.configure("TEntry", fieldbackground=field, foreground=fg)
        self.style.configure("TCombobox", fieldbackground=field, foreground=fg)
        self.style.map("TCombobox", fieldbackground=[("readonly", field)], foreground=[("readonly", fg)])
        self.style.configure("Treeview", background=panel, fieldbackground=panel, foreground=fg)
        self.style.configure("Treeview.Heading", background=field, foreground=fg)
        self.style.configure("Metric.TLabel", background=bg, foreground="#63c4c3" if dark else "#087f8c")
        self.help_text.configure(background=panel, foreground=fg, insertbackground=fg)
        self.render_figure()

    def search_later(self):
        if hasattr(self, "_search_after"):
            self.root.after_cancel(self._search_after)
        self._search_after = self.root.after(200, self.search_now)

    def search_now(self):
        self.page = 0
        self.update_table()

    def update_table(self):
        query = self.search_var.get().lower()
        rows = [r for r in self.rows if not query or any(query in str(r.get(k, "")).lower() for k, *_ in self.columns)
                or query in self.options["userNames"].get(r.get("user", r.get("name", "") if self.view == "users" else ""), "").lower()]
        if self.sort_key:
            present = [r for r in rows if r.get(self.sort_key) is not None]
            missing = [r for r in rows if r.get(self.sort_key) is None]
            present.sort(key=lambda r: (0, r[self.sort_key]) if isinstance(r[self.sort_key], (int, float)) else (1, str(r[self.sort_key])), reverse=self.sort_reverse)
            rows = present + missing
        self.visible_rows = rows
        self.page = max(0, min(self.page, max(0, (len(rows) - 1) // 50)))
        keys = [c[0] for c in self.columns]
        self.tree.configure(columns=keys)
        self.tree.delete(*self.tree.get_children())
        for key, label, *_ in self.columns:
            self.tree.heading(key, text=label + " ↕", command=lambda k=key: self.sort(k))
            self.tree.column(key, minwidth=80, width=165 if key in ("start", "submit", "end", "t", "name") else 132, stretch=False)
        for index, row in enumerate(rows[self.page * 50:(self.page + 1) * 50]):
            values = []
            for key, _label, *tail in self.columns:
                value = row.get(key)
                if key == "user" or key == "name" and (self.view == "users" or self.plot in ("gpu_user_memory", "gpu_processes")):
                    alias = self.options["userNames"].get(value)
                    if alias:
                        value = alias + " (" + value + ")"
                values.append(fmt(value, tail[0] if tail else "", self.options["offset"]))
            self.tree.insert("", "end", iid=str(index), values=values)
        self.table_message.configure(text=f"{len(rows):,}개 · {self.page + 1}/{max(1, (len(rows) + 49) // 50)} 페이지")

    def sort(self, key):
        self.sort_reverse = not self.sort_reverse if self.sort_key == key else False
        self.sort_key = key
        self.update_table()

    def move_page(self, delta):
        self.page += delta
        self.update_table()

    def row_details(self, _event):
        selected = self.tree.selection()
        if selected:
            row = self.visible_rows[self.page * 50 + int(selected[0])]
            text = "\n".join(label + ": " + fmt(row.get(key), tail[0] if tail else "", self.options["offset"])
                             for key, label, *tail in self.columns)
            self.messagebox.showinfo("기록 상세", text, parent=self.root)

    def save_session(self):
        path = self.filedialog.asksaveasfilename(parent=self.root, defaultextension=".json", initialfile="HPC_logs.json")
        if path:
            Path(path).write_text(json.dumps(self.store.session(self.options), ensure_ascii=False, allow_nan=False), encoding="utf-8")
            self.status.set("통합 로그 저장 완료")

    def save_csv(self):
        path = self.filedialog.asksaveasfilename(parent=self.root, defaultextension=".csv", initialfile="HPC_" + self.view + ".csv")
        if path:
            rows, columns = self.visible_rows, self.columns
            if self.view in ("users", "queue") or self.plot in ("gpu_user_memory", "gpu_processes"):
                key = "user" if self.view == "queue" else "name"
                rows = [dict(row, displayName=self.options["userNames"].get(row.get(key), row.get(key))) for row in rows]
                columns = [*columns, ("displayName", "표시 이름")]
            Path(path).write_text(table_csv(rows, columns, self.options["offset"]), encoding="utf-8", newline="")
            self.status.set("현재 필터·검색 결과 CSV 저장 완료")

    def save_png(self):
        path = self.filedialog.asksaveasfilename(parent=self.root, defaultextension=".png", initialfile="HPC_" + self.plot + ".png")
        if path and self.figure:
            width, height = self.figure.get_size_inches() * 100
            scale = min(self.options["chartStyle"]["exportScale"], math.sqrt(32_000_000 / (width * height)), 16384 / max(width, height))
            self.figure.savefig(path, dpi=100 * scale, facecolor=self.figure.get_facecolor())
            self.status.set("그래프 PNG 저장 완료")

    def save_preferences(self):
        try:
            temporary = PREFERENCES.with_suffix(".tmp")
            temporary.write_text(json.dumps(appearance_options(self.options), ensure_ascii=False, allow_nan=False), encoding="utf-8")
            temporary.replace(PREFERENCES)
        except OSError as exc:
            self.messagebox.showerror("설정 저장", "설정을 자동 저장하지 못했습니다. 통합 JSON으로 보관하세요.\n" + str(exc), parent=self.root)

    def apply_appearance(self, config):
        self.options.update(appearance_options(config))
        if self.data:
            self.data["settings"] = dict(self.data.get("settings", {}), **appearance_options(self.options))
        self.pie_page = 0
        self.save_preferences()
        self.render()

    def rename_user(self, event):
        uid = getattr(event.artist, "hpc_user", None)
        if uid is None or self.busy or self.toolbar.mode:
            return
        from tkinter import simpledialog
        name = simpledialog.askstring("사용자 표시 이름", f"USER ID: {uid}\n표시 이름 (비우면 ID 표시)",
                                      initialvalue=self.options["userNames"].get(uid, ""), parent=self.root)
        if name is not None:
            names = dict(self.options["userNames"])
            if name.strip():
                names[uid] = name.strip()
            else:
                names.pop(uid, None)
            self.apply_appearance(dict(self.options, userNames=names))

    def appearance_settings(self):
        if self.busy:
            return
        from tkinter import colorchooser
        tk, ttk = self.tk, self.ttk
        dialog = tk.Toplevel(self.root)
        dialog.title("이름·그래프 설정")
        dialog.transient(self.root)
        outer = ttk.Frame(dialog, padding=16)
        outer.pack(fill="both", expand=True)
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        dimensions, colors, users = [ttk.Frame(tabs, padding=12) for _ in range(3)]
        for frame, title in ((dimensions, "크기·배치·글꼴"), (colors, "그래프 색상"), (users, "USER ID–이름")):
            tabs.add(frame, text=title)
        variables = {key: tk.StringVar(value=str(value)) for key, value in self.options["chartStyle"].items()}
        for index, (key, (_default, low, high)) in enumerate(STYLE_NUMBERS.items()):
            ttk.Label(dimensions, text=f"{STYLE_LABELS[key]} ({low}–{high})").grid(row=index, column=0, sticky="w", padx=(0, 20), pady=5)
            ttk.Entry(dimensions, textvariable=variables[key], width=12).grid(row=index, column=1, pady=5)
        for index, key in enumerate(STYLE_COLORS):
            ttk.Label(colors, text=STYLE_LABELS[key]).grid(row=index, column=0, sticky="w", padx=(0, 15), pady=7)
            ttk.Entry(colors, textvariable=variables[key], width=12).grid(row=index, column=1, pady=7)
            def choose(k=key):
                value = colorchooser.askcolor(variables[k].get(), parent=dialog)[1]
                if value:
                    variables[k].set(value)
            ttk.Button(colors, text="색상 선택", command=choose).grid(row=index, column=2, padx=8)
        ttk.Label(users, text="한 줄에 ID=이름. 통계와 필터는 원본 ID를 유지합니다.\n그래프에서 사용자 이름을 클릭해 바로 편집할 수도 있습니다.").pack(anchor="w")
        names = tk.Text(users, width=52, height=8, wrap="none")
        names.pack(fill="both", expand=True, pady=9)
        names.insert("1.0", "\n".join(uid + "=" + name for uid, name in self.options["userNames"].items()))
        ttk.Label(users, text="사용자별 색상: 한 줄에 ID=#RRGGBB. 비우면 자동 색상.").pack(anchor="w")
        user_colors = tk.Text(users, width=52, height=5, wrap="none")
        user_colors.pack(fill="both", expand=True, pady=9)
        user_colors.insert("1.0", "\n".join(uid + "=" + value for uid, value in self.options["userColors"].items()))
        ttk.Label(outer, text="원 배치 공간이 부족하면 캔버스 자동 확장 · PNG는 현재 페이지 저장\n설정은 이 컴퓨터와 통합 JSON에 보관합니다.").pack(anchor="w", pady=10)
        controls = ttk.Frame(outer)
        controls.pack(fill="x")
        def reset():
            for key, value in appearance_options()["chartStyle"].items():
                variables[key].set(str(value))
        def apply():
            try:
                raw = {key: var.get() for key, var in variables.items()}
                clean = appearance_options(dict(chartStyle=raw))["chartStyle"]
                for key, (_default, low, high) in STYLE_NUMBERS.items():
                    value = float(raw[key])
                    if not low <= value <= high or key in ("pieColumns", "piesPerPage") and value != int(value):
                        raise ValueError(f"{STYLE_LABELS[key]}: {low}–{high} 범위로 입력하세요.")
                if any(clean[key] != raw[key] for key in STYLE_COLORS):
                    raise ValueError("색상을 #RRGGBB로 입력하세요.")
                config = dict(chartStyle=clean, userNames=parse_mapping(names.get("1.0", "end")),
                              userColors=parse_mapping(user_colors.get("1.0", "end"), colors=True))
            except ValueError as exc:
                self.messagebox.showerror("이름·그래프 설정", str(exc), parent=dialog)
                return
            dialog.destroy()
            self.apply_appearance(config)
        ttk.Button(controls, text="그래프 기본값", command=reset).pack(side="left")
        ttk.Button(controls, text="적용·저장", command=apply).pack(side="right")
        ttk.Button(controls, text="취소", command=dialog.destroy).pack(side="right", padx=8)
        dialog.grab_set()

    def settings(self):
        if self.busy:
            return
        tk, ttk = self.tk, self.ttk
        dialog = tk.Toplevel(self.root)
        dialog.title("집계 설정")
        dialog.transient(self.root)
        outer = ttk.Frame(dialog, padding=20)
        outer.pack(fill="both", expand=True)
        interval = tk.StringVar(value=str(self.options["interval"]))
        offset = tk.StringVar(value="한국시간" if self.options["offset"] == 540 else "UTC")
        started = tk.StringVar(value="스냅샷" if self.options["startedMode"] == "snapshot" else "시작 이벤트")
        denominator = tk.StringVar(value="관측시간" if self.options["denominator"] == "observed" else "조회기간 전체·하한")
        clock, replace = tk.BooleanVar(value=self.options["nodeClock"]), tk.BooleanVar(value=self.replace)
        for index, (label, var, values) in enumerate([
            ("기록 주기 (분)", interval, None), ("로그 시간대", offset, ["한국시간", "UTC"]),
            ("queue_started 방식", started, ["스냅샷", "시작 이벤트"]),
            ("사용률 분모", denominator, ["관측시간", "조회기간 전체·하한"])]):
            ttk.Label(outer, text=label).grid(row=index, column=0, sticky="w", padx=(0, 20), pady=8)
            widget = ttk.Combobox(outer, textvariable=var, values=values, state="readonly") if values else ttk.Entry(outer, textvariable=var)
            widget.grid(row=index, column=1, sticky="ew", pady=8)
        ttk.Checkbutton(outer, text="node_cpu 시각으로 빈 작업 스냅샷 확인", variable=clock).grid(row=4, columnspan=2, sticky="w", pady=10)
        ttk.Checkbutton(outer, text="새 파일을 읽을 때 기존 로그 전체 교체", variable=replace).grid(row=5, columnspan=2, sticky="w", pady=10)
        ttk.Label(outer, text="종료·작업 시간은 기록 간격에 따른 추정입니다.\n사용자 기록과 노드 기록이 함께 수집될 때만 시각 연동을 사용하세요.").grid(row=6, columnspan=2, sticky="w", pady=10)
        def apply():
            try:
                value = float(interval.get())
                if not 1 <= value <= 1440:
                    raise ValueError()
            except ValueError:
                self.messagebox.showerror("기록 주기", "1~1440분의 숫자를 입력하세요.", parent=dialog)
                return
            old_offset = self.options["offset"]
            self.options.update(interval=value, offset=540 if offset.get() == "한국시간" else 0,
                                startedMode="snapshot" if started.get() == "스냅샷" else "event",
                                denominator="observed" if denominator.get() == "관측시간" else "calendar",
                                nodeClock=clock.get())
            self.replace = replace.get()
            dialog.destroy()
            if old_offset != self.options["offset"]:
                self.update_range()
            if self.data:
                self.apply_filters()
        ttk.Button(outer, text="적용", command=apply).grid(row=7, column=1, sticky="e", pady=(10, 0))
        dialog.grab_set()

    def close(self):
        self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="HPC 사용량 모니터 · Tkinter/Matplotlib X11 GUI")
    parser.add_argument("files", nargs="*", help="시작할 때 읽을 TSV/LOG/JSON 파일")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--font-size", type=int, default=10)
    parser.add_argument("--self-test", action="store_true", help="X11 없이 집계·PNG 렌더링 검증")
    args = parser.parse_args(argv)
    if not 8 <= args.font_size <= 24:
        parser.error("--font-size는 8~24로 지정하세요.")
    if args.self_test:
        data = analyze(demo_store())
        assert data["summary"]["coreHours"] == 35968
        figure = figure_for(data)
        stream = io.BytesIO()
        figure.savefig(stream, format="png")
        assert stream.getvalue().startswith(b"\x89PNG")
        print("PASS: Python X11 engine + Matplotlib PNG rendering (headless)")
        return 0
    try:
        import tkinter as tk
        root = tk.Tk()
    except (ImportError, RuntimeError) as exc:
        parser.exit(2, f"Tkinter를 사용할 수 없습니다: {exc}\nTkinter와 X11 환경을 확인하세요.\n")
    except Exception as exc:
        parser.exit(2, f"X11 화면을 열 수 없습니다: {exc}\nSSH X11 포워딩과 DISPLAY를 확인하세요. 웹 GUI도 사용할 수 있습니다.\n")
    try:
        store = demo_store() if args.demo else LogStore()
        if args.files:
            store = LogStore()
            for path in args.files:
                store.load_path(path)
        app = MonitorGUI(root, store, args)
        root.mainloop()
    except ImportError as exc:
        root.destroy()
        parser.exit(2, f"GUI 패키지를 확인하세요: {exc}\npython3 -m pip install -r requirements.txt\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
