#!/usr/bin/env python3
"""Native Tkinter/Matplotlib X11 GUI using the shared Python engine."""
from __future__ import annotations

import argparse
import io
import json
import queue
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hpc_monitor_core import DEFAULTS, VERSION, LogStore, analyze, demo_store, iso, parse_time, table_csv
from hpc_monitor_plot import PLOT_NAMES, VIEWS, figure_for

TABLES = {
    "overview": [("t", "구간 시작", "time"), ("submitted", "제출", "num"), ("started", "시작", "num"), ("running", "실행", "num"), ("jobHours", "작업 시간 h", "num"), ("coreHours", "코어 시간 core·h", "num"), ("allocPct", "코어 할당률", "pct"), ("busyPct", "작업 점유율", "pct"), ("gpuUtil", "GPU 사용률", "pct")],
    "nodes": [("name", "노드"), ("groups", "partition"), ("state", "마지막 state"), ("cores", "전체 코어", "num"), ("jobs", "작업 수", "num"), ("jobHours", "작업 시간 h", "num"), ("coreHours", "코어 시간 core·h", "num"), ("busyHours", "점유 시간 h", "num"), ("busyPct", "작업 점유율", "pct"), ("allocPct", "코어 할당률", "pct"), ("coverage", "관측률", "pct"), ("memPct", "메모리 할당률", "pct"), ("loadPct", "CPU load 비율", "pct")],
    "groups": [("name", "partition"), ("nodeCount", "노드 수", "num"), ("jobs", "작업 수", "num"), ("jobHours", "작업 시간 h", "num"), ("coreHours", "코어 시간 core·h", "num"), ("busyPct", "작업 점유율", "pct"), ("allocPct", "코어 할당률", "pct"), ("coverage", "관측률", "pct")],
    "users": [("name", "사용자"), ("jobs", "작업 수", "num"), ("submitted", "제출", "num"), ("started", "시작", "num"), ("jobHours", "작업 시간 h", "num"), ("coreHours", "코어 시간 core·h", "num"), ("contribution", "전체 코어 중 기여율", "pct"), ("activityPct", "활동시간 비율", "pct"), ("gpuHours", "GPU 점유 GPU·h", "num"), ("gpuMemHours", "GPU 메모리 MB·h", "num")],
    "queue": [("id", "작업 ID"), ("user", "사용자"), ("partition", "partition"), ("nodes", "노드"), ("submit", "제출 시각", "time"), ("start", "시작 시각", "time"), ("end", "종료 추정", "time"), ("wallHours", "작업 시간 h", "num"), ("waitMin", "시작 전 대기 min", "num"), ("status", "관측 상태"), ("state", "원본 state")],
    "gpu": [("id", "GPU"), ("name", "모델"), ("util", "평균 사용률", "pct"), ("memPct", "평균 메모리 점유율", "pct"), ("tempMax", "최대 온도 °C", "num"), ("power", "평균 전력 W", "num"), ("energyWh", "추정 에너지 Wh", "num"), ("coverage", "관측률", "pct"), ("users", "관측 사용자")],
    "data": [("name", "파일명"), ("types", "로그 종류"), ("rows", "새 기록", "num"), ("duplicates", "중복", "num"), ("invalid", "제외한 행", "num")],
}
DATA_KEYS = dict(overview="series", nodes="nodes", groups="groups", users="users", queue="jobs", gpu="gpu")


def fmt(value, kind="", offset=540):
    if value is None:
        return "—"
    if kind == "time":
        return iso(value, offset).replace("T", " ")
    if isinstance(value, (float, int)):
        text = f"{value:,.2f}".rstrip("0").rstrip(".") if not float(value).is_integer() else f"{value:,.0f}"
        return text + ("%" if kind == "pct" else "")
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


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
        self.options, self.replace, self.dark = dict(DEFAULTS), False, False
        self.filter_selection = {key: [] for key in ("groups", "nodes", "users")}
        self.page, self.sort_key, self.sort_reverse = 0, None, False
        self.rows, self.visible_rows, self.figure, self.canvas, self.toolbar = [], [], None, None, None
        self.closed = False
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
        for label, command in [("로그 불러오기", self.load_files), ("예제", self.demo), ("설정", self.settings), ("통합 JSON", self.save_session), ("집계 CSV", self.save_csv), ("그래프 PNG", self.save_png)]:
            button = ttk.Button(top, text=label, command=command)
            button.pack(side="right", padx=3)
            self.controls.append(button)
        filters = ttk.LabelFrame(body, text="조회 조건", padding=10)
        filters.pack(fill="x", pady=(17, 8))
        self.from_var, self.to_var = tk.StringVar(), tk.StringVar()
        for column, (label, variable) in enumerate((("조회 시작", self.from_var), ("조회 종료 · 미포함", self.to_var))):
            ttk.Label(filters, text=label).grid(row=0, column=column, sticky="w")
            entry = ttk.Entry(filters, textvariable=variable, width=21)
            entry.grid(row=1, column=column, padx=(0, 9), sticky="ew")
            entry.bind("<Return>", lambda _e: self.apply_filters())
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
        for column in range(5):
            filters.columnconfigure(column, weight=1)
        self.caption = ttk.Label(body, text="탭 구분 파일을 선택하거나 예제로 시작하세요.")
        self.caption.pack(fill="x", pady=(0, 8))
        metrics = ttk.Frame(body)
        metrics.pack(fill="x", pady=(0, 12))
        self.metric_labels = {}
        for column, (key, label) in enumerate((("running", "관측된 실행 작업"), ("coreHours", "코어 시간 · core·h"),
                                                ("busyPct", "작업 점유율 · 추정"), ("coverage", "기록 관측률"))):
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
        self.notebook.add(self.help_frame, text="집계 기준·검증")
        chart_tools = ttk.Frame(self.chart_frame, padding=5)
        chart_tools.pack(fill="x")
        ttk.Label(chart_tools, text="그래프 선택").pack(side="left", padx=5)
        self.plot_var = tk.StringVar(value=PLOT_NAMES["overview"])
        self.plot_combo = ttk.Combobox(chart_tools, textvariable=self.plot_var, state="readonly", width=30)
        self.plot_combo.pack(side="left", padx=6)
        self.plot_combo.bind("<<ComboboxSelected>>", lambda _e: self.select_plot())
        self.dark_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(chart_tools, text="다크 모드", variable=self.dark_var, command=self.toggle_theme).pack(side="right")
        self.canvas_frame = ttk.Frame(self.chart_frame)
        self.canvas_frame.pack(fill="both", expand=True)
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
        self.help_text.pack(fill="both", expand=True)
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
        if self.store.describe()["min"] is not None and self.store.describe()["min"] != self.store.describe()["max"]:
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

    def apply_filters(self):
        if self.busy:
            return
        a = parse_time(self.from_var.get(), self.options["offset"])
        b = parse_time(self.to_var.get(), self.options["offset"])
        if a is None or b is None or b <= a:
            self.messagebox.showinfo("조회 기간", "서로 다른 시각의 기록을 두 개 이상 불러오고 시작·종료 시각을 확인하세요.", parent=self.root)
            return
        self.options.update(self.filter_selection)
        self.options.update({"from": a, "to": b, "bucket": {"시간별": "hour", "일별": "day", "주별": "week", "월별": "month"}[self.bucket_var.get()]})
        options = dict(self.options)
        self.work(lambda: analyze(self.store, options), self.accept)

    def accept(self, data):
        self.data = data
        self.options["bucket"] = data["settings"]["bucket"]
        self.bucket_var.set({"hour": "시간별", "day": "일별", "week": "주별", "month": "월별"}[self.options["bucket"]])
        self.status.set("집계 완료 · 표 행을 더블클릭하면 전체 내용을 확인할 수 있습니다.")
        self.render()

    def load_files(self):
        if self.busy:
            return
        paths = self.filedialog.askopenfilenames(parent=self.root, title="HPC 로그 파일 선택",
                    filetypes=[("HPC 로그", "*.tsv *.log *.txt *.csv *.json"), ("모든 파일", "*")])
        if not paths:
            return
        old, replace = self.store, self.replace or self.store.demo
        def task():
            store = LogStore() if replace else old
            store.imported_settings = None
            for path in paths:
                try:
                    store.load_path(path)
                except (ValueError, OSError, UnicodeError) as exc:
                    store.issue(Path(path).name, 0, str(exc))
                    store.files.append(dict(name=Path(path).name, rows=0, duplicates=0, invalid=1, types=[]))
            return store
        def done(store):
            self.store = store
            if store.imported_settings:
                self.options.update({k: v for k, v in store.imported_settings.items() if k in DEFAULTS})
            self.update_range()
            self.filter_selection = {k: [] for k in self.filter_selection}
            self.update_filter_labels()
            if store.describe()["min"] == store.describe()["max"]:
                self.data = dict(empty=True, meta=store.describe(self.options), settings=dict(self.options),
                                 error="기록 시각이 하나뿐입니다. 다음 시각의 로그를 추가하세요.")
                self.switch_view("data")
                self.render()
            else:
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
            box.insert("end", value)
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
        self.title.configure(text=VIEWS[view][0] + (" · 예제 데이터" if self.store.demo else ""))
        self.plot_combo.configure(values=[PLOT_NAMES[k] for k in VIEWS[view][1]])
        self.plot_var.set(PLOT_NAMES[self.plot])
        self.search_var.set("")
        self.sort_key = None
        if draw:
            self.render()

    def select_plot(self):
        self.plot = next(k for k in VIEWS[self.view][1] if PLOT_NAMES[k] == self.plot_var.get())
        self.render_figure()

    def render(self):
        meta = self.store.describe(self.options)
        data = self.data
        if data and not data.get("empty"):
            for key, label in self.metric_labels.items():
                label.configure(text=fmt(data["summary"][key], "pct" if key.endswith("Pct") or key == "coverage" else "num"))
            tz = "한국시간" if self.options["offset"] == 540 else "UTC"
            self.caption.configure(text=f"{self.from_var.get()} — {self.to_var.get()} · {tz} · {sum(meta['counts'].values()):,}개 기록 · 확인 사항 {meta['issueTotal']:,}개")
        else:
            self.caption.configure(text=data.get("error", "로그를 불러오세요.") if data else "탭 구분 로그 파일을 선택하거나 예제로 시작하세요.")
            for label in self.metric_labels.values():
                label.configure(text="—")
        self.rows = meta["files"] if self.view == "data" else data.get(DATA_KEYS[self.view], []) if data and not data.get("empty") else []
        self.columns = TABLES[self.view]
        self.page = 0
        self.update_table()
        notices = data.get("notices", []) if data else []
        help_text = "\n\n".join(notices) + "\n\n입력 파일 확인 사항\n" + "\n".join(f"{r['file']}:{r['line']}  {r['message']}" for r in meta["issues"])
        help_text += "\n\n시간은 추정값이며 종료 소멸은 성공·실패를 뜻하지 않습니다.\n로그는 자동 저장되지 않습니다. 다시 열려면 통합 JSON을 저장하세요.\n노드 목록은 업로드한 로그에 등장한 노드 기준입니다."
        self.help_text.configure(state="normal")
        self.help_text.delete("1.0", "end")
        self.help_text.insert("1.0", help_text)
        self.help_text.configure(state="disabled")
        self.render_figure()

    def render_figure(self):
        if self.canvas:
            self.canvas.get_tk_widget().destroy()
        if self.toolbar:
            self.toolbar.destroy()
        self.figure = figure_for(self.data, self.view, self.plot, self.dark_var.get())
        self.canvas = self.Canvas(self.figure, master=self.canvas_frame)
        self.toolbar = self.Toolbar(self.canvas, self.canvas_frame, pack_toolbar=False)
        self.toolbar.update()
        self.toolbar.pack(side="bottom", fill="x")
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.draw_idle()

    def toggle_theme(self):
        dark = self.dark_var.get()
        bg, panel, fg, field = ("#101e29", "#172b39", "#dfeaf1", "#1b3040") if dark else ("#f4f6f8", "#ffffff", "#172a3a", "#ffffff")
        self.root.configure(background=bg)
        for widget in (".", "TFrame", "TLabel", "TLabelframe", "TLabelframe.Label", "TCheckbutton", "TNotebook"):
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
        rows = [r for r in self.rows if not query or any(query in str(r.get(k, "")).lower() for k, *_ in self.columns)]
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
            self.tree.insert("", "end", iid=str(index), values=[fmt(row.get(k), tail[0] if tail else "", self.options["offset"]) for k, _label, *tail in self.columns])
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
            Path(path).write_text(table_csv(self.visible_rows, self.columns, self.options["offset"]), encoding="utf-8", newline="")
            self.status.set("현재 필터·검색 결과 CSV 저장 완료")

    def save_png(self):
        path = self.filedialog.asksaveasfilename(parent=self.root, defaultextension=".png", initialfile="HPC_" + self.plot + ".png")
        if path and self.figure:
            self.figure.savefig(path, dpi=160, facecolor=self.figure.get_facecolor())
            self.status.set("그래프 PNG 저장 완료")

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
