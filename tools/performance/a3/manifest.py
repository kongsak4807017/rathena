"""A3 reproducibility manifest capture, canonical hashing, and freeze checks.

The manifest freezes the full environment identity of one A3 baseline cycle
so that any drift between cycles is detected before execution. Capture is
best-effort: every shell or filesystem failure is recorded under
``capture_errors`` and any capture error makes the manifest ineligible for
execution.

Shell boundary rules (approved design): :func:`subprocess.run` with argument
arrays only, ``shell=False``, an explicit timeout, captured stdout/stderr
decoded as UTF-8 with replacement for invalid bytes, and no shell string
interpolation anywhere.
"""

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from tools.performance.a3.io import read_json, sha256_file
from tools.performance.a3.models import A3Config

COMMAND_TIMEOUT_SECONDS = 10

# Host files outside the repository. Module constants so tests can point
# them at synthetic fixtures without touching real hosts.
MY_CNF_PATH = Path("/etc/mysql/my.cnf")
OS_RELEASE_PATH = Path("/etc/os-release")
NVME_MODEL_PATH = Path("/sys/block/nvme0n1/device/model")
NVME_FIRMWARE_PATH = Path("/sys/block/nvme0n1/device/firmware_rev")
BIOS_VERSION_PATH = Path("/sys/class/dmi/id/bios_version")
CPU_GOVERNOR_PATH = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
BIOS_POWER_PROFILE_PATH = Path("/sys/firmware/acpi/platform_profile")
NIC_SPEED_PATH = Path("/sys/class/net/eth0/speed")
PROMETHEUS_CONFIG_PATH = Path("/etc/prometheus/prometheus.yml")
GRAFANA_DASHBOARD_PATH = Path("/etc/grafana/dashboards/a3-baseline.json")

# Approved A3 dataset constants (synthetic data only, never production rows).
DATASET_SEED = 20260802
DATASET_ROW_COUNTS = {
    "accounts": 6000,
    "characters": 12000,
    "guilds": 200,
    "parties": 500,
}

# The load harness is versioned inside this repository under
# tools/performance/a3; its commit is pinned by the harness tree revision.
LOAD_HARNESS_REPOSITORY = "https://github.com/kongsak4807017/rathena"
WEBGL_CLIENT_REVISION = "a3-webgl-v1"

REDACTION_MARKERS = ("password", "token", "secret", "api_key", "private_key")
REDACTED_VALUE = "<redacted>"

_STDERR_SUMMARY_LIMIT = 200
_SHORT_SHA_LENGTH = 7
_PACKETVER_RE = re.compile(r"^\s*#\s*define\s+PACKETVER\s+(\d+)", re.MULTILINE)
_VERSION_RE = re.compile(r"version\s+([0-9][0-9A-Za-z.\-]*)", re.IGNORECASE)
_MARIADB_VERSION_RE = re.compile(r"Distrib\s+([0-9][0-9A-Za-z.\-]*),")
_SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7}$")


# ---------------------------------------------------------------------------
# Shell and filesystem capture boundary
# ---------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _summarize(text: str) -> str:
    summary = " ".join(text.split())
    return summary[:_STDERR_SUMMARY_LIMIT]


def _error_entry(
    argv: Sequence[str],
    field: str,
    stderr: str,
    return_code: Optional[int],
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "command": list(argv),
        "field": field,
        "return_code": return_code,
        "stderr": _summarize(stderr),
    }
    if timeout is not None:
        entry["timeout"] = timeout
    return entry


def _run_command(
    argv: Sequence[str], field: str, errors: List[Dict[str, Any]]
) -> Optional[str]:
    """Run ``argv`` without a shell and return stripped stdout, or ``None``.

    Any failure (non-zero exit, timeout, or OS error) is appended to
    ``errors`` with the command, return code or timeout, a stderr summary,
    and the manifest field affected.
    """
    argv = [str(part) for part in argv]
    try:
        completed = subprocess.run(
            argv,
            shell=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = _decode(exc.stderr) if isinstance(exc.stderr, bytes) else ""
        errors.append(
            _error_entry(argv, field, stderr, None, timeout=COMMAND_TIMEOUT_SECONDS)
        )
        return None
    except OSError as exc:
        errors.append(_error_entry(argv, field, str(exc), None))
        return None
    if completed.returncode != 0:
        errors.append(
            _error_entry(
                argv, field, _decode(completed.stderr), completed.returncode
            )
        )
        return None
    return _decode(completed.stdout).strip()


def _hash_file(path: Path, field: str, errors: List[Dict[str, Any]]) -> Optional[str]:
    try:
        return sha256_file(Path(path))
    except OSError as exc:
        errors.append(
            _error_entry(["sha256_file", str(path)], field, str(exc), None)
        )
        return None


def _hash_tree(root: Path, field: str, errors: List[Dict[str, Any]]) -> Optional[str]:
    """Hash a directory tree deterministically (sorted relative paths)."""
    root = Path(root)
    if not root.is_dir():
        errors.append(
            _error_entry(["hash_tree", str(root)], field, "not a directory", None)
        )
        return None
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        file_sha = _hash_file(path, field, errors)
        if file_sha is None:
            return None
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_sha.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _read_text(path: Path, field: str, errors: List[Dict[str, Any]]) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        errors.append(
            _error_entry(["read_text", str(path)], field, str(exc), None)
        )
        return None


def _read_int(path: Path, field: str, errors: List[Dict[str, Any]]) -> Optional[int]:
    text = _read_text(path, field, errors)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        errors.append(
            _error_entry(
                ["read_int", str(path)], field, f"not an integer: {text!r}", None
            )
        )
        return None


# ---------------------------------------------------------------------------
# Capture groups
# ---------------------------------------------------------------------------


def _git(root: Path, args: Sequence[str], field: str, errors) -> Optional[str]:
    return _run_command(["git", "-C", str(root), *args], field, errors)


def _capture_source(root: Path, errors: List[Dict[str, Any]]) -> Dict[str, Any]:
    repository_url = _git(
        root, ["config", "--get", "remote.origin.url"], "source.repository_url", errors
    )
    branch = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"], "source.branch", errors)
    commit = _git(root, ["rev-parse", "HEAD"], "source.git_commit_sha", errors)
    status = _git(root, ["status", "--porcelain"], "source.working_tree_clean", errors)
    submodule_raw = _git(root, ["submodule", "status"], "source.submodules", errors)

    submodules: Dict[str, str] = {}
    if submodule_raw:
        for line in submodule_raw.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2:
                submodules[parts[1]] = parts[0].lstrip("+-U")

    return {
        "repository_url": repository_url,
        "branch": branch,
        "git_commit_sha": commit,
        "git_short_sha": commit[:_SHORT_SHA_LENGTH] if commit else None,
        "working_tree_clean": (status == "") if status is not None else None,
        "submodules": submodules,
    }


def _capture_build(root: Path, errors: List[Dict[str, Any]]) -> Dict[str, Any]:
    compiler_banner = _run_command(["cc", "--version"], "build.compiler", errors)
    compiler = compiler_banner.splitlines()[0].split()[0] if compiler_banner else None
    compiler_version = _run_command(
        ["cc", "-dumpfullversion", "-dumpversion"], "build.compiler_version", errors
    )

    build_type = "unknown"
    build_flags = "unknown"
    cmake_cache = _read_text(root / "build" / "CMakeCache.txt", "build.build_type", errors)
    if cmake_cache is not None:
        for line in cmake_cache.splitlines():
            if line.startswith("CMAKE_BUILD_TYPE:STRING="):
                build_type = line.split("=", 1)[1]
            elif line.startswith("CMAKE_CXX_FLAGS:STRING="):
                build_flags = line.split("=", 1)[1]

    return {
        "compiler": compiler,
        "compiler_version": compiler_version,
        "build_type": build_type,
        "build_flags": build_flags,
        "login_server_sha256": _hash_file(
            root / "login-server", "build.login_server_sha256", errors
        ),
        "char_server_sha256": _hash_file(
            root / "char-server", "build.char_server_sha256", errors
        ),
        "map_server_sha256": _hash_file(
            root / "map-server", "build.map_server_sha256", errors
        ),
    }


def _capture_protocol(root: Path, errors: List[Dict[str, Any]]) -> Dict[str, Any]:
    field = "protocol.packetver"
    packetver: Optional[int] = None
    # src/custom/defines_pre.hpp overrides the default in packets.hpp.
    candidates = [root / "src" / "custom" / "defines_pre.hpp",
                  root / "src" / "config" / "packets.hpp"]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        text = _read_text(candidate, field, errors)
        if text is None:
            continue
        match = _PACKETVER_RE.search(text)
        if match:
            packetver = int(match.group(1))
            break
    if packetver is None and not any(e["field"] == field for e in errors):
        errors.append(
            _error_entry(
                ["parse_packetver", str(candidates[-1])],
                field,
                "PACKETVER define not found",
                None,
            )
        )

    packet_db_revision = _git(
        root,
        ["log", "-1", "--format=%H", "--", "db/packet_db.yml"],
        "protocol.packet_database_revision",
        errors,
    )
    return {"packetver": packetver, "packet_database_revision": packet_db_revision}


def _capture_rathena_configuration(
    root: Path, config: A3Config, errors: List[Dict[str, Any]]
) -> Dict[str, Any]:
    thresholds_path = (
        root / "tools" / "performance" / "a3" / "config" / "slo-thresholds.json"
    )
    slow_sql_threshold: Any = None
    slow_script_threshold: Any = None
    try:
        thresholds = read_json(thresholds_path)
        slow_sql_threshold = thresholds["sql_ms"]["p95_max"]
        slow_script_threshold = thresholds["script_ms"]["p95_max"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(
            _error_entry(
                ["read_json", str(thresholds_path)],
                "rathena_configuration.slow_sql_threshold",
                str(exc),
                None,
            )
        )

    return {
        "login_config_sha256": _hash_file(
            root / "conf" / "login_athena.conf",
            "rathena_configuration.login_config_sha256",
            errors,
        ),
        "char_config_sha256": _hash_file(
            root / "conf" / "char_athena.conf",
            "rathena_configuration.char_config_sha256",
            errors,
        ),
        "map_config_sha256": _hash_file(
            root / "conf" / "map_athena.conf",
            "rathena_configuration.map_config_sha256",
            errors,
        ),
        "inter_config_sha256": _hash_file(
            root / "conf" / "inter_athena.conf",
            "rathena_configuration.inter_config_sha256",
            errors,
        ),
        "observability_config_sha256": _hash_file(
            root / "tools" / "observability" / "observer.example.json",
            "rathena_configuration.observability_config_sha256",
            errors,
        ),
        "slow_sql_threshold": slow_sql_threshold,
        "slow_script_threshold": slow_script_threshold,
        "snapshot_interval_seconds": config.scrape_interval_seconds,
    }


def _capture_game_content(root: Path, errors: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "script_tree_sha256": _hash_tree(
            root / "npc", "game_content.script_tree_sha256", errors
        ),
        "npc_content_sha256": _hash_tree(
            root / "npc" / "custom", "game_content.npc_content_sha256", errors
        ),
        "item_database_sha256": _hash_file(
            root / "db" / "item_db.yml", "game_content.item_database_sha256", errors
        ),
        "monster_database_sha256": _hash_file(
            root / "db" / "mob_db.yml", "game_content.monster_database_sha256", errors
        ),
        "skill_database_sha256": _hash_file(
            root / "db" / "skill_db.yml", "game_content.skill_database_sha256", errors
        ),
        "map_index_sha256": _hash_file(
            root / "db" / "map_index.txt", "game_content.map_index_sha256", errors
        ),
    }


def _capture_database(root: Path, errors: List[Dict[str, Any]]) -> Dict[str, Any]:
    version_raw = _run_command(
        ["mariadb", "--version"], "database.mariadb_version", errors
    )
    mariadb_version: Optional[str] = None
    if version_raw:
        match = _MARIADB_VERSION_RE.search(version_raw)
        mariadb_version = match.group(1) if match else version_raw.splitlines()[0]

    schema_revision = _git(
        root,
        ["log", "-1", "--format=%H", "--", "sql-files"],
        "database.schema_revision",
        errors,
    )
    dataset_preimage = json.dumps(
        {"row_counts": DATASET_ROW_COUNTS, "seed": DATASET_SEED},
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "mariadb_version": mariadb_version,
        "my_cnf_sha256": _hash_file(MY_CNF_PATH, "database.my_cnf_sha256", errors),
        "schema_revision": schema_revision,
        "schema_sha256": _hash_tree(
            root / "sql-files", "database.schema_sha256", errors
        ),
        "dataset_seed": DATASET_SEED,
        "dataset_sha256": hashlib.sha256(dataset_preimage.encode("utf-8")).hexdigest(),
        "row_counts": dict(DATASET_ROW_COUNTS),
    }


def _capture_operating_system(errors: List[Dict[str, Any]]) -> Dict[str, Any]:
    distribution: Optional[str] = None
    distribution_version: Optional[str] = None
    os_release = _read_text(
        OS_RELEASE_PATH, "operating_system.distribution", errors
    )
    if os_release is not None:
        values = {}
        for line in os_release.splitlines():
            if "=" in line:
                key, _, raw = line.partition("=")
                values[key.strip()] = raw.strip().strip('"')
        distribution = values.get("ID")
        distribution_version = values.get("VERSION_ID")

    packages_raw = _run_command(
        ["dpkg-query", "-W", "-f=${Package}=${Version}\n"],
        "operating_system.package_snapshot_sha256",
        errors,
    )
    packages_sha256 = (
        hashlib.sha256(packages_raw.encode("utf-8")).hexdigest()
        if packages_raw is not None
        else None
    )

    time_sync_raw = _run_command(
        ["chronyc", "-c", "sources"], "operating_system.time_sync_source", errors
    )
    time_sync_source: Optional[str] = None
    if time_sync_raw:
        first = time_sync_raw.splitlines()[0]
        parts = first.split(",")
        time_sync_source = parts[2] if len(parts) > 2 else first

    return {
        "distribution": distribution,
        "distribution_version": distribution_version,
        "kernel_version": _run_command(
            ["uname", "-r"], "operating_system.kernel_version", errors
        ),
        "package_snapshot_sha256": packages_sha256,
        "filesystem_type": _run_command(
            ["findmnt", "-n", "-o", "FSTYPE", "/"],
            "operating_system.filesystem_type",
            errors,
        ),
        "mount_options": _run_command(
            ["findmnt", "-n", "-o", "OPTIONS", "/"],
            "operating_system.mount_options",
            errors,
        ),
        "timezone": _run_command(
            ["timedatectl", "show", "-p", "Timezone", "--value"],
            "operating_system.timezone",
            errors,
        ),
        "time_sync_source": time_sync_source,
    }


def _lscpu_value(lines: List[str], key: str) -> Optional[str]:
    prefix = key + ":"
    for line in lines:
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return None


def _capture_hardware(errors: List[Dict[str, Any]]) -> Dict[str, Any]:
    lscpu_raw = _run_command(["lscpu"], "hardware.cpu_model", errors)
    lines = lscpu_raw.splitlines() if lscpu_raw else []
    cpu_model = _lscpu_value(lines, "Model name")
    physical_cores: Optional[int] = None
    sockets = _lscpu_value(lines, "Socket(s)")
    cores_per_socket = _lscpu_value(lines, "Core(s) per socket")
    if sockets and cores_per_socket:
        physical_cores = int(sockets) * int(cores_per_socket)
    numa_nodes = _lscpu_value(lines, "NUMA node(s)")
    numa_topology = f"{numa_nodes} node(s)" if numa_nodes else None

    threads_raw = _run_command(["nproc"], "hardware.logical_threads", errors)
    logical_threads = int(threads_raw) if threads_raw else None

    ram_bytes: Optional[int] = None
    free_raw = _run_command(["free", "-b"], "hardware.ram_bytes", errors)
    if free_raw:
        for line in free_raw.splitlines():
            if line.startswith("Mem:"):
                ram_bytes = int(line.split()[1])
                break

    nic_model: Optional[str] = None
    lspci_raw = _run_command(["lspci"], "hardware.nic_model", errors)
    if lspci_raw:
        for line in lspci_raw.splitlines():
            if "Ethernet controller:" in line:
                nic_model = line.split("Ethernet controller:", 1)[1].strip()
                break

    return {
        "cpu_model": cpu_model,
        "physical_cores": physical_cores,
        "logical_threads": logical_threads,
        "ram_bytes": ram_bytes,
        "nvme_model": _read_text(NVME_MODEL_PATH, "hardware.nvme_model", errors),
        "nvme_firmware": _read_text(
            NVME_FIRMWARE_PATH, "hardware.nvme_firmware", errors
        ),
        "nic_model": nic_model,
        "link_speed_mbps": _read_int(
            NIC_SPEED_PATH, "hardware.link_speed_mbps", errors
        ),
        "bios_version": _read_text(BIOS_VERSION_PATH, "hardware.bios_version", errors),
        "cpu_governor": _read_text(
            CPU_GOVERNOR_PATH, "hardware.cpu_governor", errors
        ),
        "bios_power_profile": _read_text(
            BIOS_POWER_PROFILE_PATH, "hardware.bios_power_profile", errors
        ),
        "numa_topology": numa_topology,
    }


def _capture_observability(errors: List[Dict[str, Any]]) -> Dict[str, Any]:
    def version_of(argv: Sequence[str], field: str) -> Optional[str]:
        raw = _run_command(argv, field, errors)
        if raw is None:
            return None
        match = _VERSION_RE.search(raw)
        return match.group(1) if match else raw.splitlines()[0]

    return {
        "prometheus_version": version_of(
            ["prometheus", "--version"], "observability.prometheus_version"
        ),
        "node_exporter_version": version_of(
            ["node_exporter", "--version"], "observability.node_exporter_version"
        ),
        "mariadb_exporter_version": version_of(
            ["mysqld_exporter", "--version"],
            "observability.mariadb_exporter_version",
        ),
        "scrape_config_sha256": _hash_file(
            PROMETHEUS_CONFIG_PATH, "observability.scrape_config_sha256", errors
        ),
        "grafana_version": version_of(
            ["grafana-server", "-v"], "observability.grafana_version"
        ),
        "grafana_dashboard_sha256": _hash_file(
            GRAFANA_DASHBOARD_PATH,
            "observability.grafana_dashboard_sha256",
            errors,
        ),
    }


def _capture_load_generation(
    root: Path, errors: List[Dict[str, Any]]
) -> Dict[str, Any]:
    harness_commit = _git(
        root,
        ["log", "-1", "--format=%H", "--", "tools/performance/a3"],
        "load_generation.harness_commit_sha",
        errors,
    )
    return {
        "harness_repository": LOAD_HARNESS_REPOSITORY,
        "harness_commit_sha": harness_commit,
        "harness_binary_sha256": _hash_file(
            root / "tools" / "performance" / "a3" / "bin" / "a3-load-harness",
            "load_generation.harness_binary_sha256",
            errors,
        ),
        "workload_profile_sha256": _hash_file(
            root / "tools" / "performance" / "a3" / "config" / "workload-profile.json",
            "load_generation.workload_profile_sha256",
            errors,
        ),
        "account_range": f"1-{DATASET_ROW_COUNTS['accounts']}",
        "random_seed": DATASET_SEED,
        "webgl_client_revision": WEBGL_CLIENT_REVISION,
    }


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _redact(value: Any) -> Any:
    """Recursively redact values whose key names a secret marker."""
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            lowered = key.lower()
            if any(marker in lowered for marker in REDACTION_MARKERS):
                redacted[key] = REDACTED_VALUE
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Canonical hashing and manifest identity
# ---------------------------------------------------------------------------


def _manifest_sha256(manifest: Dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of the manifest.

    Canonical form: UTF-8, sorted keys, ``separators=(",", ":")``. Only the
    top-level ``manifest_sha256`` field is excluded from the preimage;
    ``manifest_id``, ``capture_errors`` and ``eligible_for_execution`` are
    part of the hash.
    """
    preimage = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    payload = json.dumps(
        preimage, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def manifest_id(manifest: Dict[str, Any]) -> str:
    """Build the approved manifest ID from manifest identity fields.

    Format: ``a3-YYYYMMDD-<git-short-sha>-ubuntu2404-8c16t-32g-<sequence>``
    with an exactly three-digit sequence. Invalid or missing identity fields
    raise :class:`ValueError` naming the field.
    """
    created_utc = manifest.get("created_utc")
    if not isinstance(created_utc, str):
        raise ValueError("created_utc is required and must be an ISO-8601 string")
    try:
        stamp = datetime.fromisoformat(created_utc.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"created_utc must be a valid ISO-8601 timestamp, got {created_utc!r}"
        ) from None
    date_part = stamp.strftime("%Y%m%d")

    source = manifest.get("source")
    git_short_sha = source.get("git_short_sha") if isinstance(source, dict) else None
    if not isinstance(git_short_sha, str) or not _SHORT_SHA_RE.match(git_short_sha):
        raise ValueError(
            "git_short_sha is required and must be exactly 7 lowercase "
            f"hex characters, got {git_short_sha!r}"
        )

    sequence = manifest.get("sequence")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not 1 <= sequence <= 999
    ):
        raise ValueError(
            f"sequence is required and must be an integer in [1, 999], "
            f"got {sequence!r}"
        )

    return (
        f"a3-{date_part}-{git_short_sha}-ubuntu2404-8c16t-32g-{sequence:03d}"
    )


# ---------------------------------------------------------------------------
# Public capture and verification
# ---------------------------------------------------------------------------


def capture_manifest(repo_root: Path, config: A3Config) -> Dict[str, Any]:
    """Capture the frozen A3 environment manifest for ``repo_root``.

    Best-effort: every shell or filesystem failure is recorded in
    ``capture_errors`` and any capture error marks the manifest ineligible
    for execution (``eligible_for_execution`` is ``False`` and
    ``manifest_id`` is ``None``). Secret-shaped values are redacted and raw
    environment variables are never captured.
    """
    repo_root = Path(repo_root)
    errors: List[Dict[str, Any]] = []

    manifest: Dict[str, Any] = {
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sequence": 1,
        "source": _capture_source(repo_root, errors),
        "build": _capture_build(repo_root, errors),
        "protocol": _capture_protocol(repo_root, errors),
        "rathena_configuration": _capture_rathena_configuration(
            repo_root, config, errors
        ),
        "game_content": _capture_game_content(repo_root, errors),
        "database": _capture_database(repo_root, errors),
        "operating_system": _capture_operating_system(errors),
        "hardware": _capture_hardware(errors),
        "observability": _capture_observability(errors),
        "load_generation": _capture_load_generation(repo_root, errors),
        "capture_errors": errors,
        "eligible_for_execution": not errors,
    }

    # Safety net: no group value may carry secret material.
    for group in (
        "source",
        "build",
        "protocol",
        "rathena_configuration",
        "game_content",
        "database",
        "operating_system",
        "hardware",
        "observability",
        "load_generation",
    ):
        manifest[group] = _redact(manifest[group])

    if errors:
        manifest["manifest_id"] = None
    else:
        manifest["manifest_id"] = manifest_id(manifest)
    manifest["manifest_sha256"] = _manifest_sha256(manifest)
    return manifest


def verify_manifest(expected: Dict[str, Any], actual: Dict[str, Any]) -> List[str]:
    """Recursively compare two frozen manifests.

    Returns one sorted, deterministic dotted-path message per changed leaf
    (``<path> changed``), missing key (``<path> missing``) or unexpected key
    (``<path> unexpected``). Only the top-level ``manifest_sha256`` field is
    ignored; ``manifest_id``, ``capture_errors`` and
    ``eligible_for_execution`` are compared.
    """
    differences: List[str] = []
    _compare(expected, actual, "", differences)
    return sorted(differences)


def _compare(expected: Any, actual: Any, path: str, out: List[str]) -> None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in expected:
            child = f"{path}.{key}" if path else key
            if not path and key == "manifest_sha256":
                continue
            if key not in actual:
                out.append(f"{child} missing")
            else:
                _compare(expected[key], actual[key], child, out)
        for key in actual:
            if not path and key == "manifest_sha256":
                continue
            if key not in expected:
                child = f"{path}.{key}" if path else key
                out.append(f"{child} unexpected")
    elif expected != actual:
        out.append(f"{path} changed")
