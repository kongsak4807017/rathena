#!/usr/bin/env python3
"""Lightweight Prometheus exporter for rAthena processes.

The exporter intentionally runs out-of-process so production servers can gain
visibility without changing gameplay behavior. It uses only the Python standard
library and Linux /proc. MariaDB probing is optional and disabled by default.
"""

from __future__ import annotations

import argparse
import dataclasses
import http.server
import json
import os
import pathlib
import re
import shlex
import socketserver
import subprocess
import threading
import time
from typing import Iterable, Mapping, Sequence


@dataclasses.dataclass(frozen=True)
class ProcessTarget:
    name: str
    pid_file: str | None = None
    command_contains: str | None = None


@dataclasses.dataclass(frozen=True)
class ObserverConfig:
    listen_host: str
    listen_port: int
    scrape_interval_seconds: float
    process_targets: tuple[ProcessTarget, ...]
    tcp_ports: tuple[int, ...]
    mysql_probe_command: tuple[str, ...] | None
    mysql_probe_timeout_seconds: float


@dataclasses.dataclass
class ProcessSample:
    name: str
    pid: int
    cpu_seconds: float
    resident_bytes: int
    virtual_bytes: int
    threads: int
    open_fds: int
    start_time_seconds: float


@dataclasses.dataclass
class Snapshot:
    collected_at_seconds: float
    process_samples: list[ProcessSample]
    tcp_established: dict[int, int]
    mysql_probe_success: bool | None
    mysql_probe_duration_seconds: float | None
    collection_errors: dict[str, int]


def load_config(path: pathlib.Path) -> ObserverConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    targets = tuple(
        ProcessTarget(
            name=str(item["name"]),
            pid_file=item.get("pid_file"),
            command_contains=item.get("command_contains"),
        )
        for item in raw.get("process_targets", [])
    )
    mysql = raw.get("mysql_probe", {})
    command = mysql.get("command") if mysql.get("enabled", False) else None
    if isinstance(command, str):
        command = shlex.split(command)
    if command is not None:
        command = tuple(str(part) for part in command)

    return ObserverConfig(
        listen_host=str(raw.get("listen_host", "127.0.0.1")),
        listen_port=int(raw.get("listen_port", 9468)),
        scrape_interval_seconds=float(raw.get("scrape_interval_seconds", 5.0)),
        process_targets=targets,
        tcp_ports=tuple(int(port) for port in raw.get("tcp_ports", [6900, 6121, 5121])),
        mysql_probe_command=command,
        mysql_probe_timeout_seconds=float(mysql.get("timeout_seconds", 2.0)),
    )


def _read_key_value_file(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def _parse_kib(value: str) -> int:
    match = re.match(r"^(\d+)\s+kB$", value)
    if not match:
        return 0
    return int(match.group(1)) * 1024


def _clock_ticks_per_second() -> int:
    return int(os.sysconf(os.sysconf_names["SC_CLK_TCK"]))


def read_process_sample(proc_root: pathlib.Path, name: str, pid: int) -> ProcessSample:
    proc_dir = proc_root / str(pid)
    stat_fields = (proc_dir / "stat").read_text(encoding="utf-8").split()
    status = _read_key_value_file(proc_dir / "status")
    ticks = _clock_ticks_per_second()
    cpu_seconds = (int(stat_fields[13]) + int(stat_fields[14])) / ticks
    start_time_seconds = int(stat_fields[21]) / ticks
    resident_bytes = _parse_kib(status.get("VmRSS", "0 kB"))
    virtual_bytes = _parse_kib(status.get("VmSize", "0 kB"))
    threads = int(status.get("Threads", "0"))
    try:
        open_fds = sum(1 for _ in (proc_dir / "fd").iterdir())
    except (FileNotFoundError, PermissionError):
        open_fds = 0
    return ProcessSample(
        name=name,
        pid=pid,
        cpu_seconds=cpu_seconds,
        resident_bytes=resident_bytes,
        virtual_bytes=virtual_bytes,
        threads=threads,
        open_fds=open_fds,
        start_time_seconds=start_time_seconds,
    )


def resolve_pid(proc_root: pathlib.Path, target: ProcessTarget) -> int | None:
    if target.pid_file:
        try:
            return int(pathlib.Path(target.pid_file).read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError, PermissionError):
            return None
    if not target.command_contains:
        return None
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if target.command_contains in cmdline:
            return int(entry.name)
    return None


def _decode_proc_port(hex_address: str) -> int:
    return int(hex_address.rsplit(":", 1)[1], 16)


def count_established_tcp(proc_net_files: Iterable[pathlib.Path], ports: Sequence[int]) -> dict[int, int]:
    wanted = set(ports)
    counts = {port: 0 for port in ports}
    for path in proc_net_files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[1:]
        except FileNotFoundError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "01":
                continue
            local_port = _decode_proc_port(fields[1])
            if local_port in wanted:
                counts[local_port] += 1
    return counts


def run_mysql_probe(command: Sequence[str], timeout_seconds: float) -> tuple[bool, float]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
        )
        success = completed.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        success = False
    return success, time.monotonic() - started


class Collector:
    def __init__(self, config: ObserverConfig, proc_root: pathlib.Path = pathlib.Path("/proc")) -> None:
        self.config = config
        self.proc_root = proc_root

    def collect(self) -> Snapshot:
        errors: dict[str, int] = {}
        samples: list[ProcessSample] = []
        for target in self.config.process_targets:
            pid = resolve_pid(self.proc_root, target)
            if pid is None:
                errors[f"process_not_found:{target.name}"] = 1
                continue
            try:
                samples.append(read_process_sample(self.proc_root, target.name, pid))
            except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, IndexError):
                errors[f"process_read_failed:{target.name}"] = 1

        tcp_files = (self.proc_root / "net" / "tcp", self.proc_root / "net" / "tcp6")
        tcp_counts = count_established_tcp(tcp_files, self.config.tcp_ports)

        mysql_success: bool | None = None
        mysql_duration: float | None = None
        if self.config.mysql_probe_command:
            mysql_success, mysql_duration = run_mysql_probe(
                self.config.mysql_probe_command,
                self.config.mysql_probe_timeout_seconds,
            )

        return Snapshot(
            collected_at_seconds=time.time(),
            process_samples=samples,
            tcp_established=tcp_counts,
            mysql_probe_success=mysql_success,
            mysql_probe_duration_seconds=mysql_duration,
            collection_errors=errors,
        )


def _labels(values: Mapping[str, str | int]) -> str:
    escaped = []
    for key, value in sorted(values.items()):
        text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        escaped.append(f'{key}="{text}"')
    return "{" + ",".join(escaped) + "}"


def render_prometheus(snapshot: Snapshot) -> str:
    lines = [
        "# HELP rathena_observer_collection_timestamp_seconds Unix timestamp of the latest collection.",
        "# TYPE rathena_observer_collection_timestamp_seconds gauge",
        f"rathena_observer_collection_timestamp_seconds {snapshot.collected_at_seconds:.6f}",
        "# HELP rathena_process_up Whether the configured rAthena process was observed.",
        "# TYPE rathena_process_up gauge",
    ]
    observed_names = {sample.name for sample in snapshot.process_samples}
    error_names = {
        key.split(":", 1)[1]
        for key in snapshot.collection_errors
        if key.startswith("process_not_found:") or key.startswith("process_read_failed:")
    }
    for name in sorted(observed_names | error_names):
        lines.append(f"rathena_process_up{_labels({'process': name})} {1 if name in observed_names else 0}")

    metrics = (
        ("cpu_seconds_total", "counter", "Total CPU time consumed by the process.", lambda s: s.cpu_seconds),
        ("resident_memory_bytes", "gauge", "Resident memory used by the process.", lambda s: s.resident_bytes),
        ("virtual_memory_bytes", "gauge", "Virtual memory size of the process.", lambda s: s.virtual_bytes),
        ("threads", "gauge", "Number of process threads.", lambda s: s.threads),
        ("open_file_descriptors", "gauge", "Number of open file descriptors.", lambda s: s.open_fds),
        ("start_time_seconds", "gauge", "Process start time in clock ticks converted to seconds since boot.", lambda s: s.start_time_seconds),
    )
    for suffix, metric_type, help_text, getter in metrics:
        name = f"rathena_process_{suffix}"
        lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"])
        for sample in sorted(snapshot.process_samples, key=lambda item: item.name):
            lines.append(f"{name}{_labels({'process': sample.name, 'pid': sample.pid})} {getter(sample)}")

    lines.extend([
        "# HELP rathena_tcp_established_connections Established TCP connections by local rAthena port.",
        "# TYPE rathena_tcp_established_connections gauge",
    ])
    for port, count in sorted(snapshot.tcp_established.items()):
        lines.append(f"rathena_tcp_established_connections{_labels({'port': port})} {count}")

    if snapshot.mysql_probe_success is not None:
        lines.extend([
            "# HELP rathena_mysql_probe_success Whether the MariaDB probe succeeded.",
            "# TYPE rathena_mysql_probe_success gauge",
            f"rathena_mysql_probe_success {1 if snapshot.mysql_probe_success else 0}",
            "# HELP rathena_mysql_probe_duration_seconds Duration of the MariaDB probe.",
            "# TYPE rathena_mysql_probe_duration_seconds gauge",
            f"rathena_mysql_probe_duration_seconds {snapshot.mysql_probe_duration_seconds or 0.0:.6f}",
        ])

    lines.extend([
        "# HELP rathena_observer_collection_errors Current collection errors by reason.",
        "# TYPE rathena_observer_collection_errors gauge",
    ])
    for reason, value in sorted(snapshot.collection_errors.items()):
        lines.append(f"rathena_observer_collection_errors{_labels({'reason': reason})} {value}")
    return "\n".join(lines) + "\n"


class SnapshotStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = Snapshot(time.time(), [], {}, None, None, {"not_collected_yet": 1})

    def set(self, snapshot: Snapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def get(self) -> Snapshot:
        with self._lock:
            return self._snapshot


def make_handler(store: SnapshotStore) -> type[http.server.BaseHTTPRequestHandler]:
    class MetricsHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                body = b"ok\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
            elif self.path == "/metrics":
                body = render_prometheus(store.get()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            else:
                body = b"not found\n"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return MetricsHandler


def run(config: ObserverConfig) -> None:
    collector = Collector(config)
    store = SnapshotStore()

    def collect_forever() -> None:
        while True:
            store.set(collector.collect())
            time.sleep(config.scrape_interval_seconds)

    thread = threading.Thread(target=collect_forever, name="rathena-observer-collector", daemon=True)
    thread.start()

    with socketserver.ThreadingTCPServer(
        (config.listen_host, config.listen_port),
        make_handler(store),
    ) as server:
        server.daemon_threads = True
        server.allow_reuse_address = True
        server.serve_forever()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Expose rAthena process and connection metrics for Prometheus.")
    parser.add_argument("--config", type=pathlib.Path, required=True, help="Path to observer JSON configuration.")
    parser.add_argument("--check", action="store_true", help="Validate configuration and perform one collection.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    if args.check:
        print(render_prometheus(Collector(config).collect()), end="")
        return 0
    run(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
