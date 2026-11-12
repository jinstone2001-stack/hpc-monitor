"""Matplotlib figures shared by X11 rendering and headless validation."""
from __future__ import annotations

import math
from colorsys import hls_to_rgb

VIEWS = {
    "overview": ("전체 현황", ["overview", "jobs", "rates", "node_heat", "user_core"]),
    "nodes": ("노드별", ["node_heat", "node_pies", "node_core", "node_busy", "node_wait"]),
    "groups": ("노드 그룹별", ["group_core", "group_rate", "group_wait"]),
    "users": ("사용자별", ["user_core", "user_time", "user_gpu"]),
    "queue": ("작업·대기열", ["pending", "waiting", "node_wait", "group_wait", "reasons", "timeline"]),
    "gpu": ("GPU", ["gpu_heat", "gpu_rate", "user_gpu", "gpu_power", "gpu_temp"]),
    "data": ("로그·검증", ["quality"]),
}
PLOT_NAMES = dict(overview="주요 지표 모아보기", jobs="날짜별 작업 수", rates="할당·점유 추이",
                  node_heat="노드별 할당률 지도", user_core="사용자별 코어 시간",
                  node_pies="노드별 사용률 원그래프", node_wait="노드별 평균·최대 시작 대기",
                  group_wait="그룹별 평균·최대 시작 대기",
                  node_core="노드별 코어 시간", node_busy="노드별 작업 점유율",
                  group_core="그룹별 코어 시간", group_rate="그룹별 코어 할당률",
                  user_time="사용자별 작업 시간", user_gpu="사용자별 GPU 점유",
                  pending="대기 작업 수 추이", waiting="시작 전 대기시간", reasons="대기 사유",
                  timeline="작업 실행 구간", gpu_heat="GPU별 사용률 지도",
                  gpu_rate="GPU 사용률 추이", gpu_power="GPU 평균 전력", gpu_temp="GPU 최대 온도",
                  quality="입력 로그 현황")


def figure_for(data, view="overview", plot=None, dark=False):
    from matplotlib.figure import Figure
    from matplotlib.ticker import MaxNLocator
    import numpy as np
    from hpc_monitor_core import iso
    ink, muted = ("#dfeaf1", "#95afbf") if dark else ("#172a3a", "#6c7c89")
    bg, line = ("#172b39", "#304958") if dark else ("#ffffff", "#e3e9ee")
    teal, blue, amber = ("#63c4c3", "#7ba7d6", "#e1ad67") if dark else ("#087f8c", "#5586bb", "#d8a154")
    fig = Figure(figsize=(11.5, 5.7), dpi=100, facecolor=bg, layout="constrained")
    plot = plot or VIEWS[view][1][0]
    meta = data.get("meta", {}) if data else {}
    offset = data.get("settings", {}).get("offset", 540) if data else 540
    series = data.get("series", []) if data else []
    users = sorted(meta.get("users", []) or [r["name"] for r in (data or {}).get("users", [])])
    user_colors = {name: hls_to_rgb(i * .61803398875 % 1, .47, .6) for i, name in enumerate(users)}

    def row_label(row, label="name"):
        purpose = row.get("purpose", "").replace("양자계산", "Quantum").replace("고체·MD", "Solid / MD").replace("미지정", "Unassigned")
        return str(row[label]) + (" · " + purpose if purpose else "")

    def setup(ax, title, unit=None):
        ax.set_facecolor(bg)
        ax.set_title(title, loc="left", fontsize=11, fontweight="bold", color=ink, pad=13)
        ax.tick_params(colors=muted, labelsize=8)
        ax.yaxis.label.set_color(muted)
        ax.xaxis.label.set_color(muted)
        for spine in ax.spines.values():
            spine.set_color(line)
        ax.grid(axis="y", color=line, linewidth=.7)
        ax.set_axisbelow(True)
        if unit:
            ax.set_ylabel(unit, fontsize=9)

    def empty(ax, text="No records for this selection"):
        ax.text(.5, .5, text, transform=ax.transAxes, ha="center", va="center", color=muted, fontsize=11)
        ax.set_xticks([])
        ax.set_yticks([])

    def dates(ax):
        if series:
            positions = list(range(0, len(series), max(1, math.ceil(len(series) / 8))))
            ax.set_xticks(positions, [iso(series[i]["t"], offset)[5:10] for i in positions])

    def bars(ax, rows, key, title, unit, label="name", by_user=False):
        setup(ax, title, unit)
        values = sorted((r for r in rows if r.get(key) is not None), key=lambda r: -r[key])[:15]
        if not values:
            return empty(ax)
        values.reverse()
        rects = ax.barh([row_label(r, label) for r in values], [r[key] for r in values],
                        color=[user_colors[r[label]] for r in values] if by_user else teal, height=.6)
        ax.grid(axis="x", color=line)
        ax.grid(axis="y", visible=False)
        ax.set_ylabel("")
        ax.set_xlabel(unit, fontsize=9)
        for rect, row in zip(rects, values):
            rect.hpc_label = str(row[label])
            rect.set_picker(True)
        ax.margins(x=.12)

    def jobs(ax):
        setup(ax, "Observed jobs by period", "jobs")
        if not series or not any(r["running"] is not None or r["started"] is not None for r in series):
            return empty(ax)
        x = np.arange(len(series))
        for shift, key, label, color in [(-.25, "submitted", "Submitted", blue), (0, "started", "Started", teal), (.25, "running", "Running", amber)]:
            ax.bar(x + shift, [np.nan if r[key] is None else r[key] for r in series], width=.23, label=label, color=color)
        dates(ax)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax.legend(fontsize=8, frameon=False, labelcolor=ink)

    def trend(ax, keys, title, unit="%", fixed=True):
        setup(ax, title, unit)
        if not series or not any(r.get(key) is not None for r in series for key, _, _ in keys):
            return empty(ax)
        for key, label, color in keys:
            ax.plot(range(len(series)), [np.nan if r.get(key) is None else r[key] / 60 if key.startswith("wait") else r[key] for r in series],
                    color=color, marker="." if len(series) < 50 else None, linewidth=1.6, label=label)
        dates(ax)
        if fixed:
            ax.set_ylim(0, max(100, max((r.get(k) or 0 for r in series for k, _, _ in keys), default=100)))
        ax.legend(fontsize=8, frameon=False, labelcolor=ink)

    def heat(ax, gpu=False):
        records = data.get("gpuHeat" if gpu else "heat", [])
        names = [r["id"] if gpu else r["name"] for r in data.get("gpu" if gpu else "nodes", [])][:80]
        setup(ax, "GPU utilization (%)" if gpu else "Node core allocation (%)")
        if not names or not records or not series:
            return empty(ax)
        matrix = np.full((len(names), len(series)), np.nan)
        ri, ci = {v: i for i, v in enumerate(names)}, {r["t"]: i for i, r in enumerate(series)}
        for r in records:
            if r["node"] in ri and r["t"] in ci and r["value"] is not None:
                matrix[ri[r["node"]], ci[r["t"]]] = r["value"]
        from matplotlib import colormaps
        cmap = colormaps["YlGnBu"].copy()
        cmap.set_bad("#52606b" if dark else "#e3e9ee")
        image = ax.imshow(np.ma.masked_invalid(matrix), aspect="auto", cmap=cmap, vmin=0, vmax=100, interpolation="nearest")
        label = "id" if gpu else "name"
        lookup = {r[label]: r for r in data.get("gpu" if gpu else "nodes", [])}
        ax.set_yticks(range(len(names)), [row_label(lookup[name], label) for name in names])
        dates(ax)
        ax.grid(False)
        fig.colorbar(image, ax=ax, fraction=.025, pad=.02).ax.tick_params(labelsize=8, colors=muted)
        ax.set_xlabel("Gray = no observations; up to 80 rows shown. Use node filters for more.", fontsize=8)

    def waiting_by(ax, table):
        setup(ax, "Wait before job start by " + ("node" if table == "nodes" else "partition"), "min")
        rows = sorted((r for r in data.get(table, []) if r["waitMax"] is not None), key=lambda r: -r["waitMax"])[:15]
        if not rows:
            return empty(ax, "No started jobs with known waits for this selection")
        rows.reverse()
        y = np.arange(len(rows))
        ax.barh(y + .18, [r["waitMean"] / 60 for r in rows], height=.32, color=blue, label="Mean")
        ax.barh(y - .18, [r["waitMax"] / 60 for r in rows], height=.32, color=amber, label="Maximum")
        ax.set_yticks(y, [row_label(r) for r in rows])
        ax.set_ylabel("")
        ax.set_xlabel("Minutes · unique started jobs; top 15 by maximum wait", fontsize=8)
        ax.legend(fontsize=8, frameon=False, labelcolor=ink)

    def node_pies():
        rows = data.get("nodes", [])
        if not rows:
            return empty(fig.add_subplot())
        columns = min(4, len(rows))
        count = math.ceil(len(rows) / columns)
        fig.set_size_inches(11.5, max(5.7, count * 2.5))
        for index, row in enumerate(rows, 1):
            ax = fig.add_subplot(count, columns, index)
            value = row["allocPct"]
            known = value is not None
            clamped = max(0, min(100, value or 0))
            color = hls_to_rgb((220 + 140 * clamped / 100) / 360, .47, .7)
            ax.pie([clamped, 100 - clamped] if known else [0, 100],
                   colors=[color, line], startangle=90, counterclock=False,
                   wedgeprops=dict(width=.3, edgecolor=bg))
            ax.text(0, 0, f"{value:.1f}%" if known else "No data", ha="center", va="center", color=color if known else muted, fontsize=15)
            ax.set_title(row_label(row), color=ink, fontsize=10)
            ax.text(.5, -.07, "Coverage: " + (f"{row['coverage']:.1f}%" if row["coverage"] is not None else "unknown"),
                    transform=ax.transAxes, ha="center", color=muted, fontsize=8)
        fig.suptitle("Node core allocation · blue = low, red = high", color=ink, fontsize=12)

    def draw(ax, name):
        if name == "jobs":
            return jobs(ax)
        if name == "rates":
            return trend(ax, [("allocPct", "Core allocation", teal), ("busyPct", "Node busy", blue)], "Allocation and busy time (observed)")
        if name == "gpu_rate":
            return trend(ax, [("gpuUtil", "Whole device", teal)], "GPU utilization")
        if name in ("node_heat", "gpu_heat"):
            return heat(ax, name == "gpu_heat")
        mappings = {
            "user_core": ("users", "coreHours", "CPU allocation by user", "core h", "name"),
            "user_time": ("users", "jobHours", "Accumulated job time (estimated)", "h", "name"),
            "user_gpu": ("users", "gpuHours", "GPU process occupancy by user (estimated)", "GPU h", "name"),
            "node_core": ("nodes", "coreHours", "CPU allocation by node", "core h", "name"),
            "node_busy": ("nodes", "busyPct", "Node busy time (estimated)", "%", "name"),
            "group_core": ("groups", "coreHours", "CPU allocation by partition", "core h", "name"),
            "group_rate": ("groups", "allocPct", "Core allocation by partition", "%", "name"),
            "reasons": ("reasons", "count", "Latest pending reasons", "jobs", "name"),
            "gpu_power": ("gpu", "power", "Mean GPU power", "W", "id"),
            "gpu_temp": ("gpu", "tempMax", "Peak observed GPU temperature", "deg C", "id"),
        }
        if name in mappings:
            table, key, title, unit, label = mappings[name]
            return bars(ax, data.get(table, []), key, title, unit, label, table == "users")
        if name == "waiting":
            return trend(ax, [("waitMean", "Mean wait", blue), ("waitMax", "Maximum wait", amber),
                              ("waitMedian", "Median wait", teal)], "Wait before job start", "min", False)
        if name in ("node_wait", "group_wait"):
            return waiting_by(ax, "nodes" if name == "node_wait" else "groups")
        if name == "pending":
            setup(ax, "Peak observed pending jobs per period", "jobs")
            if not series or all(r["pendingMax"] is None for r in series):
                return empty(ax)
            ax.bar(range(len(series)), [r["pendingMax"] if r["pendingMax"] is not None else np.nan for r in series], color=amber)
            dates(ax)
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            return
        if name == "timeline":
            setup(ax, "Job execution intervals (estimated)", "job / user")
            rows = [r for r in data.get("jobs", []) if r["ranges"]][:25]
            if not rows:
                return empty(ax)
            left = data["from"]
            for index, row in enumerate(rows):
                ax.broken_barh([((a - left) / 3600000, (b - a) / 3600000) for a, b in row["ranges"]],
                               (index - .32, .64), facecolors=teal)
            ax.set_yticks(range(len(rows)), [r["id"] + " / " + r["user"] for r in rows])
            ax.invert_yaxis()
            ax.set_xlim(0, (data["to"] - left) / 3600000)
            ax.set_xlabel("Hours since " + iso(left, offset).replace("T", " "), fontsize=8)
            return
        if name == "quality":
            setup(ax, "Imported records by log type", "rows")
            counts = meta.get("counts", {})
            if not any(counts.values()):
                return empty(ax, "Load TSV logs or use the built-in demo")
            ax.barh(list(counts), list(counts.values()), color=teal)
            return
        empty(ax)

    if not data or data.get("empty"):
        ax = fig.add_subplot()
        setup(ax, "HPC Monitor")
        empty(ax, data.get("error", "Load log files or use the built-in demo") if data else "Load log files or use the built-in demo")
    elif plot == "node_pies":
        node_pies()
    elif plot == "overview":
        for index, name in enumerate(("jobs", "rates", "user_core", "node_heat"), 1):
            draw(fig.add_subplot(2, 2, index), name)
    else:
        draw(fig.add_subplot(), plot)
    return fig
