"""Shared, dependency-free HPC log parser and accounting engine.

All API times are Unix milliseconds. Intervals are half open [start, end).
This module deliberately does not import either GUI toolkit.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.3.0"
TYPES = ("user_cpu", "queue_started", "queue_pending", "node_cpu", "gpu_user", "gpu")
SCHEMA = {
    "user_cpu": "timestamp user node partition cpus_alloc mem_alloc_mb n_jobs job_ids".split(),
    "queue_started": "timestamp job_id user partition submit_time start_time wait_sec wait_min alloc_cpus n_nodes state".split(),
    "queue_pending": "timestamp job_id user partition qos req_cpus submit_time wait_sec wait_min priority reason".split(),
    "node_cpu": "timestamp node node_type partitions state cpu_total cpu_alloc alloc_pct cpu_load load_pct mem_total_mb mem_alloc_mb mem_pct".split(),
    "gpu_user": "timestamp node gpu_index user job_id pid mem_used_mb proc_name".split(),
    "gpu": "timestamp node gpu_index uuid gpu_name gpu_util mem_total_mb mem_used_mb mem_pct temp_c power_w".split(),
}
NUMBERS = set("cpus_alloc mem_alloc_mb n_jobs wait_sec wait_min alloc_cpus n_nodes req_cpus priority cpu_total cpu_alloc alloc_pct cpu_load load_pct mem_total_mb mem_pct gpu_util mem_used_mb temp_c power_w".split())
KEYS = {
    "user_cpu": ("timestamp", "user", "node", "partition"),
    "queue_started": ("timestamp", "job_id", "start_time", "submit_time"),
    "queue_pending": ("timestamp", "job_id", "submit_time"),
    "node_cpu": ("timestamp", "node"),
    "gpu_user": ("timestamp", "node", "gpu_index", "pid", "user", "job_id"),
    "gpu": ("timestamp", "node", "gpu_index"),
}
DISTINCTIVE = {
    "user_cpu": {"cpus_alloc", "job_ids", "user", "node"},
    "queue_started": {"start_time", "job_id", "alloc_cpus"},
    "queue_pending": {"req_cpus", "reason", "job_id"},
    "node_cpu": {"cpu_total", "cpu_alloc", "node"},
    "gpu_user": {"pid", "user", "gpu_index"},
    "gpu": {"gpu_util", "gpu_index", "gpu_name"},
}
DEFAULTS = dict(offset=540, interval=30, startedMode="snapshot",
                nodeClock=True, denominator="observed", bucket="day")
DAY, HOUR = 86_400_000, 3_600_000
MISSING = re.compile(r"^(?:N/A|NA|nan|null|none|-)$", re.I)


def number(value):
    if value is None or str(value).strip() == "" or MISSING.fullmatch(str(value).strip()):
        return None
    try:
        result = float(str(value).rstrip("%"))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def parse_time(value, offset=540):
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return value if math.isfinite(value) else None
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?(?:Z|[+-]\d{2}:?\d{2})?", text, re.I):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("z", "+00:00").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(minutes=offset)))
        return round(dt.timestamp() * 1000)
    except (ValueError, OverflowError, TypeError):
        return None


def iso(value, offset=540):
    return datetime.fromtimestamp(value / 1000, timezone(timedelta(minutes=offset))).isoformat(timespec="seconds")[:19]


def parse_list(value):
    text = str(value or "").strip()
    if not text or re.fullmatch(r"-|none|N/A|\(null\)", text, re.I):
        return []
    if text.startswith("["):
        try:
            values = json.loads(text)
            if isinstance(values, list):
                return list(dict.fromkeys(str(v).strip() for v in values if str(v).strip()))
        except ValueError:
            pass
    values, part, depth = [], "", 0
    for char in text:
        depth += (char == "[") - (char == "]")
        if depth == 0 and (char in ",;|" or char.isspace()):
            if part:
                values.append(part.strip("\"'"))
            part = ""
        else:
            part += char
    if part:
        values.append(part.strip("\"'"))
    return list(dict.fromkeys(v for v in values if v))


def detect_header(header):
    fields = set(header)
    return next((kind for kind in TYPES if "timestamp" in fields and DISTINCTIVE[kind] <= fields), None)


def ratio(a, b):
    return 100 * a / b if b and b > 0 else None


def options_from(value=None):
    raw, opt = value or {}, dict(DEFAULTS)
    if number(raw.get("offset")) in (0, 540):
        opt["offset"] = int(raw["offset"])
    if number(raw.get("interval")) is not None and 1 <= float(raw["interval"]) <= 1440:
        opt["interval"] = float(raw["interval"])
    for key, valid in (("startedMode", ("snapshot", "event")),
                       ("denominator", ("observed", "calendar")),
                       ("bucket", ("hour", "day", "week", "month"))):
        if raw.get(key) in valid:
            opt[key] = raw[key]
    if isinstance(raw.get("nodeClock"), bool):
        opt["nodeClock"] = raw["nodeClock"]
    for key in ("from", "to"):
        if raw.get(key) is not None:
            if isinstance(raw[key], (int, float)):
                opt[key] = number(raw[key])
            else:
                opt[key] = parse_time(raw[key], opt["offset"])
    for key in ("groups", "nodes", "users"):
        value = raw.get(key, [])
        opt[key] = [str(v) for v in value] if isinstance(value, list) else []
    return opt


class LogStore:
    """Deduplicated records, issue audit and reusable normalized time index."""

    def __init__(self):
        self.tables = {kind: {} for kind in TYPES}
        self.files = []
        self.issues = []
        self.issue_total = self.duplicates = self.conflicts = self.revision = 0
        self.demo = False
        self.imported_settings = None
        self._cache = None

    def issue(self, file, line, message):
        self.issue_total += 1
        if len(self.issues) < 300:
            self.issues.append(dict(file=file, line=line, message=message))

    def insert(self, kind, raw, name, line, summary):
        row = {key: number(raw.get(key)) if key in NUMBERS
               else str(raw.get(key) if raw.get(key) is not None else "").strip()
               for key in SCHEMA[kind]}
        if parse_time(row["timestamp"]) is None:
            self.issue(name, line, "timestamp 형식 오류")
            summary["invalid"] += 1
            return
        required = (("job_id", "user") if kind.startswith("queue_") else
                    ("node", "user", "gpu_index", "pid") if kind == "gpu_user" else
                    ("node", "user") if kind == "user_cpu" else
                    ("node", "gpu_index") if kind == "gpu" else ("node",))
        if any(row.get(k) is None or row.get(k) == "" for k in required):
            self.issue(name, line, "필수 식별자 누락: " + ", ".join(required))
            summary["invalid"] += 1
            return
        for key in ("start_time", "submit_time"):
            if row.get(key) and parse_time(row[key]) is None:
                self.issue(name, line, key + " 형식 오류; 해당 시각은 집계에서 제외")
        for key in NUMBERS.intersection(row):
            original = raw.get(key)
            if original is not None and str(original).strip() and not MISSING.fullmatch(str(original).strip()) and row[key] is None:
                self.issue(name, line, key + " 숫자 형식 오류")
        for key in "cpus_alloc cpu_total cpu_alloc n_jobs alloc_cpus req_cpus n_nodes mem_alloc_mb mem_used_mb mem_total_mb wait_sec wait_min".split():
            if row.get(key) is not None and row[key] < 0:
                self.issue(name, line, key + " 음수는 결측값으로 처리")
                row[key] = None
        if kind == "node_cpu" and row["cpu_total"] is not None and row["cpu_alloc"] is not None and row["cpu_alloc"] > row["cpu_total"]:
            self.issue(name, line, "할당 코어가 전체 코어보다 큼; 원본 수치 유지")
        if kind == "user_cpu" and row["n_jobs"] is not None and row["n_jobs"] != len(parse_list(row["job_ids"])):
            self.issue(name, line, "n_jobs와 job_ids 개수 불일치; 작업 수는 job_ids 기준")
        key = tuple(row[k] for k in KEYS[kind])
        old = self.tables[kind].get(key)
        if old is not None:
            self.duplicates += 1
            summary["duplicates"] += 1
            if old != row:
                self.conflicts += 1
                self.issue(name, line, "같은 식별자·시각의 값이 다름; 나중 행 사용")
        else:
            summary["rows"] += 1
        self.tables[kind][key] = row
        summary["types"].add(kind)
        self.revision += 1
        self._cache = None

    def load_stream(self, stream, name="input.tsv"):
        first = stream.readline()
        while first and not first.strip("\ufeff \t\r\n"):
            first = stream.readline()
        if first.lstrip("\ufeff \r\n\t").startswith("{"):
            return self.load_session(json.loads(first.lstrip("\ufeff") + stream.read()), name)
        import itertools
        summary = dict(name=name, rows=0, duplicates=0, invalid=0, types=set())
        header, kind = None, None
        reader = csv.reader(itertools.chain([first], stream), delimiter="\t", strict=True)
        try:
            for fields in reader:
                if not fields or not any(v.strip() for v in fields):
                    continue
                candidate = [re.sub(r"^#\s*", "", v.lstrip("\ufeff").strip()) for v in fields]
                detected = detect_header(candidate)
                if detected:
                    header, kind = candidate, detected
                    summary["types"].add(kind)
                    continue
                if fields[0].startswith("#"):
                    continue
                if not header:
                    summary["invalid"] += 1
                    if summary["invalid"] < 5:
                        self.issue(name, reader.line_num, "인식 가능한 탭 구분 헤더가 필요합니다.")
                    continue
                if len(fields) != len(header):
                    summary["invalid"] += 1
                    self.issue(name, reader.line_num, f"열 개수 불일치 ({len(fields)}/{len(header)})")
                    continue
                self.insert(kind, dict(zip(header, fields)), name, reader.line_num, summary)
        except csv.Error as exc:
            summary["invalid"] += 1
            self.issue(name, reader.line_num, "TSV 따옴표 형식 오류: " + str(exc))
        summary["types"] = sorted(summary["types"])
        self.files.append(summary)
        return summary

    def load_text(self, text, name="input.tsv"):
        if isinstance(text, bytes):
            text = text.decode("utf-8-sig")
        return self.load_stream(io.StringIO(text), name)

    def load_path(self, path):
        path = Path(path)
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return self.load_stream(stream, path.name)

    def load_session(self, obj, name="session.json"):
        if not isinstance(obj, dict) or obj.get("format") != "hpc-monitor-session" or not isinstance(obj.get("tables"), dict):
            raise ValueError("지원하는 통합 JSON 파일이 아닙니다.")
        self.demo = self.demo or obj.get("demo") is True
        if isinstance(obj.get("settings"), dict):
            self.imported_settings = options_from(obj["settings"])
        summary = dict(name=name, rows=0, duplicates=0, invalid=0, types=set())
        for kind in TYPES:
            rows = obj["tables"].get(kind)
            if not isinstance(rows, list):
                continue
            summary["types"].add(kind)
            for index, raw in enumerate(rows, 1):
                if isinstance(raw, dict):
                    self.insert(kind, raw, name, index, summary)
                else:
                    summary["invalid"] += 1
                    self.issue(name, index, "JSON 행 형식 오류")
        summary["types"] = sorted(summary["types"])
        self.files.append(summary)
        return summary

    def session(self, settings=None):
        return dict(format="hpc-monitor-session", version=1, settings=settings or DEFAULTS,
                    demo=self.demo, tables={k: list(v.values()) for k, v in self.tables.items()})

    def normalized(self, offset=540):
        cache_key = (self.revision, offset)
        if self._cache and self._cache[0] == cache_key:
            return self._cache[1]
        tables, times, nodes, users, groups = {}, {}, {}, set(), set()
        low, high = None, None
        for kind in TYPES:
            tables[kind] = sorted((dict(r, t=parse_time(r["timestamp"], offset),
                                       sub=parse_time(r.get("submit_time"), offset),
                                       start=parse_time(r.get("start_time"), offset))
                                   for r in self.tables[kind].values()), key=lambda r: r["t"])
            times[kind] = sorted({r["t"] for r in tables[kind]})
            if times[kind]:
                low = min(low if low is not None else times[kind][0], times[kind][0])
                high = max(high if high is not None else times[kind][-1], times[kind][-1])
            for row in tables[kind]:
                if row.get("user"):
                    users.add(row["user"])
                membership = parse_list(row.get("partitions") or row.get("partition"))
                groups.update(membership)
                if row.get("node"):
                    node = nodes.setdefault(row["node"], dict(groups=set(), cores=None, nodeType="", hasCPU=False))
                    node["groups"].update(membership)
                    if kind == "node_cpu":
                        node["hasCPU"] = True
                    if row.get("cpu_total") is not None:
                        node["cores"] = row["cpu_total"]
                    if row.get("node_type"):
                        node["nodeType"] = row["node_type"]
        value = dict(tables=tables, times=times, nodes=nodes, users=sorted(users),
                     groups=sorted(groups), min=low, max=high)
        self._cache = cache_key, value
        return value

    def describe(self, options=None):
        n = self.normalized(options_from(options)["offset"])
        return dict(counts={k: len(self.tables[k]) for k in TYPES}, files=self.files,
                    issues=self.issues, issueTotal=self.issue_total, duplicates=self.duplicates,
                    conflicts=self.conflicts, demo=self.demo, min=n["min"], max=n["max"],
                    groups=n["groups"], users=n["users"], nodes=sorted(n["nodes"], key=natural_key))


def natural_key(text):
    return [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in re.split(r"(\d+)", str(text))]


def purpose_for(groups, node=""):
    purposes = {"g1": "양자계산", "g1n": "양자계산", "g2": "양자계산",
                "g3": "고체·MD", "g4": "고체·MD"}
    labels = list(dict.fromkeys(purposes[g.lower()] for g in sorted(groups) if g.lower() in purposes))
    if not labels:
        prefix = re.match(r"^(g1n|g[1-4])(?:[-_.]|\d|$)", node, re.I)
        if prefix:
            labels.append(purposes[prefix[1].lower()])
    return " / ".join(labels) or "미지정"


def wait_stats(values):
    return dict(waitCount=len(values), waitMean=statistics.mean(values) if values else None,
                waitMax=max(values) if values else None)


def merge_ranges(ranges):
    result = []
    for start, end in sorted((a, b) for a, b in ranges if b > a):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def seconds(ranges):
    return sum((b - a) / 1000 for a, b in merge_ranges(ranges))


def bucket_start(t, mode, offset):
    local = t + offset * 60000
    if mode == "hour":
        return local // HOUR * HOUR - offset * 60000
    dt = datetime.fromtimestamp(local / 1000, timezone.utc)
    if mode == "month":
        return datetime(dt.year, dt.month, 1, tzinfo=timezone.utc).timestamp() * 1000 - offset * 60000
    day = local // DAY * DAY - offset * 60000
    return day - dt.weekday() * DAY if mode == "week" else day


def next_bucket(t, mode, offset):
    if mode == "month":
        dt = datetime.fromtimestamp((t + offset * 60000) / 1000, timezone.utc)
        year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
        return datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000 - offset * 60000
    return t + (HOUR if mode == "hour" else 7 * DAY if mode == "week" else DAY)


def splits(a, b, mode, offset):
    while a < b:
        bucket = bucket_start(a, mode, offset)
        end = min(b, next_bucket(bucket, mode, offset))
        yield bucket, (end - a) / 1000
        a = end


def intervals(rows, entity, clock, step, end=None):
    if not clock or end is None:
        return
    next_clock = {t: clock[i + 1] if i + 1 < len(clock) else t for i, t in enumerate(clock)}
    by_entity = defaultdict(list)
    for row in rows:
        by_entity[entity(row)].append(row)
    for values in by_entity.values():
        for index, row in enumerate(values):
            following = values[index + 1]["t"] if index + 1 < len(values) else end
            stop = min(following, row["t"] + step, end, next_clock.get(row["t"], end))
            if stop > row["t"]:
                yield row["t"], stop, row


def fresh_stats():
    return dict(observed=0, busy=0, coreSec=0, capacitySec=0, memSec=0, memWeight=0,
                loadSec=0, loadWeight=0, gpuSec=0, gpuWeight=0, gpuMemSec=0,
                tempMax=None, powerSec=0, powerWeight=0, jobs=set(), nodes=set(),
                jobSec=0, submitted=0, started=0, gpuDeviceSec=0)


def analyze(store: LogStore, options=None):
    opt = options_from(options)
    n = store.normalized(opt["offset"])
    tables, times, inventory = n["tables"], n["times"], n["nodes"]
    available = {k: bool(tables[k]) or any(k in f["types"] for f in store.files) for k in TYPES}
    empty = dict(empty=True, meta=store.describe(opt), settings=opt)
    if n["min"] is None:
        return empty
    start, stop = opt.get("from"), opt.get("to")
    start = n["min"] if start is None else start
    stop = n["max"] if stop is None else stop
    snapshot_only = bool(available["gpu"] and start == stop == n["min"] == n["max"])
    if stop <= start and not snapshot_only:
        return dict(empty, error="집계에는 서로 다른 시각의 기록이 두 개 이상 필요합니다.")
    if (stop - start) / DAY > 3653:
        return dict(empty, error="한 번에 조회할 수 있는 기간은 최대 10년입니다.")
    if opt["bucket"] == "hour" and (stop - start) / HOUR > 240:
        opt["bucket"] = "day"
    if opt["bucket"] == "day" and (stop - start) / DAY > 240:
        opt["bucket"] = "week"
    if opt["bucket"] == "week" and (stop - start) / DAY / 7 > 240:
        opt["bucket"] = "month"
    offset, mode, step = opt["offset"], opt["bucket"], opt["interval"] * 60000
    duration = (stop - start) / 1000
    choose = lambda kind, value: not opt[kind] or value in opt[kind]
    group_match = lambda groups: not opt["groups"] or bool(set(groups).intersection(opt["groups"]))
    row_group = lambda r: group_match(parse_list(r.get("partition") or r.get("partitions")))
    user_match = lambda r: choose("users", r.get("user", ""))
    selected_nodes = {name for name, node in inventory.items() if choose("nodes", name) and group_match(node["groups"])}
    node_stats, group_stats, user_stats, gpu_stats = {}, {}, {}, {}
    daily, heat, gpu_heat = {}, {}, {}
    stat = lambda mapping, key: mapping.setdefault(key, fresh_stats())

    def fresh_day(t):
        return dict(t=t, submitted=set(), started=set(), running=set(), jobSec=0,
                    coreSec=0, capacitySec=0, observed=0, busy=0, gpuSec=0,
                    gpuWeight=0, pendingMax=0, wait=[])

    b = bucket_start(start, mode, offset)
    while b < stop:
        daily[b] = fresh_day(b)
        b = next_bucket(b, mode, offset)

    def day(t):
        key = bucket_start(t, mode, offset)
        return daily.setdefault(key, fresh_day(key))

    def clipped(a, b):
        return max(a, start), min(b, stop)

    def clock_for(kind, anchor):
        return sorted(set(times[kind] + (times[anchor] if opt["nodeClock"] else [])))

    user_clock = clock_for("user_cpu", "node_cpu")
    user_end = user_clock[-1] if user_clock else None
    user_intervals = list(intervals(tables["user_cpu"], lambda r: (r["user"], r["node"], r["partition"]),
                                    user_clock, step, user_end))
    user_at = {}
    for row in tables["user_cpu"]:
        if row["node"] in selected_nodes and user_match(row) and row_group(row):
            item = user_at.setdefault((row["t"], row["node"]), dict(cpu=0, mem=0, cpuKnown=False, memKnown=False))
            if row["cpus_alloc"] is not None:
                item["cpu"] += row["cpus_alloc"]
                item["cpuKnown"] = True
            if row["mem_alloc_mb"] is not None:
                item["mem"] += row["mem_alloc_mb"]
                item["memKnown"] = True
    scoped = bool(opt["users"])
    user_sample_times = set(times["user_cpu"])
    has_jobs = available["user_cpu"] or (opt["startedMode"] == "snapshot" and available["queue_started"])
    for name in selected_nodes:
        if inventory[name]["hasCPU"]:
            node_stats[name] = fresh_stats()
    physical_capacity = 0
    for a, b, row in intervals(tables["node_cpu"], lambda r: r["node"], times["node_cpu"], step,
                               times["node_cpu"][-1] if times["node_cpu"] else None):
        if row["node"] not in selected_nodes:
            continue
        a, b = clipped(a, b)
        if b <= a:
            continue
        if row["cpu_total"] is not None:
            physical_capacity += row["cpu_total"] * (b - a) / 1000
        user = user_at.get((row["t"], row["node"]))
        absent_known = available["user_cpu"] and (row["t"] in user_sample_times or (opt["nodeClock"] and row["cpu_alloc"] == 0))
        cpu, mem = row["cpu_alloc"], row["mem_alloc_mb"]
        if scoped:
            cpu = user["cpu"] if user and user["cpuKnown"] else None if user or not absent_known else 0
            mem = user["mem"] if user and user["memKnown"] else None if user or not absent_known else 0
        targets = [stat(node_stats, row["node"])]
        targets += [stat(group_stats, g) for g in inventory[row["node"]]["groups"] if choose("groups", g)]
        for value in targets:
            value["nodes"].add(row["node"])
        for bt, sec in splits(a, b, mode, offset):
            d = day(bt)
            h = heat.setdefault((row["node"], bt), dict(node=row["node"], t=bt, core=0, cap=0, busy=0, obs=0))
            for value in targets + [d]:
                if cpu is not None:
                    value["observed"] += sec
                    value["busy"] += sec if cpu > 0 else 0
                    value["coreSec"] += cpu * sec
                    if row["cpu_total"] is not None:
                        value["capacitySec"] += row["cpu_total"] * sec
                if mem is not None and row["mem_total_mb"] and row["mem_total_mb"] > 0:
                    value["memSec"] = value.get("memSec", 0) + 100 * mem / row["mem_total_mb"] * sec
                    value["memWeight"] = value.get("memWeight", 0) + sec
                if not scoped and row["load_pct"] is not None:
                    value["loadSec"] = value.get("loadSec", 0) + row["load_pct"] * sec
                    value["loadWeight"] = value.get("loadWeight", 0) + sec
            if cpu is not None:
                h["obs"] += sec
                h["core"] += cpu * sec
                h["busy"] += sec if cpu > 0 else 0
                if row["cpu_total"] is not None:
                    h["cap"] += row["cpu_total"] * sec
    for a, b, row in user_intervals:
        if row["node"] not in selected_nodes or not user_match(row) or not row_group(row):
            continue
        a, b = clipped(a, b)
        if b <= a:
            continue
        u, sec = stat(user_stats, row["user"]), (b - a) / 1000
        u["nodes"].add(row["node"])
        if row["cpus_alloc"] is not None:
            u["coreSec"] += row["cpus_alloc"] * sec
            u["observed"] += sec
            if not available["node_cpu"]:
                for bt, dt in splits(a, b, mode, offset):
                    day(bt)["coreSec"] += row["cpus_alloc"] * dt

    jobs, by_id = {}, defaultdict(list)

    def get_job(row, kind):
        key = (str(row["job_id"]), row["sub"] if row.get("sub") is not None else row.get("start") if row.get("start") is not None else "unknown")
        if key not in jobs:
            wait = row.get("wait_sec")
            if wait is None and row.get("wait_min") is not None:
                wait = row["wait_min"] * 60
            jobs[key] = dict(key=key, id=str(row["job_id"]), user=row.get("user", ""),
                             partition=row.get("partition", ""), submit=row.get("sub"),
                             start=row.get("start"), state=row.get("state", ""),
                             alloc=row.get("alloc_cpus"), nNodes=row.get("n_nodes"), wait=wait,
                             nodes=set(), ranges=[], nodeRanges=defaultdict(list),
                             cpuObservations=[], observations=[], source=set(), hasCPU=False)
            by_id[key[0]].append(jobs[key])
        j = jobs[key]
        if row.get("start") is not None:
            j["start"] = row["start"]
        if row.get("state"):
            j["state"] = row["state"]
        j["source"].add(kind)
        if kind == "queue_started":
            j["observations"].append(row["t"])
            if row.get("alloc_cpus") is not None:
                j["alloc"] = row["alloc_cpus"]
            if row.get("wait_sec") is not None:
                j["wait"] = row["wait_sec"]
            elif row.get("wait_min") is not None:
                j["wait"] = row["wait_min"] * 60
        return j

    for kind in ("queue_pending", "queue_started"):
        for row in tables[kind]:
            get_job(row, kind)

    def resolve_job(job_id, row):
        candidates = [j for j in by_id[job_id] if j["user"] == row["user"] and
                      (j["start"] if j["start"] is not None else j["submit"] if j["submit"] is not None else -math.inf) <= row["t"]]
        if candidates:
            return max(candidates, key=lambda j: j["start"] if j["start"] is not None else j["submit"] or 0)
        return get_job(dict(job_id=job_id, user=row["user"], partition=row["partition"], sub=None, start=None), "user_cpu")

    for row in tables["user_cpu"]:
        for job_id in parse_list(row["job_ids"]):
            j = resolve_job(job_id, row)
            j["hasCPU"] = True
            j["nodes"].add(row["node"])
            j["source"].add("user_cpu")
            j["cpuObservations"].append(row["t"])
    for a, b, row in user_intervals:
        for job_id in parse_list(row["job_ids"]):
            if row["node"] in selected_nodes and user_match(row) and row_group(row):
                j = resolve_job(job_id, row)
                j["ranges"].append([a, b])
                j["nodeRanges"][row["node"]].append([a, b])
    clock_next = {t: user_clock[i + 1] if i + 1 < len(user_clock) else None for i, t in enumerate(user_clock)}
    start_next = {t: times["queue_started"][i + 1] if i + 1 < len(times["queue_started"]) else None for i, t in enumerate(times["queue_started"])}
    for j in jobs.values():
        if not j["hasCPU"] and opt["startedMode"] == "snapshot":
            for t in j["observations"]:
                next_t = start_next.get(t)
                if next_t is not None:
                    j["ranges"].append([t, min(t + step, next_t)])
    queue_clock = sorted(t for t in set(times["queue_pending"] + times["queue_started"] + (times["node_cpu"] if opt["nodeClock"] else [])) if start <= t <= stop)
    last_queue = queue_clock[-1] if queue_clock else None
    current_pending = {get_job(r, "queue_pending")["key"]: r for r in tables["queue_pending"] if r["t"] == last_queue}
    visible, pending_rows, reasons, waits, activity = [], [], defaultdict(int), [], defaultdict(list)
    per_node_jobs, per_group_jobs = defaultdict(set), defaultdict(set)
    per_node_seconds, per_group_seconds = defaultdict(float), defaultdict(float)
    node_waits, group_waits = defaultdict(list), defaultdict(list)
    submitted = started = running = ended_count = 0
    total_job_sec = 0
    for j in jobs.values():
        if not choose("users", j["user"]) or not group_match(parse_list(j["partition"])):
            continue
        if opt["nodes"] and not j["nodes"].intersection(opt["nodes"]):
            continue
        if opt["groups"] and j["nodes"] and not j["nodes"].intersection(selected_nodes):
            continue
        obs = j["cpuObservations"] if j["hasCPU"] else j["observations"]
        first = min(obs, default=math.inf)

        def refine(ranges):
            refined = merge_ranges(ranges)
            if refined and refined[0][0] == first and j["start"] is not None and j["start"] <= first and first - j["start"] <= step:
                refined[0][0] = j["start"]
            return merge_ranges(clipped(*r) for r in refined)

        ranges = refine(j["ranges"])
        elapsed = seconds(ranges)
        is_sub = j["submit"] is not None and start <= j["submit"] < stop
        is_start = j["start"] is not None and start <= j["start"] < stop
        pending = current_pending.get(j["key"])
        if pending and j["start"] is not None and j["start"] <= last_queue:
            pending = None
        if not elapsed and not is_sub and not is_start and not pending:
            continue
        u = stat(user_stats, j["user"] or "미상")
        u["nodes"].update(j["nodes"].intersection(selected_nodes))
        if is_sub:
            submitted += 1
            u["submitted"] += 1
            day(j["submit"])["submitted"].add(j["key"])
        if is_start:
            started += 1
            u["started"] += 1
            day(j["start"])["started"].add(j["key"])
            wait = j["wait"] if j["wait"] is not None else (j["start"] - j["submit"]) / 1000 if j["submit"] is not None else None
            if wait is not None and wait >= 0:
                waits.append(wait)
                day(j["start"])["wait"].append(wait)
                for node in j["nodes"].intersection(selected_nodes):
                    node_waits[node].append(wait)
                for group in set(parse_list(j["partition"])):
                    if choose("groups", group):
                        group_waits[group].append(wait)
        if elapsed:
            running += 1
            total_job_sec += elapsed
            u["jobs"].add(j["key"])
            u["jobSec"] += elapsed
            activity[j["user"]].extend(ranges)
            for a, b in ranges:
                for bt, sec in splits(a, b, mode, offset):
                    day(bt)["running"].add(j["key"])
                    day(bt)["jobSec"] += sec
        last = max((t for t in obs if t <= stop), default=None)
        next_t = (clock_next if j["hasCPU"] else start_next).get(last)
        ended = last is not None and next_t is not None and next_t - last <= step * 1.1 and next_t <= stop
        status = "대기" if pending else "종료 추정" if ended else "종료 미확인" if elapsed else "시작 기록" if is_start else "제출 기록"
        if ended and elapsed:
            ended_count += 1
        if pending:
            wait = pending["wait_sec"] if pending["wait_sec"] is not None else pending["wait_min"] * 60 if pending["wait_min"] is not None else (last_queue - j["submit"]) / 1000 if j["submit"] is not None else None
            pending_rows.append(dict(id=j["id"], user=j["user"], partition=j["partition"],
                                     cpus=pending["req_cpus"], waitMin=wait / 60 if wait is not None else None,
                                     reason=pending["reason"], priority=pending["priority"]))
            reasons[pending["reason"] or "미상"] += 1
        group_ranges = defaultdict(list)
        for node, rs in j["nodeRanges"].items():
            refined = refine(rs)
            sec = seconds(refined)
            if sec:
                per_node_jobs[node].add(j["key"])
                per_node_seconds[node] += sec
                for group in inventory[node]["groups"]:
                    per_group_jobs[group].add(j["key"])
                    group_ranges[group].extend(refined)
        for group, rs in group_ranges.items():
            per_group_seconds[group] += seconds(rs)
        wait = None
        if j["start"] is not None and j["start"] <= stop:
            wait = j["wait"] if j["wait"] is not None else (j["start"] - j["submit"]) / 1000 if j["submit"] is not None else None
        visible.append(dict(id=j["id"], user=j["user"], partition=j["partition"],
                            nodes=", ".join(sorted(j["nodes"], key=natural_key)), submit=j["submit"],
                            start=j["start"] if j["start"] is not None and j["start"] <= stop else None,
                            last=last, end=next_t if ended else None, status=status, state=j["state"],
                            wallHours=elapsed / 3600 if elapsed else None, waitMin=wait / 60 if wait is not None else None,
                            alloc=j["alloc"], source=", ".join(sorted(j["source"])),
                            key=json.dumps(j["key"]), ranges=ranges))
    pending_samples = defaultdict(set)
    for row in tables["queue_pending"]:
        if start <= row["t"] < stop and user_match(row) and row_group(row) and not opt["nodes"]:
            pending_samples[row["t"]].add((row["job_id"], row["sub"]))
    for t, values in pending_samples.items():
        day(t)["pendingMax"] = max(day(t)["pendingMax"], len(values))

    gpu_user_clock = clock_for("gpu_user", "gpu")
    gpu_user_ranges, allowed_gpu, gpu_users = defaultdict(list), set(), defaultdict(set)
    for row in tables["gpu_user"]:
        if start <= row["t"] <= stop and row["node"] in selected_nodes and user_match(row):
            gid = row["node"] + "/GPU" + row["gpu_index"]
            allowed_gpu.add(gid)
            gpu_users[gid].add(row["user"])
    for a, b, row in intervals(tables["gpu_user"], lambda r: (r["node"], r["gpu_index"], r["pid"], r["user"], r["job_id"]),
                               gpu_user_clock, step, gpu_user_clock[-1] if gpu_user_clock else None):
        if row["node"] not in selected_nodes or not user_match(row):
            continue
        a, b = clipped(a, b)
        if b <= a:
            continue
        u, sec = stat(user_stats, row["user"]), (b - a) / 1000
        u["nodes"].add(row["node"])
        if row["mem_used_mb"] is not None:
            u["gpuMemSec"] += row["mem_used_mb"] * sec
        gid = row["node"] + "/GPU" + row["gpu_index"]
        allowed_gpu.add(gid)
        gpu_users[gid].add(row["user"])
        gpu_user_ranges[(row["user"], gid)].append([a, b])
    for (user, _gid), rs in gpu_user_ranges.items():
        stat(user_stats, user)["gpuDeviceSec"] += seconds(rs)

    def gpu_entry(row):
        gid = row["node"] + "/GPU" + row["gpu_index"]
        if gid not in gpu_stats:
            gpu_stats[gid] = dict(fresh_stats(), id=gid, node=row["node"], name=row["gpu_name"], memMax=None)
        return gpu_stats[gid]

    latest_gpu = {}
    for row in tables["gpu"]:
        gid = row["node"] + "/GPU" + row["gpu_index"]
        if start <= row["t"] <= stop and row["node"] in selected_nodes and (not scoped or gid in allowed_gpu):
            gpu_entry(row)
            latest_gpu[gid] = dict(id=gid, node=row["node"], name=row["gpu_name"], t=row["t"],
                                   purpose=purpose_for(inventory[row["node"]]["groups"], row["node"]),
                                   util=row["gpu_util"], memPct=row["mem_pct"], temp=row["temp_c"], power=row["power_w"],
                                   users=sorted(gpu_users[gid]))

    for a, b, row in intervals(tables["gpu"], lambda r: (r["node"], r["gpu_index"]),
                               times["gpu"], step, times["gpu"][-1] if times["gpu"] else None):
        gid = row["node"] + "/GPU" + row["gpu_index"]
        if row["node"] not in selected_nodes or (scoped and gid not in allowed_gpu):
            continue
        a, b = clipped(a, b)
        if b <= a:
            continue
        g = gpu_entry(row)
        for bt, sec in splits(a, b, mode, offset):
            d = day(bt)
            h = gpu_heat.setdefault((gid, bt), dict(node=gid, t=bt, value=0, weight=0))
            g["observed"] += sec
            if row["gpu_util"] is not None:
                g["gpuSec"] += row["gpu_util"] * sec
                g["gpuWeight"] += sec
                d["gpuSec"] += row["gpu_util"] * sec
                d["gpuWeight"] += sec
                h["value"] += row["gpu_util"] * sec
                h["weight"] += sec
            if row["mem_pct"] is not None:
                g["memSec"] += row["mem_pct"] * sec
                g["memWeight"] += sec
            if row["power_w"] is not None:
                g["powerSec"] += row["power_w"] * sec
                g["powerWeight"] += sec
    for row in tables["gpu"]:
        gid = row["node"] + "/GPU" + row["gpu_index"]
        if not start <= row["t"] < stop or row["node"] not in selected_nodes or (scoped and gid not in allowed_gpu):
            continue
        g = gpu_entry(row)
        for field, source in (("tempMax", "temp_c"), ("memMax", "mem_used_mb")):
            if row[source] is not None:
                g[field] = max(g[field] if g[field] is not None else -math.inf, row[source])
    states = {r["node"]: r["state"] for r in tables["node_cpu"] if r["t"] <= stop}
    calendar = opt["denominator"] == "calendar"
    for name in node_waits:
        stat(node_stats, name)
    node_rows = []
    for name, s in node_stats.items():
        inv = inventory[name]
        node_rows.append(dict(name=name, groups=", ".join(sorted(inv["groups"])),
                              purpose=purpose_for(inv["groups"], name), **wait_stats(node_waits[name]),
                              type=inv["nodeType"], state=states.get(name, ""), cores=inv["cores"],
                              jobs=len(per_node_jobs[name]) if has_jobs else None,
                              jobHours=per_node_seconds[name] / 3600 if has_jobs else None,
                              coreHours=None if scoped and not available["user_cpu"] else s["coreSec"] / 3600,
                              busyHours=s["busy"] / 3600,
                              busyPct=ratio(s["busy"], duration if calendar else s["observed"]),
                              coveredBusyPct=ratio(s["busy"], s["observed"]),
                              allocPct=ratio(s["coreSec"], (inv["cores"] or 0) * duration if calendar else s["capacitySec"]),
                              coverage=ratio(s["observed"], duration),
                              memPct=s["memSec"] / s["memWeight"] if s["memWeight"] else None,
                              loadPct=s["loadSec"] / s["loadWeight"] if s["loadWeight"] else None))
    observed = sum(s["observed"] for s in node_stats.values())
    busy = sum(s["busy"] for s in node_stats.values())
    core_sec = sum(s["coreSec"] for s in node_stats.values())
    capacity = sum(s["capacitySec"] for s in node_stats.values())
    full_node = duration * sum(inventory[name]["hasCPU"] for name in selected_nodes)
    full_core = duration * sum(inventory[name]["cores"] or 0 for name in selected_nodes)
    user_rows = []
    for name, s in user_stats.items():
        user_rows.append(dict(name=name, coreHours=s["coreSec"] / 3600 if available["user_cpu"] else None,
                              jobs=len(s["jobs"]) if has_jobs else None,
                              submitted=s["submitted"] if available["queue_pending"] or available["queue_started"] else None,
                              started=s["started"] if available["queue_started"] else None,
                              jobHours=s["jobSec"] / 3600 if has_jobs else None,
                              activityPct=ratio(seconds(activity[name]), duration) if has_jobs else None,
                              contribution=ratio(s["coreSec"], full_core if calendar else physical_capacity) if available["user_cpu"] else None,
                              gpuHours=s["gpuDeviceSec"] / 3600 if available["gpu_user"] else None,
                              gpuMemHours=s["gpuMemSec"] / 3600 if available["gpu_user"] else None,
                              nodes=", ".join(sorted(s["nodes"], key=natural_key))))
    group_rows = []
    for name in group_waits:
        stat(group_stats, name)
    for name, s in group_stats.items():
        group_rows.append(dict(name=name, nodeCount=len(s["nodes"]),
                               purpose=purpose_for([name]), **wait_stats(group_waits[name]),
                               jobs=len(per_group_jobs[name]) if has_jobs else None,
                               jobHours=per_group_seconds[name] / 3600 if has_jobs else None,
                               coreHours=None if scoped and not available["user_cpu"] else s["coreSec"] / 3600,
                               busyHours=s["busy"] / 3600,
                               allocPct=ratio(s["coreSec"], duration * sum(inventory[x]["cores"] or 0 for x in s["nodes"]) if calendar else s["capacitySec"]),
                               busyPct=ratio(s["busy"], len(s["nodes"]) * duration if calendar else s["observed"]),
                               coverage=ratio(s["observed"], len(s["nodes"]) * duration)))
    gpu_rows = [dict(id=g["id"], node=g["node"], name=g["name"],
                     purpose=purpose_for(inventory[g["node"]]["groups"], g["node"]),
                     util=g["gpuSec"] / g["gpuWeight"] if g["gpuWeight"] else None,
                     memPct=g["memSec"] / g["memWeight"] if g["memWeight"] else None,
                     tempMax=g["tempMax"], power=g["powerSec"] / g["powerWeight"] if g["powerWeight"] else None,
                     energyWh=g["powerSec"] / 3600 if g["powerWeight"] else None, memMax=g["memMax"],
                     coverage=ratio(g["observed"], duration), users=", ".join(sorted(gpu_users[g["id"]])))
                for g in gpu_stats.values()]
    gpu_weight = sum(g["gpuWeight"] for g in gpu_stats.values())
    latest_rows = [r for r in tables["node_cpu"] if start <= r["t"] <= stop and r["node"] in selected_nodes]
    last_node = latest_rows[-1]["t"] if latest_rows else None
    summary = dict(submitted=submitted if available["queue_pending"] or available["queue_started"] else None,
                   started=started if available["queue_started"] else None, running=running if has_jobs else None,
                   jobHours=total_job_sec / 3600 if has_jobs else None, inferredEnded=ended_count,
                   coreHours=None if scoped and not available["user_cpu"] else core_sec / 3600 if available["node_cpu"] else sum(u["coreHours"] or 0 for u in user_rows) if available["user_cpu"] else None,
                   busyPct=ratio(busy, full_node if calendar else observed),
                   coveredBusyPct=ratio(busy, observed), allocPct=ratio(core_sec, full_core if calendar else capacity),
                   coverage=ratio(observed, full_node), nodes=len(selected_nodes), users=len(user_rows),
                   waitMedian=statistics.median(waits) if waits else None,
                   **wait_stats(waits),
                   pending=len(pending_rows) if not opt["nodes"] and available["queue_pending"] else None,
                   gpuUtil=sum(g["gpuSec"] for g in gpu_stats.values()) / gpu_weight if gpu_weight else None)
    series = []
    for d in sorted(daily.values(), key=lambda d: d["t"]):
        series.append(dict(t=d["t"], submitted=len(d["submitted"]) if available["queue_pending"] or available["queue_started"] else None,
                           started=len(d["started"]) if available["queue_started"] else None,
                           running=len(d["running"]) if has_jobs else None, jobHours=d["jobSec"] / 3600 if has_jobs else None,
                           coreHours=d["coreSec"] / 3600 if d["capacitySec"] or not available["node_cpu"] and available["user_cpu"] else None,
                           busyPct=ratio(d["busy"], d["observed"]), allocPct=ratio(d["coreSec"], d["capacitySec"]),
                           pendingMax=d["pendingMax"] if available["queue_pending"] and not opt["nodes"] else None,
                           gpuUtil=d["gpuSec"] / d["gpuWeight"] if d["gpuWeight"] else None,
                           waitMedian=statistics.median(d["wait"]) if d["wait"] else None,
                           **wait_stats(d["wait"])))

    def bucket_duration(t):
        return (min(stop, next_bucket(t, mode, offset)) - max(start, t)) / 1000

    notices = [
        f"{opt['interval']:g}분 사이에 시작하고 끝난 작업은 누락될 수 있습니다. 건수는 관측된 작업 기준입니다.",
        "작업 소멸은 종료 추정이며 성공·실패를 의미하지 않습니다. 다음 확인 기록 시각을 종료로 사용합니다.",
        "상태를 다음 기록까지 유지하되, 긴 공백은 최대 한 주기까지만 적분합니다. 마지막 기록 이후는 연장하지 않습니다.",
        "빈 작업 기록과 수집 실패는 완전히 구분할 수 없습니다. node_cpu 시각을 빈 스냅샷 판단에 사용할 수 있습니다.",
        "코어 할당률은 실제 CPU 계산 효율이 아닙니다. GPU 사용자별 계산 사용률은 메모리 비중으로 배분하지 않습니다.",
        "GPU 사용자 점유시간은 GPU·h입니다. 공유 GPU의 사용자 합계는 장치 시간보다 클 수 있습니다.",
        "시작 전 대기는 조회기간에 시작한 고유 작업 기준입니다. 그룹은 작업 partition 기준이며, 노드는 user_cpu에서 확인된 실행 노드에만 집계합니다. 다중 노드 작업은 각 노드에 한 번씩 포함합니다.",
    ]
    if scoped:
        notices.append("GPU 사용률·온도·전력은 선택 사용자가 사용한 장치 전체 값입니다.")
        notices.append("사용자 기여율의 분모는 선택 노드 전체 용량입니다. 사용자 기록이 빠진 가동 구간은 미관측입니다.")
    if snapshot_only:
        notices.append("단일 GPU 스냅샷입니다. 마지막 관측값만 표시하며 경과 시간·기간 평균·에너지를 추정하지 않습니다.")
    if opt["groups"]:
        notices.append("노드 그래프는 선택 partition 소속 물리 노드 전체이며 사용자별 집계에는 작업 partition도 적용합니다.")
    if calendar:
        notices.append("전체기간 사용률은 미관측 구간을 제외한 하한값입니다. 관측률과 함께 해석하세요.")
    if not available["node_cpu"]:
        notices.append("node_cpu가 없어 전체 코어 용량과 노드 사용률·관측률은 계산할 수 없습니다.")
    if any(len(inventory[x]["groups"]) > 1 for x in selected_nodes):
        notices.append("여러 partition의 공통 노드는 그룹별 표에 중복될 수 있으나 전체 합계에서는 한 번만 계산합니다.")
    return dict(empty=False, snapshotOnly=snapshot_only, settings=opt, meta=store.describe(opt), **{"from": start, "to": stop},
                summary=summary, series=series, nodes=sorted(node_rows, key=lambda r: natural_key(r["name"])),
                groups=sorted(group_rows, key=lambda r: r["name"]),
                users=sorted(user_rows, key=lambda r: -(r["coreHours"] or r["gpuHours"] or 0)),
                gpu=sorted(gpu_rows, key=lambda r: natural_key(r["id"])),
                latestGpu=sorted(latest_gpu.values(), key=lambda r: natural_key(r["id"])),
                jobs=sorted(visible, key=lambda r: -(r["start"] if r["start"] is not None else r["submit"] if r["submit"] is not None else r["last"] or 0)),
                heat=[dict(node=h["node"], t=h["t"], value=ratio(h["core"], h["cap"]),
                           coverage=ratio(h["obs"], bucket_duration(h["t"]))) for h in heat.values()],
                gpuHeat=[dict(node=h["node"], t=h["t"], value=h["value"] / h["weight"] if h["weight"] else None,
                              coverage=ratio(h["weight"], bucket_duration(h["t"]))) for h in gpu_heat.values()],
                reasons=[dict(name=k, count=v) for k, v in reasons.items()], pendingRows=pending_rows,
                latestNodes=[dict(node=r["node"], state=r["state"], cpu=r["cpu_alloc"], total=r["cpu_total"], mem=r["mem_pct"])
                             for r in latest_rows if r["t"] == last_node],
                lastQueue=last_queue, lastNodeTs=last_node, notices=notices)


def demo_store():
    """Load the same six-type demo as the original HTML, without executing JS."""
    store = LogStore()
    store.demo = True
    summary = dict(rows=0, duplicates=0, invalid=0, types=set())
    base = parse_time("2026-11-01T00:00:00")
    nodes = [("node01", "cpu-small", 64), ("node02", "cpu-small", 64),
             ("node06", "cpu-large", 128), ("node07", "cpu-large", 128),
             ("node10", "gpu", 64), ("node11", "gpu", 64)]
    owners, starts, submits = ["kim", "lee", "park", "kang"], {}, {}
    def put(kind, row):
        store.insert(kind, row, "내장 예제", 0, summary)
    for i in range(7 * 48 + 1):
        if 137 <= i <= 141:
            continue
        t, stamp = base + i * 1_800_000, iso(base + i * 1_800_000)
        for ni, (node, partition, total) in enumerate(nodes):
            hour, cycle = i % 48 / 2, i // (8 if ni % 2 else 12)
            active = i % 96 < 18 if ni == 3 else 5 < hour < 23 or i % 17 < 4
            jid, user = str(12000 + ni * 1000 + cycle), owners[(cycle + ni) % len(owners)]
            alloc = total * (.5 if ni % 2 else .75) if active else 0
            put("node_cpu", dict(timestamp=stamp, node=node, node_type="GPU" if ni >= 4 else "CPU",
                                partitions=partition, state="MIXED" if active else "IDLE", cpu_total=total,
                                cpu_alloc=alloc, alloc_pct=100 * alloc / total, cpu_load=alloc * .86,
                                load_pct=86 * alloc / total, mem_total_mb=262144,
                                mem_alloc_mb=65536 if active else 0, mem_pct=25 if active else 0))
            if active:
                starts.setdefault(jid, t - 9 * 60000)
                submits.setdefault(jid, t - (25 + ni * 13) * 60000)
                put("user_cpu", dict(timestamp=stamp, user=user, node=node, partition=partition,
                                    cpus_alloc=alloc, mem_alloc_mb=65536, n_jobs=1, job_ids=jid))
                put("queue_started", dict(timestamp=stamp, job_id=jid, user=user, partition=partition,
                                         submit_time=iso(submits[jid]), start_time=iso(starts[jid]),
                                         wait_sec=(starts[jid] - submits[jid]) / 1000,
                                         wait_min=(starts[jid] - submits[jid]) / 60000, alloc_cpus=alloc,
                                         n_nodes=1, state="RUNNING"))
            if ni >= 4:
                for gi in range(2):
                    util, mem = (35 + (i * 7 + gi * 21 + ni * 3) % 64, 8000 + gi * 4000) if active else (0, 0)
                    put("gpu", dict(timestamp=stamp, node=node, gpu_index=str(gi), uuid=f"GPU-example-{ni}-{gi}",
                                    gpu_name="Example GPU 48 GB", gpu_util=util, mem_total_mb=49152,
                                    mem_used_mb=mem, mem_pct=100 * mem / 49152,
                                    temp_c=55 + util * .25 if active else 35, power_w=140 + util * 2 if active else 28))
                    if active:
                        put("gpu_user", dict(timestamp=stamp, node=node, gpu_index=str(gi), user=user,
                                            job_id=jid, pid=str(5000 + ni * 100 + gi), mem_used_mb=mem, proc_name="simulation"))
        for pi in range(4 if 17 < i % 48 < 40 else 1):
            sub = base + (i // 10) * 10 * 1_800_000 - 3_600_000
            put("queue_pending", dict(timestamp=stamp, job_id=str(60000 + (i // 10) * 10 + pi),
                                     user=owners[pi % 4], partition="cpu-large" if pi % 2 else "gpu", qos="normal",
                                     req_cpus=64, submit_time=iso(sub), wait_sec=(t - sub) / 1000,
                                     wait_min=(t - sub) / 60000, priority=1000 + pi,
                                     reason="Priority" if pi % 2 else "Resources"))
    store.files = [dict(name="내장 예제 · 실제 로그가 아닙니다", rows=summary["rows"],
                        duplicates=0, invalid=0, types=list(TYPES))]
    return store


def table_csv(rows, columns, offset=540):
    """UTF-8 BOM CSV, protecting text fields from spreadsheet formulas."""
    stream = io.StringIO(newline="")
    stream.write("\ufeff")
    writer = csv.writer(stream)
    writer.writerow(label for _, label, *_ in columns)
    for row in rows:
        values = []
        for key, _label, *fmt in columns:
            value = row.get(key)
            if fmt and fmt[0] == "time" and value is not None:
                value = iso(value, offset).replace("T", " ")
            if fmt and fmt[0] == "minutes" and value is not None:
                value /= 60
            if isinstance(value, list):
                value = ", ".join(map(str, value))
            if isinstance(value, str) and re.match(r"^\s*[=+\-@]", value):
                value = "'" + value
            values.append("" if value is None else value)
        writer.writerow(values)
    return stream.getvalue()
