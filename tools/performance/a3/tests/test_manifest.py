"""Tests for A3 reproducibility manifest capture, hashing, and freeze checks."""

import copy
import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.performance.a3.config import load_config
from tools.performance.a3.manifest import (
    _manifest_sha256,
    _redact,
    capture_manifest,
    manifest_id,
    verify_manifest,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "valid_manifest.json"
EXAMPLE_CONFIG = (
    Path(__file__).resolve().parents[1] / "config" / "a3.example.json"
)

EXPECTED_GROUPS = {
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
    "capture_errors",
    "eligible_for_execution",
    "manifest_id",
    "manifest_sha256",
}

MANIFEST_ID_RE = re.compile(
    r"^a3-\d{8}-[0-9a-f]{7}-ubuntu2404-8c16t-32g-\d{3}$"
)


def load_fixture() -> dict:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def mutate(fixture: dict, *path: str, value) -> dict:
    actual = copy.deepcopy(fixture)
    node = actual
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return actual


class ManifestEqualityTests(unittest.TestCase):
    def test_identical_manifests_produce_no_differences(self):
        expected = load_fixture()
        actual = copy.deepcopy(expected)
        self.assertEqual(verify_manifest(expected, actual), [])


class FreezeDriftTests(unittest.TestCase):
    def setUp(self):
        self.expected = load_fixture()

    def test_map_server_binary_checksum_change(self):
        actual = mutate(self.expected, "build", "map_server_sha256", value="0" * 64)
        self.assertEqual(
            verify_manifest(self.expected, actual),
            ["build.map_server_sha256 changed"],
        )

    def test_login_server_binary_checksum_change(self):
        actual = mutate(self.expected, "build", "login_server_sha256", value="0" * 64)
        self.assertEqual(
            verify_manifest(self.expected, actual),
            ["build.login_server_sha256 changed"],
        )

    def test_workload_profile_checksum_change(self):
        actual = mutate(
            self.expected,
            "load_generation",
            "workload_profile_sha256",
            value="0" * 64,
        )
        self.assertEqual(
            verify_manifest(self.expected, actual),
            ["load_generation.workload_profile_sha256 changed"],
        )

    def test_kernel_version_change(self):
        actual = mutate(
            self.expected,
            "operating_system",
            "kernel_version",
            value="6.9.0-99-generic",
        )
        self.assertEqual(
            verify_manifest(self.expected, actual),
            ["operating_system.kernel_version changed"],
        )

    def test_dataset_seed_change(self):
        actual = mutate(self.expected, "database", "dataset_seed", value=1)
        self.assertEqual(
            verify_manifest(self.expected, actual),
            ["database.dataset_seed changed"],
        )

    def test_packetver_change(self):
        actual = mutate(self.expected, "protocol", "packetver", value=20220406)
        self.assertEqual(
            verify_manifest(self.expected, actual),
            ["protocol.packetver changed"],
        )

    def test_my_cnf_checksum_change(self):
        actual = mutate(self.expected, "database", "my_cnf_sha256", value="0" * 64)
        self.assertEqual(
            verify_manifest(self.expected, actual),
            ["database.my_cnf_sha256 changed"],
        )

    def test_script_tree_checksum_change(self):
        actual = mutate(
            self.expected, "game_content", "script_tree_sha256", value="0" * 64
        )
        self.assertEqual(
            verify_manifest(self.expected, actual),
            ["game_content.script_tree_sha256 changed"],
        )

    def test_exporter_version_change(self):
        actual = mutate(
            self.expected, "observability", "node_exporter_version", value="9.9.9"
        )
        self.assertEqual(
            verify_manifest(self.expected, actual),
            ["observability.node_exporter_version changed"],
        )

    def test_cpu_governor_change(self):
        actual = mutate(
            self.expected, "hardware", "cpu_governor", value="powersave"
        )
        self.assertEqual(
            verify_manifest(self.expected, actual),
            ["hardware.cpu_governor changed"],
        )

    def test_bios_power_profile_change(self):
        actual = mutate(
            self.expected, "hardware", "bios_power_profile", value="balanced"
        )
        self.assertEqual(
            verify_manifest(self.expected, actual),
            ["hardware.bios_power_profile changed"],
        )

    def test_missing_key_is_reported(self):
        actual = copy.deepcopy(self.expected)
        del actual["database"]["dataset_seed"]
        self.assertEqual(
            verify_manifest(self.expected, actual),
            ["database.dataset_seed missing"],
        )

    def test_unexpected_key_is_reported(self):
        actual = copy.deepcopy(self.expected)
        actual["database"]["unexpected_field"] = 1
        self.assertEqual(
            verify_manifest(self.expected, actual),
            ["database.unexpected_field unexpected"],
        )

    def test_manifest_sha256_is_ignored(self):
        actual = mutate(self.expected, "manifest_sha256", value="0" * 64)
        self.assertEqual(verify_manifest(self.expected, actual), [])

    def test_manifest_id_is_not_ignored(self):
        actual = mutate(
            self.expected,
            "manifest_id",
            value="a3-20260803-f82d9b0-ubuntu2404-8c16t-32g-002",
        )
        self.assertEqual(
            verify_manifest(self.expected, actual), ["manifest_id changed"]
        )

    def test_capture_errors_are_not_ignored(self):
        actual = mutate(self.expected, "capture_errors", value=[{"field": "x"}])
        self.assertEqual(
            verify_manifest(self.expected, actual), ["capture_errors changed"]
        )

    def test_eligible_for_execution_is_not_ignored(self):
        actual = mutate(self.expected, "eligible_for_execution", value=False)
        self.assertEqual(
            verify_manifest(self.expected, actual),
            ["eligible_for_execution changed"],
        )


class DeterminismTests(unittest.TestCase):
    def test_verify_manifest_returns_sorted_dotted_paths(self):
        expected = load_fixture()
        actual = copy.deepcopy(expected)
        actual["operating_system"]["kernel_version"] = "9.9.9"
        actual["build"]["map_server_sha256"] = "0" * 64
        actual["database"]["dataset_seed"] = 7
        self.assertEqual(
            verify_manifest(expected, actual),
            [
                "build.map_server_sha256 changed",
                "database.dataset_seed changed",
                "operating_system.kernel_version changed",
            ],
        )

    def test_manifest_id_is_stable_for_identical_canonical_content(self):
        fixture = load_fixture()
        self.assertEqual(manifest_id(fixture), fixture["manifest_id"])
        self.assertEqual(manifest_id(copy.deepcopy(fixture)), fixture["manifest_id"])

    def test_manifest_sha_is_stable_regardless_of_insertion_order(self):
        fixture = load_fixture()
        shuffled = {key: fixture[key] for key in reversed(list(fixture))}
        self.assertEqual(_manifest_sha256(shuffled), fixture["manifest_sha256"])

    def test_fixture_sha256_matches_canonical_recomputation(self):
        fixture = load_fixture()
        preimage = {
            key: value for key, value in fixture.items() if key != "manifest_sha256"
        }
        payload = json.dumps(
            preimage, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self.assertEqual(fixture["manifest_sha256"], expected)

    def test_capture_errors_are_part_of_hash_preimage(self):
        fixture = load_fixture()
        with_errors = copy.deepcopy(fixture)
        with_errors["capture_errors"] = [{"field": "x"}]
        self.assertNotEqual(
            _manifest_sha256(with_errors), _manifest_sha256(fixture)
        )


class ManifestIdFormatTests(unittest.TestCase):
    def test_fixture_manifest_id_matches_approved_format(self):
        fixture = load_fixture()
        self.assertRegex(fixture["manifest_id"], MANIFEST_ID_RE)
        sequence = fixture["manifest_id"].rsplit("-", 1)[-1]
        self.assertEqual(len(sequence), 3)

    def test_manifest_id_example_shape(self):
        manifest = {
            "created_utc": "2026-08-02T19:00:00Z",
            "sequence": 1,
            "source": {"git_short_sha": "f82d9b0"},
        }
        self.assertEqual(
            manifest_id(manifest), "a3-20260802-f82d9b0-ubuntu2404-8c16t-32g-001"
        )

    def test_missing_created_utc_raises_and_names_field(self):
        manifest = {"sequence": 1, "source": {"git_short_sha": "f82d9b0"}}
        with self.assertRaises(ValueError) as ctx:
            manifest_id(manifest)
        self.assertIn("created_utc", str(ctx.exception))

    def test_invalid_git_short_sha_raises_and_names_field(self):
        manifest = {
            "created_utc": "2026-08-02T19:00:00Z",
            "sequence": 1,
            "source": {"git_short_sha": "XYZ"},
        }
        with self.assertRaises(ValueError) as ctx:
            manifest_id(manifest)
        self.assertIn("git_short_sha", str(ctx.exception))

    def test_missing_sequence_raises_and_names_field(self):
        manifest = {
            "created_utc": "2026-08-02T19:00:00Z",
            "source": {"git_short_sha": "f82d9b0"},
        }
        with self.assertRaises(ValueError) as ctx:
            manifest_id(manifest)
        self.assertIn("sequence", str(ctx.exception))

    def test_out_of_range_sequence_raises_and_names_field(self):
        manifest = {
            "created_utc": "2026-08-02T19:00:00Z",
            "sequence": 1000,
            "source": {"git_short_sha": "f82d9b0"},
        }
        with self.assertRaises(ValueError) as ctx:
            manifest_id(manifest)
        self.assertIn("sequence", str(ctx.exception))


GIT_SHA = "f82d9b00e28d6b8dba6abddce90ed50a433d42a1"
PACKET_DB_SHA = "1" * 40
SCHEMA_SHA = "2" * 40
HARNESS_TREE_SHA = "3" * 40


def _completed(argv, stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess(
        argv, rc, stdout.encode("utf-8"), stderr.encode("utf-8")
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def build_fake_repo(root: Path) -> None:
    _write(root / "src" / "config" / "packets.hpp", "#define PACKETVER 20211103\n")
    _write(root / "conf" / "login_athena.conf", "// login\n")
    _write(root / "conf" / "char_athena.conf", "// char\n")
    _write(root / "conf" / "map_athena.conf", "// map\n")
    _write(root / "conf" / "inter_athena.conf", "// inter\n")
    _write(root / "tools" / "observability" / "observer.example.json", "{}\n")
    _write(
        root / "tools" / "performance" / "a3" / "config" / "slo-thresholds.json",
        json.dumps({"sql_ms": {"p95_max": 25}, "script_ms": {"p95_max": 5}}),
    )
    _write(
        root / "tools" / "performance" / "a3" / "config" / "workload-profile.json",
        json.dumps({"profile_id": "a3-mixed-gameplay-v1"}),
    )
    _write(root / "db" / "item_db.yml", "Body: []\n")
    _write(root / "db" / "mob_db.yml", "Body: []\n")
    _write(root / "db" / "skill_db.yml", "Body: []\n")
    _write(root / "db" / "map_index.txt", "prontera\n")
    _write(root / "sql-files" / "main.sql", "CREATE TABLE x (id INT);\n")
    _write(root / "npc" / "core.txt", "npc\n")
    _write(root / "npc" / "custom" / "note.txt", "custom\n")
    _write(root / "login-server", "login-binary\n")
    _write(root / "char-server", "char-binary\n")
    _write(root / "map-server", "map-binary\n")
    _write(
        root / "tools" / "performance" / "a3" / "bin" / "a3-load-harness",
        "harness-binary\n",
    )
    _write(
        root / "build" / "CMakeCache.txt",
        "CMAKE_BUILD_TYPE:STRING=Release\nCMAKE_CXX_FLAGS:STRING=-O2\n",
    )


def build_host_files(root: Path) -> dict:
    host = root / "host"
    files = {
        "MY_CNF_PATH": "[mysqld]\n",
        "OS_RELEASE_PATH": 'ID=ubuntu\nVERSION_ID="24.04"\n',
        "NVME_MODEL_PATH": "Samsung MZQL23T8HCLS-00A07\n",
        "NVME_FIRMWARE_PATH": "GDC5602Q\n",
        "BIOS_VERSION_PATH": "2.7.0\n",
        "CPU_GOVERNOR_PATH": "performance\n",
        "BIOS_POWER_PROFILE_PATH": "performance\n",
        "NIC_SPEED_PATH": "10000\n",
        "PROMETHEUS_CONFIG_PATH": "global: {}\n",
        "GRAFANA_DASHBOARD_PATH": "{}\n",
    }
    paths = {}
    for name, content in files.items():
        path = host / (name.lower() + ".txt")
        _write(path, content)
        paths[name] = path
    return paths


def make_dispatcher(fail_command=None, timeout_command=None, calls=None):
    def fake_run(argv, **kwargs):
        if calls is not None:
            calls.append((list(argv), dict(kwargs)))
        joined = " ".join(str(part) for part in argv)
        if fail_command is not None and joined == " ".join(fail_command):
            return _completed(argv, rc=1, stderr="fatal: synthetic boom")
        if timeout_command is not None and joined == " ".join(timeout_command):
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=10)
        if "remote.origin.url" in joined:
            return _completed(argv, "https://github.com/kongsak4807017/rathena\n")
        if "--abbrev-ref" in joined:
            return _completed(argv, "feat/a3-baseline-toolchain\n")
        if "rev-parse" in joined and "HEAD" in argv:
            return _completed(argv, GIT_SHA + "\n")
        if "status" in argv and "--porcelain" in argv:
            return _completed(argv, "")
        if "submodule" in argv:
            return _completed(argv, "")
        if "db/packet_db.yml" in joined:
            return _completed(argv, PACKET_DB_SHA + "\n")
        if "sql-files" in joined:
            return _completed(argv, SCHEMA_SHA + "\n")
        if "tools/performance/a3" in joined:
            return _completed(argv, HARNESS_TREE_SHA + "\n")
        if argv[:1] == ["cc"] and "--version" in argv:
            return _completed(argv, "gcc (Ubuntu 13.2.0-4ubuntu3) 13.2.0\n")
        if argv[:1] == ["cc"]:
            return _completed(argv, "13.2.0\n")
        if argv[:1] == ["mariadb"]:
            return _completed(
                argv, "mariadb  Ver 15.1 Distrib 10.11.8-MariaDB, for linux\n"
            )
        if argv[:1] == ["dpkg-query"]:
            return _completed(argv, "adduser=3.137ubuntu1\n")
        if argv[:1] == ["findmnt"] and "FSTYPE" in argv:
            return _completed(argv, "ext4\n")
        if argv[:1] == ["findmnt"]:
            return _completed(argv, "rw,noatime\n")
        if argv[:1] == ["timedatectl"]:
            return _completed(argv, "UTC\n")
        if argv[:1] == ["chronyc"]:
            return _completed(argv, "MS,^*,ntp.ubuntu.com,3,6,377\n")
        if argv[:1] == ["uname"]:
            return _completed(argv, "6.8.0-41-generic\n")
        if argv[:1] == ["lscpu"]:
            return _completed(
                argv,
                "Model name: AMD EPYC 7763 64-Core Processor\n"
                "Socket(s): 1\n"
                "Core(s) per socket: 8\n"
                "NUMA node(s): 1\n",
            )
        if argv[:1] == ["nproc"]:
            return _completed(argv, "16\n")
        if argv[:1] == ["free"]:
            return _completed(
                argv,
                "               total         used         free\n"
                "Mem:     33734471680   1073741824   32660729856\n",
            )
        if argv[:1] == ["lspci"]:
            return _completed(
                argv,
                "00:03.0 Ethernet controller: Mellanox Technologies "
                "MT27710 Family [ConnectX-4 Lx]\n",
            )
        if argv[:1] == ["prometheus"]:
            return _completed(argv, "prometheus, version 2.53.0 (branch: HEAD)\n")
        if argv[:1] == ["node_exporter"]:
            return _completed(argv, "node_exporter, version 1.8.2 (branch: HEAD)\n")
        if argv[:1] == ["mysqld_exporter"]:
            return _completed(argv, "mysqld_exporter, version 0.15.1 (branch: HEAD)\n")
        if argv[:1] == ["grafana-server"]:
            return _completed(argv, "Version 11.1.0 (commit: abc123, branch: HEAD)\n")
        raise AssertionError(f"unexpected command: {joined}")

    return fake_run


class CaptureManifestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        build_fake_repo(self.repo)
        self.host_paths = build_host_files(Path(self._tmp.name))
        self.config = load_config(EXAMPLE_CONFIG)

    def _capture(self, dispatcher):
        patches = [
            mock.patch("tools.performance.a3.manifest.subprocess.run", dispatcher),
        ]
        for name, path in self.host_paths.items():
            patches.append(
                mock.patch(f"tools.performance.a3.manifest.{name}", path)
            )
        for patcher in patches:
            self.addCleanup(patcher.stop)
            patcher.start()
        return capture_manifest(self.repo, self.config)

    def test_capture_produces_all_required_groups(self):
        manifest = self._capture(make_dispatcher())
        self.assertTrue(EXPECTED_GROUPS.issubset(set(manifest)))

    def test_capture_is_eligible_with_no_errors(self):
        manifest = self._capture(make_dispatcher())
        self.assertEqual(manifest["capture_errors"], [])
        self.assertIs(manifest["eligible_for_execution"], True)

    def test_capture_computes_matching_id_and_sha(self):
        manifest = self._capture(make_dispatcher())
        self.assertRegex(manifest["manifest_id"], MANIFEST_ID_RE)
        self.assertEqual(manifest_id(manifest), manifest["manifest_id"])
        self.assertEqual(_manifest_sha256(manifest), manifest["manifest_sha256"])

    def test_capture_freezes_expected_values(self):
        manifest = self._capture(make_dispatcher())
        self.assertEqual(manifest["source"]["git_commit_sha"], GIT_SHA)
        self.assertEqual(manifest["source"]["git_short_sha"], "f82d9b0")
        self.assertIs(manifest["source"]["working_tree_clean"], True)
        self.assertEqual(manifest["protocol"]["packetver"], 20211103)
        self.assertEqual(manifest["build"]["compiler"], "gcc")
        self.assertEqual(manifest["build"]["compiler_version"], "13.2.0")
        self.assertEqual(manifest["build"]["build_type"], "Release")
        self.assertEqual(
            manifest["rathena_configuration"]["slow_sql_threshold"], 25
        )
        self.assertEqual(
            manifest["rathena_configuration"]["slow_script_threshold"], 5
        )
        self.assertEqual(
            manifest["rathena_configuration"]["snapshot_interval_seconds"], 5
        )
        self.assertEqual(manifest["database"]["dataset_seed"], 20260802)
        self.assertEqual(manifest["database"]["mariadb_version"], "10.11.8-MariaDB")
        self.assertEqual(manifest["operating_system"]["distribution"], "ubuntu")
        self.assertEqual(manifest["operating_system"]["distribution_version"], "24.04")
        self.assertEqual(
            manifest["operating_system"]["kernel_version"], "6.8.0-41-generic"
        )
        self.assertEqual(manifest["hardware"]["physical_cores"], 8)
        self.assertEqual(manifest["hardware"]["logical_threads"], 16)
        self.assertEqual(manifest["hardware"]["ram_bytes"], 33734471680)
        self.assertEqual(manifest["hardware"]["cpu_governor"], "performance")
        self.assertEqual(manifest["hardware"]["bios_power_profile"], "performance")
        self.assertEqual(manifest["hardware"]["link_speed_mbps"], 10000)
        self.assertEqual(manifest["observability"]["prometheus_version"], "2.53.0")
        self.assertEqual(manifest["observability"]["node_exporter_version"], "1.8.2")
        self.assertEqual(
            manifest["observability"]["mariadb_exporter_version"], "0.15.1"
        )
        self.assertEqual(manifest["observability"]["grafana_version"], "11.1.0")
        self.assertEqual(manifest["load_generation"]["account_range"], "1-6000")
        self.assertEqual(manifest["load_generation"]["random_seed"], 20260802)

    def test_capture_uses_argument_arrays_without_shell(self):
        calls = []
        self._capture(make_dispatcher(calls=calls))
        self.assertTrue(calls)
        for argv, kwargs in calls:
            self.assertIsInstance(argv, list)
            self.assertIs(kwargs.get("shell"), False)
            self.assertIsNotNone(kwargs.get("timeout"))

    def test_failed_command_is_recorded_and_marks_ineligible(self):
        fail = ["git", "-C", str(self.repo), "rev-parse", "HEAD"]
        manifest = self._capture(make_dispatcher(fail_command=fail))
        self.assertIs(manifest["eligible_for_execution"], False)
        self.assertIsNone(manifest["manifest_id"])
        matches = [
            error
            for error in manifest["capture_errors"]
            if error["field"] == "source.git_commit_sha"
        ]
        self.assertEqual(len(matches), 1)
        entry = matches[0]
        self.assertEqual(entry["command"], fail)
        self.assertEqual(entry["return_code"], 1)
        self.assertIn("boom", entry["stderr"])

    def test_timed_out_command_is_recorded_and_marks_ineligible(self):
        timeout = ["chronyc", "-c", "sources"]
        manifest = self._capture(make_dispatcher(timeout_command=timeout))
        self.assertIs(manifest["eligible_for_execution"], False)
        matches = [
            error
            for error in manifest["capture_errors"]
            if error["field"] == "operating_system.time_sync_source"
        ]
        self.assertEqual(len(matches), 1)
        entry = matches[0]
        self.assertEqual(entry["command"], timeout)
        self.assertIn("timeout", entry)
        self.assertIn("return_code", entry)
        self.assertIsNone(entry["return_code"])


class RedactionTests(unittest.TestCase):
    def test_redacts_sensitive_keys_case_insensitively(self):
        value = {
            "db_password": "hunter2",
            "API_Token": "abc",
            "client_secret": "xyz",
            "service_api_key": "key",
            "ssh_private_key": "pem",
            "nested": {"mysql_password": "pw", "plain": "ok"},
            "list": [{"oauth_token": "t"}],
            "ordinary": "value",
        }
        redacted = _redact(value)
        for key in ("db_password", "API_Token", "client_secret", "service_api_key",
                    "ssh_private_key"):
            self.assertEqual(redacted[key], "<redacted>")
        self.assertEqual(redacted["nested"]["mysql_password"], "<redacted>")
        self.assertEqual(redacted["nested"]["plain"], "ok")
        self.assertEqual(redacted["list"][0]["oauth_token"], "<redacted>")
        self.assertEqual(redacted["ordinary"], "value")


if __name__ == "__main__":
    unittest.main()
