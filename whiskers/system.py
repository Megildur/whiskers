"""What this machine (or hosting container) actually lets us use.

Hosting panels like Pterodactyl cap CPU and RAM with cgroups, while os.cpu_count() still reports every
core of the host. Running llama.cpp with 32 threads on a 2-core slice makes it crawl, so read the limits.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path

CGROUP = Path("/sys/fs/cgroup")


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def cpu_limit() -> int:
    """Whole CPUs this process may use (at least 1)."""
    try:
        available = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        available = os.cpu_count() or 1

    quota_cpus: float | None = None
    cpu_max = _read(CGROUP / "cpu.max")  # cgroup v2: "<quota> <period>" or "max <period>"
    if cpu_max:
        parts = cpu_max.split()
        if len(parts) == 2 and parts[0] != "max":
            quota_cpus = int(parts[0]) / int(parts[1])
    else:  # cgroup v1
        quota = _read(CGROUP / "cpu" / "cpu.cfs_quota_us") or _read(CGROUP / "cpu,cpuacct" / "cpu.cfs_quota_us")
        period = _read(CGROUP / "cpu" / "cpu.cfs_period_us") or _read(CGROUP / "cpu,cpuacct" / "cpu.cfs_period_us")
        if quota and period and int(quota) > 0:
            quota_cpus = int(quota) / int(period)

    if quota_cpus is not None:
        available = min(available, max(1, int(quota_cpus)))
    return max(1, available)


def memory_limit_bytes() -> int | None:
    """RAM this process may use, or None if unknown."""
    limits: list[int] = []
    for path in (CGROUP / "memory.max", CGROUP / "memory" / "memory.limit_in_bytes"):
        value = _read(path)
        if value and value.isdigit() and int(value) < 1 << 60:
            limits.append(int(value))
    meminfo = _read(Path("/proc/meminfo"))
    if meminfo:
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                limits.append(int(line.split()[1]) * 1024)
                break
    return min(limits) if limits else None


def is_musl() -> bool:
    """Alpine-style images use musl instead of glibc, so the prebuilt llama.cpp can't run there."""
    if platform.libc_ver()[0] == "glibc":
        return False
    return any(Path("/lib").glob("ld-musl-*.so.1"))


def release_arch() -> str | None:
    """llama.cpp release name for this CPU, or None if there's no prebuilt Linux build for it."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return None
