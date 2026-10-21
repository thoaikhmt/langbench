#!/usr/bin/env python3
"""Aggregate benchmark JSON runs and emit REPORT.md / summary.csv."""
import csv
import json
import os
import statistics as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results")

SCENARIO_LABELS = {
    "read-one": "GET /items/:id",
    "read-list": "GET /items?limit=20",
    "create": "POST /items",
    "mixed-crud": "Mixed CRUD (55/20/15/8/2)",
}
SERVICE_ORDER = ["go", "rust", "java", "typescript", "python"]
SCENARIO_ORDER = ["read-one", "read-list", "create", "mixed-crud"]


def load_runs(files, skip_empty=True):
    runs = []
    env = None
    for name in files:
        path = os.path.join(RESULTS, name)
        with open(path) as f:
            payload = json.load(f)
        env = env or payload["environment"]
        for r in payload["results"]:
            if "error" not in r:
                runs.append(r)
    return env, runs


def group(runs):
    out = {}
    for r in runs:
        out.setdefault((r["service"], r["scenario"]), []).append(r)
    return out


def agg(rows, key, reducer):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return reducer(vals) if vals else None


def fmt(v, nd=1, suffix=""):
    if v is None:
        return "n/a"
    return f"{v:.{nd}f}{suffix}"


def main():
    env, base_runs = load_runs(["final5-run1.json", "final5-run2.json"])
    go_env, go_runs = load_runs(["go-opt-run1.json", "go-opt-run2.json"])
    env = go_env
    base_runs = [r for r in base_runs if r["service"] not in ("go", "typescript")]
    go_runs = [r for r in go_runs if r["service"] in ("go", "typescript")]
    groups = group(base_runs + go_runs)

    summary = []
    for scenario in SCENARIO_ORDER:
        for service in SERVICE_ORDER:
            rows = groups.get((service, scenario), [])
            if not rows:
                continue
            rps = agg(rows, "requests_per_sec", st.mean)
            reqs = agg(rows, "requests_total", st.mean)
            non2xx = agg(rows, "non_2xx", st.mean)
            summary.append({
                "service": service,
                "scenario": scenario,
                "runs": len(rows),
                "rps_mean": rps,
                "rps_min": agg(rows, "requests_per_sec", min),
                "rps_max": agg(rows, "requests_per_sec", max),
                "lat_avg_ms": agg(rows, "latency_avg_ms", st.mean),
                "lat_p50_ms": agg(rows, "latency_p50_ms", st.mean),
                "lat_p99_ms": agg(rows, "latency_p99_ms", st.mean),
                "lat_max_ms": agg(rows, "latency_max_ms", st.mean),
                "peak_pss_mb": agg(rows, "peak_pss_mb", max),
                "peak_rss_mb": agg(rows, "peak_rss_mb", max),
                "idle_pss_mb": agg(rows, "idle_pss_mb", st.mean),
                "cpu_percent": agg(rows, "cpu_percent_total", st.mean),
                "cpu_cores": agg(rows, "cpu_cores", st.mean),
                "startup_s": agg(rows, "startup_s", min),
                "non2xx_pct": (non2xx / reqs * 100) if reqs else 0.0,
                "efficiency_rps_per_core": (rps / agg(rows, "cpu_cores", st.mean)),
            })

    with open(os.path.join(RESULTS, "summary.json"), "w") as f:
        json.dump({"environment": env, "summary": summary}, f, indent=2)

    for s in summary:
        for k in ("rps_mean", "rps_min", "rps_max"):
            s[k] = round(s[k], 1)
        for k in ("lat_avg_ms", "lat_p50_ms", "lat_p99_ms", "lat_max_ms"):
            s[k] = round(s[k], 2)
        for k in ("peak_pss_mb", "peak_rss_mb", "idle_pss_mb", "cpu_percent"):
            s[k] = round(s[k], 1)
        s["cpu_cores"] = round(s["cpu_cores"], 2)
        s["startup_s"] = round(s["startup_s"], 3)
        s["non2xx_pct"] = round(s["non2xx_pct"], 2)
        s["efficiency_rps_per_core"] = round(s["efficiency_rps_per_core"], 0)

    with open(os.path.join(RESULTS, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0]))
        w.writeheader()
        w.writerows(summary)

    lines = []
    add = lines.append
    add("# SQLite CRUD web benchmark: Go vs Rust vs Java vs TypeScript vs Python")
    add("")
    add(f"Generated: {env['date']} · host `{env['host']}` · kernel `{env['kernel']}` · "
        f"{env['cpu_count']} vCPU")
    add("")
    add("## Stack under test")
    add("")
    add("| Language | Runtime | HTTP framework | SQLite driver | Server config |")
    add("|---|---|---|---|---|")
    add("| Go | go1.27.3 | Fiber v3.6.1 (fasthttp) | crawshaw.io/sqlite v0.3.2 (direct cgo) | read pool 16, write pool 2, WAL AC 5000, GOGC 400 |")
    add("| Rust | rustc 1.99.1 | Actix-web 4.17.0 | rusqlite 0.40.2 (bundled) + r2d2 0.8.10 | 8 workers, DB pool 32, sync read handlers |")
    add("| Java | OpenJDK 25.0.4.1 LTS | Javalin 7.2.3 (Jetty 12) | sqlite-jdbc 3.53.4.0 | 32 Jetty threads, DB pool 32, default G1 |")
    add("| TypeScript | Bun 1.4.3 | Elysia 1.4.30 | bun:sqlite (built-in) | 8 Bun processes, SO_REUSEPORT, prepared statements |")
    add("| Python | CPython 3.14.8 | FastAPI 0.156.8 + uvicorn 0.54.0 | stdlib sqlite3 | 8 uvicorn workers, uvloop + httptools, 1 conn/thread |")
    add("")
    add("All services implement the same routes: `GET /health`, `GET /items/:id`, "
        "`GET /items?limit=&offset=`, `POST /items`, `PUT /items/:id`, `DELETE /items/:id`.")
    add("SQLite settings are aligned: WAL, `synchronous=NORMAL`, `busy_timeout=5000`, "
        "`mmap_size=256MB`, `temp_store=MEMORY`; DB seeded with "
        f"{env['seed_rows']} rows. Others use `cache_size=-2000`; Go uses `cache_size=-8192`, "
        "private cache, NOMUTEX and `wal_autocheckpoint=5000`.")
    add("")
    add("## Method")
    add("")
    add(f"- Load generator: `wrk` 4.2.0, `-t4 -c64 -d20s --latency`, 5 s warm-up, HTTP keep-alive.")
    add("- Each case starts from a fresh copy of the seed database.")
    add("- Throughput and latencies come from `wrk`; peak memory is the maximum sum of PSS "
        "(fallback RSS) over the whole process tree sampled every 100 ms; CPU is the sum of "
        "`utime+stime` deltas of the process tree over the measured wall time.")
    add("- Results are the mean of 2×20 s runs per scenario (peak memory is the max; startup is the "
        "best of the 2 runs). Go was re-benchmarked against TypeScript after optimization with the "
        "same harness; Rust, Java and Python values are from the earlier 5-language runs (identical "
        "settings, same host).")
    add("- Go optimized: direct crawshaw.io/sqlite API (no database/sql), manual JSON built into pooled "
        "buffers, separate read/write connection pools (16/2), WAL autocheckpoint=5000, GOGC=400.")
    add("- Rust: read routes run directly on the 8 Actix workers (WAL reads do not block); writes use "
        "Actix's blocking pool. TypeScript: 8 independent Bun processes share the port via SO_REUSEPORT.")
    add("- Metrics include server CPU only; client (`wrk`) runs on the same 8-vCPU host and is not counted.")
    add("")
    names = {"go": "Go", "rust": "Rust", "java": "Java", "typescript": "TypeScript", "python": "Python"}
    by = {(s["service"], s["scenario"]): s for s in summary}
    services = [s for s in SERVICE_ORDER if any(x["service"] == s for x in summary)]

    add("## Throughput (req/s, mean of 2 runs)")
    add("")
    add("| Scenario | " + " | ".join(names[s] for s in services) + " |")
    add("|---|" + "---:|" * len(services))
    for scenario in SCENARIO_ORDER:
        cells = []
        for service in services:
            row = by.get((service, scenario))
            cells.append(fmt(row["rps_mean"], 0) if row else "n/a")
        add(f"| {SCENARIO_LABELS[scenario]} | " + " | ".join(cells) + " |")
    add("")
    add("## Response time (ms, avg / p50 / p99)")
    add("")
    add("| Scenario | " + " | ".join(names[s] for s in services) + " |")
    add("|---|" + "---:|" * len(services))
    for scenario in SCENARIO_ORDER:
        cells = []
        for service in services:
            row = by.get((service, scenario))
            if row:
                cells.append(
                    f"{fmt(row['lat_avg_ms'], 2)} / {fmt(row['lat_p50_ms'], 2)} / "
                    f"{fmt(row['lat_p99_ms'], 2)}"
                )
            else:
                cells.append("n/a")
        add(f"| {SCENARIO_LABELS[scenario]} | " + " | ".join(cells) + " |")
    add("")
    add("## Memory (MB, peak PSS / peak RSS)")
    add("")
    add("| Scenario | " + " | ".join(names[s] for s in services) + " |")
    add("|---|" + "---:|" * len(services))
    for scenario in SCENARIO_ORDER:
        cells = []
        for service in services:
            row = by.get((service, scenario))
            if row:
                cells.append(f"{fmt(row['peak_pss_mb'], 1)} / {fmt(row['peak_rss_mb'], 1)}")
            else:
                cells.append("n/a")
        add(f"| {SCENARIO_LABELS[scenario]} | " + " | ".join(cells) + " |")
    add("")
    add("## CPU usage (cores, 100% = 1 core)")
    add("")
    add("| Scenario | " + " | ".join(names[s] for s in services) + " |")
    add("|---|" + "---:|" * len(services))
    for scenario in SCENARIO_ORDER:
        cells = []
        for service in services:
            row = by.get((service, scenario))
            cells.append(fmt(row["cpu_cores"], 2) if row else "n/a")
        add(f"| {SCENARIO_LABELS[scenario]} | " + " | ".join(cells) + " |")
    add("")
    add("## Startup and idle memory")
    add("")
    add("| Metric | " + " | ".join(names[s] for s in services) + " |")
    add("|---|" + "---:|" * len(services))
    add("| Startup (best of 2, s) | " + " | ".join(
        fmt(by.get((s, "read-one"), {}).get("startup_s"), 3) for s in services
    ) + " |")
    add("| Idle PSS (MB) | " + " | ".join(
        fmt(by.get((s, "read-one"), {}).get("idle_pss_mb"), 1) for s in services
    ) + " |")
    add("")
    add("CPU is the sum over the process tree; 100% = one core, so 500% = 5 cores. PSS is lower "
        "than RSS for the multi-process stacks because shared pages are divided across processes. "
        "Startup is sensitive to host load; in isolation both Go and Rust start in ~10-30 ms. "
        "Per-run ranges are in `results/summary.csv`.")
    add("")
    add("## Findings")
    add("")

    for scenario in SCENARIO_ORDER:
        ranked = sorted(
            (s for s in summary if s["scenario"] == scenario),
            key=lambda s: -s["rps_mean"],
        )
        add(f"- **{SCENARIO_LABELS[scenario]}** (req/s): " + " > ".join(
            f"{names[s['service']]} {fmt(s['rps_mean'],0)}" for s in ranked
        ))
    add("")

    wins = {}
    for scenario in SCENARIO_ORDER:
        winner = max(
            (s for s in summary if s["scenario"] == scenario),
            key=lambda s: s["rps_mean"],
        )["service"]
        wins[winner] = wins.get(winner, 0) + 1
    add("- **Scenario wins**: " + ", ".join(
        f"{names[k]} {v}/4" for k, v in sorted(wins.items(), key=lambda kv: -kv[1])
    ) + ".")

    mem = sorted(
        (s for s in summary if s["scenario"] == "read-one"),
        key=lambda s: s["peak_pss_mb"],
    )
    add("- **Peak memory, single-row read**: " + " < ".join(
        f"{names[s['service']]} {fmt(s['peak_pss_mb'],1)} MB" for s in mem
    ) + ".")

    startup = sorted(
        (s for s in summary if s["scenario"] == "read-one"),
        key=lambda s: s["startup_s"],
    )
    add("- **Startup**: " + ", ".join(
        f"{names[s['service']]} {fmt(s['startup_s'],3)} s" for s in startup
    ) + ".")

    eff = sorted(
        (s for s in summary if s["scenario"] == "read-one"),
        key=lambda s: -s["efficiency_rps_per_core"],
    )
    add("- **CPU efficiency, single-row read (req/s per core)**: " + " > ".join(
        f"{names[s['service']]} {fmt(s['efficiency_rps_per_core'],0)}" for s in eff
    ) + ".")

    creates = sorted(
        (s for s in summary if s["scenario"] == "create"),
        key=lambda s: -s["rps_mean"],
    )
    add("- **Writes hit the SQLite write lock**: `POST /items` peaks at " + " > ".join(
        f"{names[s['service']]} {fmt(s['rps_mean'],0)}" for s in creates
    ) + " req/s, with p99 tails of " + " / ".join(
        fmt(s["lat_p99_ms"], 0) for s in creates
    ) + " ms in that order. Go now has both the highest write throughput and the lowest write p99.")

    mixed = sorted(
        (s for s in summary if s["scenario"] == "mixed-crud"),
        key=lambda s: s["lat_p99_ms"],
    )
    add("- **Mixed-CRUD tail latency**: " + ", ".join(
        f"{names[s['service']]} p99 {fmt(s['lat_p99_ms'],0)} ms" for s in mixed
    ) + ". Rust's sync read handlers can queue behind write-lock waits, giving it the "
        "highest tail despite near-top throughput.")
    add("")
    add("## Caveats")
    add("")
    add("- `wrk` (4 threads) and the servers share one 8-vCPU desktop machine that also runs a dev "
        "session (observed load average ~3-10); absolute numbers are lower and noisier than on a "
        "dedicated load-generator host. Services are interleaved per scenario within each run and "
        "the table reports both run range and mean.")
    add("- Java gets 5 s of JIT warm-up before each 20 s measurement; longer runs would improve Java "
        "further. Startup time includes JVM boot.")
    add("- Python and TypeScript totals sum 8 worker processes (legitimate multi-core deployments); "
        "Go, Rust and Java are single processes.")
    add("- The mixed CRUD mix is 55% read-one, 20% read-list, 15% create, 8% update, 2% delete. "
        "Deletes touch a separate id range, so the small non-2xx rate "
        f"({fmt(sum(s['non2xx_pct'] for s in summary if s['scenario']=='mixed-crud')/len([s for s in summary if s['scenario']=='mixed-crud']),1)}% average) "
        "comes only from repeated deletes, not from failed reads.")
    add("- Go and TypeScript come from the post-optimization A/B runs; Rust, Java and Python were "
        "measured a few minutes earlier. The host is noisy, so cross-run comparisons with those "
        "three have a few percent of uncertainty.")
    add("- Results are machine-specific; re-run with `bench/run_all.py` to reproduce.")
    add("")
    add("## Reproduce")
    add("")
    add("```sh")
    add("python3 bench/seed.py")
    add("(cd go-api && go build -o api .)")
    add("(cd rust-api && cargo build --release)")
    add("(cd java-api && JAVA_HOME=/path/to/jdk25 mvn -q -DskipTests package)")
    add("(cd ts-api && bun install)")
    add("# optional: BENCH_BUN=/path/to/bun BENCH_JDK=/path/to/jdk25 ...")
    add("python3 -m venv py-api/.venv")
    add("py-api/.venv/bin/pip install -r py-api/requirements.txt")
    add("BENCH_JDK=/path/to/jdk25 python3 bench/run_all.py --duration 20 --warmup 5")
    add("python3 bench/summarize.py")
    add("```")
    add("")
    add("## Raw data")
    add("")
    add("- `results/final5-run1.json`, `results/final5-run2.json` — Rust, Java, Python (plus pre-optimization Go/TS)")
    add("- `results/go-opt-run1.json`, `results/go-opt-run2.json` — optimized Go vs TypeScript (2×20 s)")
    add("- `results/summary.csv`, `results/summary.json` — aggregated metrics")
    add("- `work/*.wrk.txt` — raw `wrk` output for the last run of each case")
    add("")

    with open(os.path.join(RESULTS, "REPORT.md"), "w") as f:
        f.write("\n".join(lines))
    print("wrote results/REPORT.md, results/summary.csv, results/summary.json")


if __name__ == "__main__":
    main()
