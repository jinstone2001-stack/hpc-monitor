"""Matplotlib figures shared by X11 rendering and headless validation."""
from __future__ import annotations

import math
from colorsys import hls_to_rgb

VIEWS = {
    "overview": ("전체 현황", ["overview", "jobs", "rates", "usage_pie", "user_core"]),
    "nodes": ("노드별", ["node_heat", "node_pies", "node_core", "node_busy", "node_wait"]),
    "groups": ("노드 그룹별", ["group_pies", "group_core", "group_rate", "group_wait"]),
    "users": ("사용자별", ["user_core", "user_time", "user_gpu"]),
    "queue": ("작업·대기열", ["pending", "waiting", "node_wait", "group_wait", "reasons", "timeline"]),
    "gpu": ("GPU", ["gpu_latest", "gpu_heat", "gpu_rate", "user_gpu", "gpu_user_memory", "gpu_processes", "gpu_memory", "gpu_memory_used", "gpu_power_latest", "gpu_temp_latest", "gpu_power", "gpu_temp"]),
    "data": ("로그·검증", ["quality"]),
}
PLOT_NAMES = dict(overview="주요 지표 모아보기", jobs="날짜별 작업 수", rates="할당·점유 추이",
                  usage_pie="전체 사용률 원그래프", group_pies="그룹별 사용률 원그래프",
                  node_heat="노드별 할당률 지도", user_core="사용자별 코어 시간",
                  node_pies="노드별 사용률 원그래프", node_wait="노드별 평균·최대 시작 대기",
                  group_wait="그룹별 평균·최대 시작 대기",
                  node_core="노드별 코어 시간", node_busy="노드별 작업 점유율",
                  group_core="그룹별 코어 시간", group_rate="그룹별 코어 할당률",
                  user_time="사용자별 작업 시간", user_gpu="사용자별 GPU 점유",
                  pending="대기 작업 수 추이", waiting="시작 전 대기시간", reasons="대기 사유",
                  timeline="작업 실행 구간", gpu_heat="GPU별 사용률 지도",
                  gpu_latest="GPU 마지막 관측 사용률",
                  gpu_user_memory="사용자별 마지막 GPU 메모리", gpu_memory="GPU 마지막 메모리 점유율",
                  gpu_processes="사용자별 마지막 GPU 프로세스", gpu_power_latest="GPU 마지막 전력", gpu_temp_latest="GPU 마지막 온도",
                  gpu_memory_used="GPU 마지막 사용 메모리 MB",
                  gpu_rate="GPU 사용률 추이", gpu_power="GPU 평균 전력", gpu_temp="GPU 최대 온도",
                  quality="입력 로그 현황")


def figure_for(data, view="overview", plot=None, dark=False, pie_max=None, pie_page=0, settings=None):
    from matplotlib.figure import Figure
    from matplotlib.ticker import MaxNLocator
    import numpy as np
    from hpc_monitor_core import appearance_options, iso, number
    config = appearance_options(settings if settings is not None else (data or {}).get("settings"))
    style, aliases = config["chartStyle"], config["userNames"]
    ink, muted = ("#dfeaf1", "#95afbf") if dark else ("#172a3a", "#6c7c89")
    bg, line = ("#172b39", "#304958") if dark else ("#ffffff", "#e3e9ee")
    teal, blue, amber = ("#63c4c3", "#7ba7d6", "#e1ad67") if dark else ("#087f8c", "#5586bb", "#d8a154")
    teal, blue, amber = (style[k] for k in ("chartColor", "secondaryColor", "warningColor"))
    fig = Figure(figsize=(style["chartWidth"] / 100, style["chartHeight"] / 100), dpi=100, facecolor=bg, layout="constrained")
    plot = plot or VIEWS[view][1][0]
    meta = data.get("meta", {}) if data else {}
    offset = data.get("settings", {}).get("offset", 540) if data else 540
    series = data.get("series", []) if data else []
    maximum = number(pie_max if pie_max is not None else (data or {}).get("settings", {}).get("pieMax", 100))
    maximum = maximum if maximum is not None and 0 < maximum <= 100 else 100
    users = sorted(meta.get("users", []) or [r["name"] for r in (data or {}).get("users", [])])
    user_colors = {name: config["userColors"].get(name, hls_to_rgb(i * .61803398875 % 1, .47, .6)) for i, name in enumerate(users)}

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
        rects = ax.barh(range(len(values)), [r[key] for r in values],
                        color=[user_colors[r[label]] for r in values] if by_user else teal, height=.6)
        ax.set_yticks(range(len(values)), [aliases.get(r[label], r[label]) if by_user else row_label(r, label) for r in values])
        ax.grid(axis="x", color=line)
        ax.grid(axis="y", visible=False)
        ax.set_ylabel("")
        ax.set_xlabel(unit, fontsize=9)
        for rect, row in zip(rects, values):
            rect.hpc_label = str(row[label])
            rect.set_picker(True)
            if by_user:
                rect.hpc_user = str(row[label])
        if by_user:
            for text, row in zip(ax.get_yticklabels(), values):
                text.hpc_user = str(row[label])
                text.set_picker(True)
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
        from matplotlib.colors import LinearSegmentedColormap
        cmap = LinearSegmentedColormap.from_list("hpc", [style["pieLowColor"], style["pieHighColor"]]).with_extremes(bad="#52606b" if dark else "#e3e9ee")
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

    def allocation_pie(ax, row, title):
        value = row["allocPct"]
        known = value is not None
        clamped = max(0, min(1, (value or 0) / maximum))
        from matplotlib.colors import to_rgb
        low, high = to_rgb(style["pieLowColor"]), to_rgb(style["pieHighColor"])
        fraction = max(0, min(1, (value or 0) / 100))
        color = tuple(a + (b - a) * fraction for a, b in zip(low, high))
        ax.set_facecolor(bg)
        ax.pie([clamped, 1 - clamped] if known else [0, 1],
               colors=[color, style["pieEmptyColor"]], startangle=90, counterclock=False,
               wedgeprops=dict(width=1 - style["pieHole"] / 100, edgecolor=bg))
        text = ax.text(0, 0, f"{value:.1f}%" if known else "No data", ha="center", va="center", color=color if known else muted)
        from matplotlib.patheffects import withStroke
        text.set_path_effects([withStroke(linewidth=3, foreground=bg)])
        text.hpc_value = True
        ax.set_title(title, color=ink, fontsize=10)
        ax.text(.5, -.07, "Coverage: " + (f"{row['coverage']:.1f}%" if row["coverage"] is not None else "unknown") + f"\nFull circle: {maximum:g}%",
                transform=ax.transAxes, ha="center", color=muted, fontsize=8)

    def allocation_pies(table):
        rows = [dict(data["summary"], name="Overall core allocation", purpose="")] if table == "overall" else data.get(table, [])
        if not rows:
            return empty(fig.add_subplot())
        total = len(rows)
        per_page = style["piesPerPage"]
        pages = max(1, math.ceil(total / per_page))
        page = max(0, min(int(pie_page), pages - 1))
        rows = rows[page * per_page:(page + 1) * per_page]
        columns = min(style["pieColumns"], len(rows))
        count = math.ceil(len(rows) / columns)
        cell = max(style["pieSize"] + 2 * style["pieGap"], style["titleSize"] * 10)
        row_height = style["pieSize"] + 2 * style["pieGap"] + style["titleSize"] + 3 * style["fontSize"]
        top = 2 * style["titleSize"] + 12
        width, height = max(style["chartWidth"], columns * cell), max(style["chartHeight"], count * row_height + top + 20)
        fig.set_size_inches(width / 100, height / 100)
        fig.set_layout_engine(None)
        for index, row in enumerate(rows, 1):
            ax = fig.add_subplot(count, columns, index)
            allocation_pie(ax, row, row_label(row))
            x = ((index - 1) % columns + .5) * width / columns
            y = top + ((index - 1) // columns) * row_height + style["pieGap"] + style["titleSize"] + 2 * style["fontSize"] + style["pieSize"] / 2
            box = style["pieSize"] * 1.25
            ax.set_position(((x - box / 2) / width, (height - y - box / 2) / height, box / width, box / height))
        suffix = f" · Page {page + 1}/{pages} ({total} total)" if pages > 1 else ""
        fig.suptitle(("Node" if table == "nodes" else "Group" if table == "groups" else "Overall") + " core allocation" + suffix, color=ink, fontsize=style["titleSize"])

    def draw(ax, name):
        if name == "gpu_latest":
            return bars(ax, data.get("latestGpu", []), "util", "Last observed GPU utilization (snapshot)", "%", "id")
        if name == "gpu_memory":
            return bars(ax, data.get("latestGpu", []), "memPct", "Last observed GPU memory (snapshot)", "%", "id")
        if name == "gpu_memory_used":
            return bars(ax, data.get("latestGpu", []), "memUsedMb", "Last observed GPU memory (snapshot)", "MB", "id")
        if name == "gpu_user_memory":
            return bars(ax, data.get("latestGpuUsers", []), "memUsedMb", "Last observed process memory by user (snapshot)", "MB", by_user=True)
        if name == "gpu_processes":
            return bars(ax, data.get("latestGpuUsers", []), "processes", "Last observed GPU processes by user (snapshot)", "processes", by_user=True)
        if name in ("gpu_power_latest", "gpu_temp_latest"):
            key, unit = ("power", "W") if name == "gpu_power_latest" else ("temp", "deg C")
            return bars(ax, data.get("latestGpu", []), key, "Last observed GPU " + key + " (snapshot)", unit, "id")
        if name == "usage_pie":
            return allocation_pie(ax, data["summary"], "Overall core allocation")
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
            ax.set_yticks(range(len(rows)), [r["id"] + " / " + aliases.get(r["user"], r["user"]) for r in rows])
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
    elif plot in ("node_pies", "group_pies", "usage_pie"):
        allocation_pies("nodes" if plot == "node_pies" else "groups" if plot == "group_pies" else "overall")
    elif plot == "overview":
        for index, name in enumerate(("jobs", "rates", "user_core", "usage_pie"), 1):
            draw(fig.add_subplot(2, 2, index), name)
    else:
        draw(fig.add_subplot(), plot)
    for ax in fig.axes:
        ax.title.set_fontsize(style["titleSize"])
        ax.set_title(ax.get_title(loc="left"), loc="left", fontsize=style["titleSize"])
        for text in [*ax.get_xticklabels(), *ax.get_yticklabels(), ax.xaxis.label, ax.yaxis.label]:
            text.set_fontsize(style["fontSize"])
        for text in ax.texts:
            text.set_fontsize(style["valueSize"] if getattr(text, "hpc_value", False) else style["fontSize"])
        if ax.get_legend():
            for text in ax.get_legend().get_texts():
                text.set_fontsize(style["fontSize"])
    from matplotlib import font_manager
    from matplotlib.text import Text
    available_fonts = {font.name for font in font_manager.fontManager.ttflist}
    family = next((name for name in ("Malgun Gothic", "NanumGothic", "Noto Sans CJK KR", "Apple SD Gothic Neo") if name in available_fonts), None)
    if family:
        for text in fig.findobj(match=Text):
            text.set_fontfamily([family, "DejaVu Sans"])
    width, height = fig.get_size_inches() * 100
    fig.set_dpi(100 * min(1, math.sqrt(32_000_000 / (width * height)), 16384 / max(width, height)))
    return fig
