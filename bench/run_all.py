#!/usr/bin/env python3
"""Run the same HTTP+SQLite CRUD workload against Go, Python and Java.

Metrics per (service, scenario): throughput, latency (avg/p50/p99/max),
peak memory (PSS, plus RSS), CPU usage and startup time.
"""
import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED = os.path.join(ROOT, "bench", "seed.db")
LUA = os.path.join(ROOT, "bench", "lua")
RESULTS = os.path.join(ROOT, "results")
WORK = os.path.join(ROOT, "work")
JDK = os.environ.get("BENCH_JDK", "/tmp/opencode/jdk/jdk-25.0.4.1+1")
BUN = os.environ.get("BENCH_BUN") or shutil.which("bun") or os.path.expanduser("~/.bun/bin/bun")

HOST = "127.0.0.1"
PORT = 8080
BASE = f"http://{HOST}:{PORT}"

SCENARIOS = {
    "read-one": {"url": "/items/5000"},
    "read-list": {"url": "/items?limit=20&offset=1000"},
    "create": {"lua": "write.lua"},
    "mixed-crud": {"lua": "mixed.lua"},
}

SERVICES = {
    "go": {
        "cwd": os.path.join(ROOT, "go-api"),
        "cmd": ["./api"],
        "env": {
            "GOMAXPROCS": os.environ.get("GO_MAXPROCS", "8"),
            "DB_POOL": os.environ.get("GO_POOL", "16"),
            "DB_WRITE_POOL": os.environ.get("GO_WRITE_POOL", "2"),
            "GOGC": os.environ.get("GO_GOGC", "400"),
            "WAL_AC": os.environ.get("GO_WAL_AC", "5000"),

        },
    },
    "rust": {
        "cwd": os.path.join(ROOT, "rust-api"),
        "cmd": [os.path.join(ROOT, "rust-api", "target", "release", "rust-api")],
        "env": {},
    },
    "typescript": {
        "cwd": os.path.join(ROOT, "ts-api"),
        "cmd": [BUN, "run", "src/server.ts"],
        "env": {"WEB_WORKERS": "8"},
    },
    "python": {
        "cwd": os.path.join(ROOT, "py-api"),
        "cmd": [
            os.path.join(ROOT, "py-api", ".venv", "bin", "uvicorn"),
            "app:app", "--host", HOST, "--port", str(PORT),
            "--workers", "8", "--loop", "uvloop", "--http", "httptools",
            "--no-access-log", "--log-level", "warning",
        ],
        "env": {"PYTHONUNBUFFERED": "1"},
    },
    "java": {
        "cwd": os.path.join(ROOT, "java-api"),
        "cmd": [
            os.path.join(JDK, "bin", "java"),
            "--enable-native-access=ALL-UNNAMED",
            "-jar", os.path.join(ROOT, "java-api", "target", "java-api.jar"),
        ],
        "env": {"JAVA_HOME": JDK, "JETTY_THREADS": "32", "DB_POOL": "32"},
    },
}

CLK_TCK = os.sysconf("SC_CLK_TCK")


def wait_port_free(timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, PORT))
            return True
        except OSError:
            time.sleep(0.1)
        finally:
            s.close()
    return False


def http_get(path, timeout=1.0):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, r.read()


def descendants(root):
    pids = [root]
    seen = set()
    out = []
    while pids:
        pid = pids.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        try:
            with open(f"/proc/{pid}/task/{pid}/children") as f:
                kids = [int(x) for x in f.read().split()]
            pids.extend(kids)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    return out


def read_proc(pid):
    """Return (rss_kb, pss_kb, cpu_ticks) for a pid, or None."""
    rss = None
    pss = None
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                    break
    except (FileNotFoundError, ProcessLookupError):
        return None
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    pss = int(line.split()[1])
                    break
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pss = None
    if pss is None:
        pss = rss
    ticks = 0
    try:
        with open(f"/proc/{pid}/stat") as f:
            raw = f.read()
        rest = raw[raw.rindex(")") + 2:].split()
        ticks = int(rest[11]) + int(rest[12])
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return None
    return rss, pss, ticks


class Sampler(threading.Thread):
    def __init__(self, root_pid):
        super().__init__(daemon=True)
        self.root_pid = root_pid
        self.stop_evt = threading.Event()
        self.samples = []
        self.cpu_start = None
        self.t0 = None
        self.elapsed = 0.0
        self.error = None

    def snapshot(self):
        rss = pss = cpu = 0
        for pid in descendants(self.root_pid):
            v = read_proc(pid)
            if v is None:
                continue
            rss += v[0]
            pss += v[1]
            cpu += v[2]
        return rss, pss, cpu

    def run(self):
        try:
            self.t0 = time.monotonic()
            self.cpu_start = self.snapshot()
            while not self.stop_evt.wait(0.1):
                self.samples.append(self.snapshot())
            self.samples.append(self.snapshot())
        except Exception as e:  # keep metrics best-effort
            self.error = repr(e)
        self.elapsed = time.monotonic() - self.t0

    def result(self):
        if not self.samples:
            return {}
        rss_peak = max(s[0] for s in self.samples) / 1024
        pss_peak = max(s[1] for s in self.samples) / 1024
        cpu_seconds = (self.samples[-1][2] - self.cpu_start[2]) / CLK_TCK
        wall = max(self.elapsed, 0.001)
        return {
            "peak_rss_mb": round(rss_peak, 1),
            "peak_pss_mb": round(pss_peak, 1),
            "cpu_seconds": round(cpu_seconds, 2),
            "cpu_percent_total": round(cpu_seconds / wall * 100, 1),
            "cpu_cores": round(cpu_seconds / wall, 2),
            "sample_count": len(self.samples),
        }


def parse_wrk(out):
    def num(pattern, cast=float):
        m = re.search(pattern, out)
        return cast(m.group(1)) if m else None

    def to_ms(value, unit):
        f = {"us": 0.001, "ms": 1.0, "s": 1000.0}.get(unit)
        return None if f is None else round(float(value) * f, 3)

    thread_avg = re.search(
        r"Latency\s+([\d.]+)(\w+)\s+([\d.]+)(\w+)\s+([\d.]+)(\w+)", out
    )
    dist = {
        pct: to_ms(val, unit)
        for pct, val, unit in re.findall(r"^\s+(\d+)%\s+([\d.]+)(\w+)$", out, re.M)
    }
    total_requests = re.search(r"(\d+) requests in", out)
    non2xx = re.search(r"Non-2xx or 3xx responses:\s+(\d+)", out)
    sockets = re.search(
        r"Socket errors: connect (\d+), read (\d+), write (\d+), timeout (\d+)", out
    )
    return {
        "requests_per_sec": num(r"Requests/sec:\s+([\d.]+)"),
        "transfer_per_sec": (re.search(r"Transfer/sec:\s+([\d.]+\w+)", out) or [None, None])[1],
        "requests_total": int(total_requests.group(1)) if total_requests else None,
        "latency_avg_ms": to_ms(thread_avg.group(1), thread_avg.group(2)) if thread_avg else None,
        "latency_stdev_ms": to_ms(thread_avg.group(3), thread_avg.group(4)) if thread_avg else None,
        "latency_max_ms": to_ms(thread_avg.group(5), thread_avg.group(6)) if thread_avg else None,
        "latency_p50_ms": dist.get("50"),
        "latency_p90_ms": dist.get("90"),
        "latency_p99_ms": dist.get("99"),
        "non_2xx": int(non2xx.group(1)) if non2xx else 0,
        "socket_errors": sum(int(x) for x in sockets.groups()) if sockets else 0,
    }


def wrk(url=None, lua=None, duration=20, threads=4, conns=64, latency=True):
    cmd = ["wrk", f"-t{threads}", f"-c{conns}", f"-d{duration}s"]
    if latency:
        cmd.append("--latency")
    if lua:
        cmd.append("-s")
        cmd.append(os.path.join(LUA, lua))
    target = url if lua is None else BASE
    cmd.append(target if target.startswith("http") else BASE + target)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 60)
    return p.stdout + p.stderr, p.returncode


def reset_db(service):
    os.makedirs(WORK, exist_ok=True)
    path = os.path.join(WORK, f"{service}.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass
    shutil.copyfile(SEED, path)
    return path


def start_service(name, db_path):
    spec = SERVICES[name]
    env = os.environ.copy()
    env.update(spec["env"])
    env["DB_PATH"] = db_path
    log = open(os.path.join(WORK, f"{name}.log"), "wb")
    t0 = time.monotonic()
    proc = subprocess.Popen(
        spec["cmd"], cwd=spec["cwd"], env=env, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    startup = None
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            log.close()
            raise RuntimeError(f"{name} exited during startup")
        try:
            status, _ = http_get("/health", timeout=0.5)
            if status == 200:
                startup = time.monotonic() - t0
                break
        except Exception:
            pass
        time.sleep(0.01)
    if startup is None:
        stop_service(proc)
        log.close()
        raise RuntimeError(f"{name} did not become healthy")
    return proc, log, startup


def stop_service(proc):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)


def idle_memory(proc):
    time.sleep(1.0)
    rss = pss = 0
    for pid in descendants(proc.pid):
        v = read_proc(pid)
        if v:
            rss += v[0]
            pss += v[1]
    return round(rss / 1024, 1), round(pss / 1024, 1)


def run_case(service, scenario, cfg, duration, warmup, threads, conns):
    if not wait_port_free():
        raise RuntimeError(f"port {PORT} is busy before {service}/{scenario}")
    db_path = reset_db(service)
    proc, log, startup = start_service(service, db_path)
    try:
        idle_rss, idle_pss = idle_memory(proc)

        warm_out, _ = wrk(
            url=cfg.get("url"), lua=cfg.get("lua"),
            duration=warmup, threads=threads, conns=conns, latency=False,
        )
        warm = parse_wrk(warm_out)

        sampler = Sampler(proc.pid)
        sampler.start()
        out, rc = wrk(
            url=cfg.get("url"), lua=cfg.get("lua"),
            duration=duration, threads=threads, conns=conns, latency=True,
        )
        sampler.stop_evt.set()
        sampler.join(timeout=10)

        metrics = parse_wrk(out)
        result = {
            "service": service,
            "scenario": scenario,
            "duration_s": duration,
            "connections": conns,
            "threads": threads,
            "startup_s": round(startup, 3),
            "idle_rss_mb": idle_rss,
            "idle_pss_mb": idle_pss,
            **metrics,
            **sampler.result(),
            "warmup_rps": warm.get("requests_per_sec"),
            "wrk_exit": rc,
        }
        with open(os.path.join(WORK, f"{service}-{scenario}.wrk.txt"), "w") as f:
            f.write(out)
        print(json.dumps(result), flush=True)
        return result
    finally:
        stop_service(proc)
        log.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--connections", type=int, default=64)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--services", default="go,rust,java,typescript,python")
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--out", default=os.path.join(RESULTS, "results.json"))
    args = ap.parse_args()

    os.makedirs(RESULTS, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)
    selected_services = [s.strip() for s in args.services.split(",") if s.strip()]
    selected_scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]

    if not wait_port_free(30):
        sys.exit(f"port {PORT} is already in use")

    env_info = {
        "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": os.uname().nodename,
        "kernel": os.uname().release,
        "cpu_count": os.cpu_count(),
        "go": subprocess.run(["go", "version"], capture_output=True, text=True).stdout.strip(),
        "python": sys.version.split()[0],
        "java": subprocess.run(
            [os.path.join(JDK, "bin", "java"), "-version"], capture_output=True, text=True
        ).stderr.splitlines()[0],
        "rust": subprocess.run(["rustc", "--version"], capture_output=True, text=True).stdout.strip(),
        "bun": subprocess.run([BUN, "--version"], capture_output=True, text=True).stdout.strip(),
        "wrk": subprocess.run(["wrk", "--version"], capture_output=True, text=True).stdout.strip()
        or "wrk",
        "method": "wrk keep-alive, latency distribution; server process tree sampled every 100ms",
        "db": "SQLite WAL, synchronous=NORMAL, busy_timeout=5000, cache_size=-2000, mmap 256MB",
        "seed_rows": 10000,
    }

    results = []
    for scenario in selected_scenarios:
        cfg = SCENARIOS[scenario]
        for service in selected_services:
            print(f"==> {service} / {scenario}", file=sys.stderr, flush=True)
            try:
                results.append(
                    run_case(
                        service, scenario, cfg, args.duration, args.warmup,
                        args.threads, args.connections,
                    )
                )
            except Exception as e:
                print(f"!! {service}/{scenario} failed: {e}", file=sys.stderr, flush=True)
                results.append(
                    {"service": service, "scenario": scenario, "error": repr(e)}
                )
            time.sleep(0.5)

    payload = {"environment": env_info, "results": results}
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
