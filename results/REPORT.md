# SQLite CRUD web benchmark: Go vs Rust vs Java vs TypeScript vs Python

Generated: 2026-10-21T23:32:56+0700 · host `fedora` · kernel `7.2.11-200.fc44.x86_64` · 8 vCPU

## Stack under test

| Language | Runtime | HTTP framework | SQLite driver | Server config |
|---|---|---|---|---|
| Go | go1.27.3 | Fiber v3.6.1 (fasthttp) | crawshaw.io/sqlite v0.3.2 (direct cgo) | read pool 16, write pool 2, WAL AC 5000, GOGC 400 |
| Rust | rustc 1.99.1 | Actix-web 4.17.0 | rusqlite 0.40.2 (bundled) + r2d2 0.8.10 | 8 workers, DB pool 32, sync read handlers |
| Java | OpenJDK 25.0.4.1 LTS | Javalin 7.2.3 (Jetty 12) | sqlite-jdbc 3.53.4.0 | 32 Jetty threads, DB pool 32, default G1 |
| TypeScript | Bun 1.4.3 | Elysia 1.4.30 | bun:sqlite (built-in) | 8 Bun processes, SO_REUSEPORT, prepared statements |
| Python | CPython 3.14.8 | FastAPI 0.156.8 + uvicorn 0.54.0 | stdlib sqlite3 | 8 uvicorn workers, uvloop + httptools, 1 conn/thread |

All services implement the same routes: `GET /health`, `GET /items/:id`, `GET /items?limit=&offset=`, `POST /items`, `PUT /items/:id`, `DELETE /items/:id`.
SQLite settings are aligned: WAL, `synchronous=NORMAL`, `busy_timeout=5000`, `mmap_size=256MB`, `temp_store=MEMORY`; DB seeded with 10000 rows. Others use `cache_size=-2000`; Go uses `cache_size=-8192`, private cache, NOMUTEX and `wal_autocheckpoint=5000`.

## Method

- Load generator: `wrk` 4.2.0, `-t4 -c64 -d20s --latency`, 5 s warm-up, HTTP keep-alive.
- Each case starts from a fresh copy of the seed database.
- Throughput and latencies come from `wrk`; peak memory is the maximum sum of PSS (fallback RSS) over the whole process tree sampled every 100 ms; CPU is the sum of `utime+stime` deltas of the process tree over the measured wall time.
- Results are the mean of 2×20 s runs per scenario (peak memory is the max; startup is the best of the 2 runs). Go was re-benchmarked against TypeScript after optimization with the same harness; Rust, Java and Python values are from the earlier 5-language runs (identical settings, same host).
- Go optimized: direct crawshaw.io/sqlite API (no database/sql), manual JSON built into pooled buffers, separate read/write connection pools (16/2), WAL autocheckpoint=5000, GOGC=400.
- Rust: read routes run directly on the 8 Actix workers (WAL reads do not block); writes use Actix's blocking pool. TypeScript: 8 independent Bun processes share the port via SO_REUSEPORT.
- Metrics include server CPU only; client (`wrk`) runs on the same 8-vCPU host and is not counted.

## Throughput (req/s, mean of 2 runs)

| Scenario | Go | Rust | Java | TypeScript | Python |
|---|---:|---:|---:|---:|---:|
| GET /items/:id | 74633 | 62419 | 24569 | 67640 | 5580 |
| GET /items?limit=20 | 33586 | 16227 | 14648 | 28507 | 2988 |
| POST /items | 5362 | 4397 | 3104 | 4487 | 2594 |
| Mixed CRUD (55/20/15/8/2) | 16151 | 11005 | 7601 | 13046 | 3662 |

## Response time (ms, avg / p50 / p99)

| Scenario | Go | Rust | Java | TypeScript | Python |
|---|---:|---:|---:|---:|---:|
| GET /items/:id | 0.96 / 0.60 / 4.34 | 1.14 / 0.81 / 5.06 | 2.71 / 2.39 / 9.68 | 1.05 / 0.70 / 4.88 | 12.18 / 10.62 / 39.58 |
| GET /items?limit=20 | 2.18 / 1.65 / 9.71 | 3.95 / 3.66 / 9.05 | 4.58 / 4.15 / 16.61 | 2.41 / 1.95 / 9.27 | 22.59 / 20.50 / 69.03 |
| POST /items | 12.84 / 11.52 / 31.86 | 48.05 / 9.24 / 629.81 | 46.23 / 18.86 / 325.56 | 35.48 / 12.17 / 392.69 | 73.42 / 17.06 / 746.53 |
| Mixed CRUD (55/20/15/8/2) | 5.44 / 3.44 / 31.59 | 24.23 / 4.36 / 364.94 | 11.99 / 8.54 / 90.19 | 8.54 / 3.96 / 66.62 | 19.27 / 15.45 / 69.92 |

## Memory (MB, peak PSS / peak RSS)

| Scenario | Go | Rust | Java | TypeScript | Python |
|---|---:|---:|---:|---:|---:|
| GET /items/:id | 35.0 / 38.9 | 12.8 / 18.2 | 377.3 / 384.1 | 184.7 / 424.9 | 355.4 / 522.5 |
| GET /items?limit=20 | 36.5 / 41.6 | 13.0 / 19.2 | 384.9 / 393.8 | 195.9 / 442.3 | 359.1 / 535.9 |
| POST /items | 40.5 / 42.7 | 41.0 / 44.8 | 263.4 / 268.2 | 172.9 / 439.0 | 375.7 / 545.6 |
| Mixed CRUD (55/20/15/8/2) | 51.9 / 66.4 | 30.1 / 36.7 | 326.7 / 336.6 | 202.6 / 464.2 | 382.6 / 565.6 |

## CPU usage (cores, 100% = 1 core)

| Scenario | Go | Rust | Java | TypeScript | Python |
|---|---:|---:|---:|---:|---:|
| GET /items/:id | 3.71 | 3.71 | 4.46 | 3.79 | 5.35 |
| GET /items?limit=20 | 5.33 | 4.96 | 5.33 | 5.58 | 5.81 |
| POST /items | 1.25 | 1.62 | 2.15 | 1.04 | 4.22 |
| Mixed CRUD (55/20/15/8/2) | 3.75 | 3.28 | 4.41 | 2.58 | 5.47 |

## Startup and idle memory

| Metric | Go | Rust | Java | TypeScript | Python |
|---|---:|---:|---:|---:|---:|
| Startup (best of 2, s) | 0.020 | 0.012 | 1.420 | 0.166 | 1.218 |
| Idle PSS (MB) | 14.6 | 10.9 | 145.3 | 118.1 | 322.1 |

CPU is the sum over the process tree; 100% = one core, so 500% = 5 cores. PSS is lower than RSS for the multi-process stacks because shared pages are divided across processes. Startup is sensitive to host load; in isolation both Go and Rust start in ~10-30 ms. Per-run ranges are in `results/summary.csv`.

## Findings

- **GET /items/:id** (req/s): Go 74633 > TypeScript 67640 > Rust 62419 > Java 24569 > Python 5580
- **GET /items?limit=20** (req/s): Go 33586 > TypeScript 28507 > Rust 16227 > Java 14648 > Python 2988
- **POST /items** (req/s): Go 5362 > TypeScript 4487 > Rust 4397 > Java 3104 > Python 2594
- **Mixed CRUD (55/20/15/8/2)** (req/s): Go 16151 > TypeScript 13046 > Rust 11005 > Java 7601 > Python 3662

- **Scenario wins**: Go 4/4.
- **Peak memory, single-row read**: Rust 12.8 MB < Go 35.0 MB < TypeScript 184.7 MB < Python 355.4 MB < Java 377.3 MB.
- **Startup**: Rust 0.012 s, Go 0.020 s, TypeScript 0.166 s, Python 1.218 s, Java 1.420 s.
- **CPU efficiency, single-row read (req/s per core)**: Go 20090 > TypeScript 17824 > Rust 16802 > Java 5515 > Python 1043.
- **Writes hit the SQLite write lock**: `POST /items` peaks at Go 5362 > TypeScript 4487 > Rust 4397 > Java 3104 > Python 2594 req/s, with p99 tails of 32 / 393 / 630 / 326 / 747 ms in that order. Go now has both the highest write throughput and the lowest write p99.
- **Mixed-CRUD tail latency**: Go p99 32 ms, TypeScript p99 67 ms, Python p99 70 ms, Java p99 90 ms, Rust p99 365 ms. Rust's sync read handlers can queue behind write-lock waits, giving it the highest tail despite near-top throughput.

## Caveats

- `wrk` (4 threads) and the servers share one 8-vCPU desktop machine that also runs a dev session (observed load average ~3-10); absolute numbers are lower and noisier than on a dedicated load-generator host. Services are interleaved per scenario within each run and the table reports both run range and mean.
- Java gets 5 s of JIT warm-up before each 20 s measurement; longer runs would improve Java further. Startup time includes JVM boot.
- Python and TypeScript totals sum 8 worker processes (legitimate multi-core deployments); Go, Rust and Java are single processes.
- The mixed CRUD mix is 55% read-one, 20% read-list, 15% create, 8% update, 2% delete. Deletes touch a separate id range, so the small non-2xx rate (1.3% average) comes only from repeated deletes, not from failed reads.
- Go and TypeScript come from the post-optimization A/B runs; Rust, Java and Python were measured a few minutes earlier. The host is noisy, so cross-run comparisons with those three have a few percent of uncertainty.
- Results are machine-specific; re-run with `bench/run_all.py` to reproduce.

## Reproduce

```sh
python3 bench/seed.py
(cd go-api && go build -o api .)
(cd rust-api && cargo build --release)
(cd java-api && JAVA_HOME=/path/to/jdk25 mvn -q -DskipTests package)
(cd ts-api && bun install)
# optional: BENCH_BUN=/path/to/bun BENCH_JDK=/path/to/jdk25 ...
python3 -m venv py-api/.venv
py-api/.venv/bin/pip install -r py-api/requirements.txt
BENCH_JDK=/path/to/jdk25 python3 bench/run_all.py --duration 20 --warmup 5
python3 bench/summarize.py
```

## Raw data

- `results/final5-run1.json`, `results/final5-run2.json` — Rust, Java, Python (plus pre-optimization Go/TS)
- `results/go-opt-run1.json`, `results/go-opt-run2.json` — optimized Go vs TypeScript (2×20 s)
- `results/summary.csv`, `results/summary.json` — aggregated metrics
- `work/*.wrk.txt` — raw `wrk` output for the last run of each case
