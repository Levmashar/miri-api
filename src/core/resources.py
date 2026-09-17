"""
CPU / memory readings for the admin panel.

Deliberately dependency-free. The gateway's real home is a Docker container, and
inside one the honest numbers are the CGROUP's, not the host's: a machine with
64 GB of RAM says nothing about a container capped at 4 GB, and that cap is
exactly what makes the browsers get OOM-killed. So this reads, in order:

  1. cgroup v2  (/sys/fs/cgroup/cpu.stat, memory.current, memory.max)
  2. cgroup v1  (/sys/fs/cgroup/{cpuacct,memory}/...)
  3. /proc      (/proc/stat, /proc/meminfo) — the host, when uncapped
  4. psutil     — only if it happens to be installed (local dev on Windows/macOS,
                  where none of the above exist). Never required.

Memory "used" excludes the page cache (inactive_file), which is what `docker
stats` reports and what an operator means by "how full is it" — otherwise the
bar creeps to 100% purely from cached file reads and looks like a leak.

CPU percent is a DELTA between calls, so the first call after start returns None
(the panel shows "—" until the next poll). Everything is best-effort: any read
that fails is skipped, and snapshot() never raises.
"""

from __future__ import annotations

import os
import time

_CG2 = "/sys/fs/cgroup"
_PAGE_SIZE = 4096

# CPU is a rate, so it needs two samples: (monotonic seconds, cpu-seconds used).
_last_cpu: "tuple[float, float] | None" = None
# Scanning every process for the browser total is the expensive part; cache it.
_last_browsers: "tuple[float, dict]" = (0.0, {})
_BROWSER_TTL = 10.0


def _read(path: str) -> "str | None":
    try:
        with open(path, "r") as fh:
            return fh.read()
    except Exception:
        return None


def _read_int(path: str) -> "int | None":
    raw = (_read(path) or "").strip()
    if not raw or raw == "max":
        return None
    try:
        return int(raw.split()[0])
    except Exception:
        return None


# ── CPU ─────────────────────────────────────────────────────────

def _cpu_seconds_used() -> "float | None":
    """Total CPU seconds this cgroup (or the host) has burned."""
    stat = _read(f"{_CG2}/cpu.stat")            # cgroup v2
    if stat:
        for line in stat.splitlines():
            if line.startswith("usage_usec"):
                try:
                    return int(line.split()[1]) / 1_000_000
                except Exception:
                    break

    ns = _read_int("/sys/fs/cgroup/cpuacct/cpuacct.usage")   # cgroup v1
    if ns is not None:
        return ns / 1_000_000_000

    proc = _read("/proc/stat")                   # host
    if proc:
        for line in proc.splitlines():
            if line.startswith("cpu "):
                parts = [float(x) for x in line.split()[1:]]
                ticks = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
                # total - idle - iowait = busy
                busy = sum(parts) - (parts[3] if len(parts) > 3 else 0) \
                                  - (parts[4] if len(parts) > 4 else 0)
                return busy / ticks
    return None


def _cpu_limit() -> float:
    """Effective core count: the cgroup quota if capped, else the CPU count."""
    raw = (_read(f"{_CG2}/cpu.max") or "").split()          # cgroup v2: "quota period"
    if len(raw) == 2 and raw[0] != "max":
        try:
            return max(0.1, int(raw[0]) / int(raw[1]))
        except Exception:
            pass
    quota = _read_int("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")  # cgroup v1
    period = _read_int("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota and period and quota > 0:
        return max(0.1, quota / period)
    return float(os.cpu_count() or 1)


def _cpu_percent(limit: float) -> "float | None":
    """CPU use since the previous call, as a percentage of the budget."""
    global _last_cpu
    used = _cpu_seconds_used()
    now = time.monotonic()
    if used is None:
        return None
    prev, _last_cpu = _last_cpu, (now, used)
    if prev is None:
        return None                     # first sample: nothing to diff against
    elapsed = now - prev[0]
    if elapsed <= 0:
        return None
    pct = ((used - prev[1]) / elapsed) / max(0.1, limit) * 100
    return round(max(0.0, min(100.0, pct)), 1)


# ── Memory ──────────────────────────────────────────────────────

def _cgroup_mem() -> "tuple[int, int] | None":
    """(used, total) bytes from the cgroup, page cache excluded."""
    for base, cur, lim, stat, cache_key in (
        (_CG2, "memory.current", "memory.max", "memory.stat", "inactive_file"),
        ("/sys/fs/cgroup/memory", "memory.usage_in_bytes", "memory.limit_in_bytes",
         "memory.stat", "total_inactive_file"),
    ):
        used = _read_int(f"{base}/{cur}")
        if used is None:
            continue
        raw = _read(f"{base}/{stat}") or ""
        for line in raw.splitlines():
            if line.startswith(cache_key + " "):
                try:
                    used = max(0, used - int(line.split()[1]))
                except Exception:
                    pass
                break
        total = _read_int(f"{base}/{lim}")
        # cgroup v1 reports "no limit" as a huge sentinel, not "max".
        if total is None or total > (1 << 62):
            total = _meminfo_total() or 0
        if total:
            return used, total
    return None


def _meminfo_total() -> "int | None":
    raw = _read("/proc/meminfo")
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("MemTotal:"):
            try:
                return int(line.split()[1]) * 1024
            except Exception:
                return None
    return None


def _proc_mem() -> "tuple[int, int] | None":
    """(used, total) bytes for the host, from /proc/meminfo."""
    raw = _read("/proc/meminfo")
    if not raw:
        return None
    vals = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            try:
                vals[parts[0][:-1]] = int(parts[1]) * 1024
            except Exception:
                continue
    total = vals.get("MemTotal")
    avail = vals.get("MemAvailable")
    if total and avail is not None:
        return max(0, total - avail), total
    return None


# ── Browser processes ───────────────────────────────────────────

def _browsers() -> dict:
    """{count, rss} for the Chrome/Chromium processes, cached for a few seconds.

    This is the number that actually matters here: the browsers, not the Python
    server, are what fills the container.
    """
    global _last_browsers
    now = time.monotonic()
    if now - _last_browsers[0] < _BROWSER_TTL:
        return _last_browsers[1]

    out = {"count": 0, "rss": 0}
    try:
        if os.path.isdir("/proc"):
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                comm = (_read(f"/proc/{pid}/comm") or "").strip().lower()
                if "chrome" not in comm and "chromium" not in comm:
                    continue
                statm = (_read(f"/proc/{pid}/statm") or "").split()
                if len(statm) >= 2:
                    out["count"] += 1
                    out["rss"] += int(statm[1]) * _PAGE_SIZE
        else:
            import psutil  # optional; only reached off Linux (local dev)

            for p in psutil.process_iter(["name", "memory_info"]):
                name = (p.info.get("name") or "").lower()
                if "chrome" in name or "chromium" in name:
                    mi = p.info.get("memory_info")
                    if mi:
                        out["count"] += 1
                        out["rss"] += mi.rss
    except Exception:
        pass

    _last_browsers = (now, out)
    return out


# ── Public ──────────────────────────────────────────────────────

def snapshot() -> dict:
    """CPU/RAM for whatever this process is confined to. Never raises.

    `available` is False when nothing could be read (no /proc, no psutil) — the
    panel then says so rather than drawing a bar out of zeroes.
    """
    try:
        limit = _cpu_limit()
        cpu = _cpu_percent(limit)
        source = "cgroup" if os.path.exists(f"{_CG2}/memory.current") else "proc"

        mem = _cgroup_mem() or _proc_mem()
        if mem is None:
            try:
                import psutil

                vm = psutil.virtual_memory()
                mem, source = (vm.total - vm.available, vm.total), "psutil"
                if cpu is None:
                    cpu = round(psutil.cpu_percent(interval=None), 1)
            except Exception:
                mem = None

        if mem is None:
            return {"available": False, "source": ""}

        used, total = mem
        browsers = _browsers()
        return {
            "available": True,
            "source": source,
            "cpu_percent": cpu,                       # None until the 2nd poll
            "cpu_limit": round(limit, 2),
            "mem_used": used,
            "mem_total": total,
            "mem_percent": round(used / total * 100, 1) if total else None,
            "browser_procs": browsers["count"],
            "browser_rss": browsers["rss"],
        }
    except Exception:
        return {"available": False, "source": ""}
