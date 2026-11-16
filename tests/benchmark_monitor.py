"""Reproducible synthetic import/query benchmark (no third-party packages).

python tests/benchmark_monitor.py --days 14 --nodes 32
python tests/benchmark_monitor.py --engine /path/to/old/hpc_monitor_core.py
Timings vary by machine. No logs are written to disk or sent to a server.
"""
import argparse
import importlib.util
import io
import json
import time
from datetime import datetime, timedelta
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, default=Path(__file__).resolve().parents[1] / "hpc_monitor_core.py")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--nodes", type=int, default=32)
    args = parser.parse_args()
    if args.days < 2 or args.nodes < 1:
        parser.error("Use at least 2 days and 1 node")
    spec = importlib.util.spec_from_file_location("benchmark_engine", args.engine)
    c = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c)
    base = datetime(2026, 11, 1)
    buffers = {kind: io.StringIO() for kind in ("node_cpu", "user_cpu", "queue_started")}
    for kind, out in buffers.items():
        out.write("\t".join(c.SCHEMA[kind]) + "\n")
    for i in range(args.days * 48 + 1):
        timestamp = (base + timedelta(minutes=30 * i)).isoformat()
        for j in range(args.nodes):
            node, user = f"g4-{j:02}", f"user{j % 8}"
            rows = {
                "node_cpu": dict(timestamp=timestamp, node=node, partitions="g4", state="MIXED", cpu_total=128,
                                 cpu_alloc=96, mem_total_mb=1024, mem_alloc_mb=512, load_pct=75),
                "user_cpu": dict(timestamp=timestamp, node=node, user=user, partition="g4", cpus_alloc=96,
                                 n_jobs=1, job_ids=str(1000 + j + i // 48 * args.nodes)),
                "queue_started": dict(timestamp=timestamp, job_id=str(1000 + j + i // 48 * args.nodes),
                                      user=user, partition="g4", submit_time=(base + timedelta(minutes=30 * (i // 48 * 48) - 10)).isoformat(),
                                      start_time=(base + timedelta(minutes=30 * (i // 48 * 48))).isoformat(),
                                      alloc_cpus=96, n_nodes=1, state="RUNNING"),
            }
            for kind, out in buffers.items():
                out.write("\t".join(str(rows[kind].get(field, "")) for field in c.SCHEMA[kind]) + "\n")
    texts = {kind: out.getvalue() for kind, out in buffers.items()}
    store = c.LogStore()
    started = time.perf_counter()
    for kind, text in texts.items():
        store.load_text(text, kind + ".tsv")
    load_seconds = time.perf_counter() - started
    started = time.perf_counter()
    c.analyze(store)
    analysis_seconds = time.perf_counter() - started
    first = base + timedelta(days=args.days - 4 if args.days >= 4 else 0)
    last = first + timedelta(days=2)
    started = time.perf_counter()
    c.analyze(store, {"from": c.parse_time(first.isoformat()), "to": c.parse_time(last.isoformat())})
    filtered_seconds = time.perf_counter() - started
    print(json.dumps(dict(version=c.VERSION, rows=sum(len(rows) for rows in store.tables.values()),
                          mib=sum(len(text.encode()) for text in texts.values()) / 1024 ** 2,
                          load=load_seconds, analysis=analysis_seconds, date_filter=filtered_seconds)))


if __name__ == "__main__":
    main()
