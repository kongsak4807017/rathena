"""Tests for A3 approval governance and approved-baseline lifecycle."""

import copy
import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.performance.a3.approval import (
    ApprovalError,
    ApprovalRecord,
    ApprovalState,
    SupersessionRecord,
    approve_baseline,
    create_approval_record,
    is_transition_allowed,
    reject_baseline,
    supersede_baseline,
)
from tools.performance.a3.models import CapacityVerdict, MetricVerdict

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "approved-baseline.schema.json"
)

CYCLE = "cycle-2026-08"
MANIFEST_ID = "a3-20260802-f82d9b0-ubuntu2404-8c16t-32g-001"
GIT_SHA = "f82d9b00e28d6b8dba6abddce90ed50a433d42a1"
REPORT_SHA = "a" * 64
UTC = "2026-08-02T21:00:00Z"
APPROVER = "J. Operator"
RATIONALE = "Reviewed all artifacts; capacity is acceptable."


def cycle_summary(**overrides) -> dict:
    summary = {
        "state": "AWAITING_APPROVAL",
        "baseline_cycle_id": CYCLE,
        "manifest_id": MANIFEST_ID,
        "git_sha": GIT_SHA,
        "capacity": {
            "verdict": "PASS",
            "safe_capacity": 5000,
            "conditional_capacity": None,
            "tested_ceiling": 5000,
            "first_degradation_level": None,
            "notes": [],
        },
        "levels": {
            "500": {"verdict": "PASS"},
            "1000": {"verdict": "PASS"},
            "2500": {"verdict": "PASS"},
            "5000": {"verdict": "PASS"},
        },
        "warnings": [],
        "report_checksums_sha256": REPORT_SHA,
        "ci": {"evaluated": True, "status": "success"},
    }
    summary.update(overrides)
    return summary


def warning_cycle() -> dict:
    summary = cycle_summary()
    summary["capacity"]["verdict"] = "PASS_WITH_WARNING"
    summary["capacity"]["conditional_capacity"] = 5000
    summary["capacity"]["safe_capacity"] = 2500
    summary["warnings"] = ["cpu.p95_percent near hard limit"]
    return summary


class ApprovalTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.artifact_root = Path(self._tmp.name) / "root"
        self.artifact_root.mkdir()

    def approval_dir(self, cycle=CYCLE) -> Path:
        return (
            self.artifact_root
            / "artifacts"
            / "performance"
            / "a3"
            / cycle
            / "approval"
        )


class StateTransitionTests(unittest.TestCase):
    def test_allowed_transitions(self):
        allowed = (
            ("DRAFT", "CI_EVALUATED"),
            ("CI_EVALUATED", "AWAITING_APPROVAL"),
            ("AWAITING_APPROVAL", "APPROVED"),
            ("AWAITING_APPROVAL", "REJECTED"),
            ("APPROVED", "SUPERSEDED"),
        )
        for source, target in allowed:
            with self.subTest(source=source, target=target):
                self.assertTrue(
                    is_transition_allowed(
                        ApprovalState(source), ApprovalState(target)
                    )
                )

    def test_forbidden_transitions(self):
        forbidden = (
            ("DRAFT", "APPROVED"),
            ("DRAFT", "REJECTED"),
            ("DRAFT", "AWAITING_APPROVAL"),
            ("CI_EVALUATED", "APPROVED"),
            ("CI_EVALUATED", "REJECTED"),
            ("APPROVED", "APPROVED"),
            ("APPROVED", "REJECTED"),
            ("REJECTED", "APPROVED"),
            ("REJECTED", "SUPERSEDED"),
            ("REJECTED", "CI_EVALUATED"),
        )
        for source, target in forbidden:
            with self.subTest(source=source, target=target):
                self.assertFalse(
                    is_transition_allowed(
                        ApprovalState(source), ApprovalState(target)
                    )
                )

    def test_superseded_is_terminal(self):
        for target in ApprovalState:
            self.assertFalse(
                is_transition_allowed(ApprovalState.SUPERSEDED, target)
            )

    def test_state_values(self):
        self.assertEqual(
            {state.value for state in ApprovalState},
            {
                "DRAFT",
                "CI_EVALUATED",
                "AWAITING_APPROVAL",
                "APPROVED",
                "REJECTED",
                "SUPERSEDED",
            },
        )


class ApprovalEligibilityTests(ApprovalTestBase):
    def test_valid_pass_approval(self):
        record = create_approval_record(cycle_summary(), APPROVER, RATIONALE, UTC)
        self.assertIsInstance(record, ApprovalRecord)
        self.assertIs(record.state, ApprovalState.APPROVED)
        self.assertIs(record.capacity_verdict, CapacityVerdict.PASS)
        self.assertEqual(record.safe_capacity, 5000)
        self.assertEqual(record.per_level_verdicts[500], MetricVerdict.PASS)
        self.assertEqual(record.version, 1)

    def test_valid_warning_approval(self):
        record = create_approval_record(
            warning_cycle(), APPROVER, RATIONALE + " warning acknowledged", UTC
        )
        self.assertIs(record.state, ApprovalState.APPROVED)
        self.assertIs(record.capacity_verdict, CapacityVerdict.PASS_WITH_WARNING)

    def test_warning_requires_acknowledgement_word(self):
        with self.assertRaises(ValueError):
            create_approval_record(warning_cycle(), APPROVER, RATIONALE, UTC)

    def test_warning_requires_non_empty_warnings(self):
        cycle = warning_cycle()
        cycle["warnings"] = []
        with self.assertRaises(ValueError):
            create_approval_record(
                cycle, APPROVER, RATIONALE + " warning acknowledged", UTC
            )

    def test_blocked_cycle_rejected(self):
        cycle = cycle_summary()
        cycle["capacity"]["verdict"] = "BLOCKED"
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)

    def test_fail_cycle_rejected(self):
        cycle = cycle_summary()
        cycle["capacity"]["verdict"] = "FAIL"
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)

    def test_not_established_rejected(self):
        cycle = cycle_summary()
        cycle["capacity"]["verdict"] = "NOT_ESTABLISHED"
        cycle["capacity"]["safe_capacity"] = None
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)

    def test_missing_safe_capacity_rejected(self):
        cycle = cycle_summary()
        cycle["capacity"]["safe_capacity"] = None
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)

    def test_missing_tested_ceiling_rejected(self):
        cycle = cycle_summary()
        cycle["capacity"]["tested_ceiling"] = None
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)

    def test_safe_above_ceiling_rejected(self):
        cycle = cycle_summary()
        cycle["capacity"]["safe_capacity"] = 5000
        cycle["capacity"]["tested_ceiling"] = 2500
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)

    def test_conditional_below_safe_rejected(self):
        cycle = warning_cycle()
        cycle["capacity"]["conditional_capacity"] = 1000
        with self.assertRaises(ValueError):
            create_approval_record(
                cycle, APPROVER, RATIONALE + " warning acknowledged", UTC
            )

    def test_invalid_capacity_value_rejected(self):
        cycle = cycle_summary()
        cycle["capacity"]["safe_capacity"] = 750
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)

    def test_ci_not_evaluated_rejected(self):
        cycle = cycle_summary()
        cycle["ci"]["evaluated"] = False
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)

    def test_ci_failed_rejected(self):
        cycle = cycle_summary()
        cycle["ci"]["status"] = "failure"
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)

    def test_wrong_source_state_rejected(self):
        for state in ("DRAFT", "CI_EVALUATED", "APPROVED", "REJECTED", "SUPERSEDED"):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    create_approval_record(
                        cycle_summary(state=state), APPROVER, RATIONALE, UTC
                    )

    def test_missing_500_level_rejected(self):
        cycle = cycle_summary()
        del cycle["levels"]["500"]
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)

    def test_unexpected_level_rejected(self):
        cycle = cycle_summary()
        cycle["levels"]["750"] = {"verdict": "PASS"}
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)

    def test_blocked_level_rejected(self):
        cycle = cycle_summary()
        cycle["levels"]["2500"] = {"verdict": "BLOCKED"}
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)

    def test_fail_level_allowed(self):
        cycle = cycle_summary()
        cycle["levels"]["5000"] = {"verdict": "FAIL"}
        record = create_approval_record(cycle, APPROVER, RATIONALE, UTC)
        self.assertIs(record.state, ApprovalState.APPROVED)

    def test_malformed_git_sha_rejected(self):
        with self.assertRaises(ValueError):
            create_approval_record(
                cycle_summary(git_sha="nothex"), APPROVER, RATIONALE, UTC
            )

    def test_malformed_report_checksum_rejected(self):
        with self.assertRaises(ValueError):
            create_approval_record(
                cycle_summary(report_checksums_sha256="zz"), APPROVER, RATIONALE, UTC
            )

    def test_unsafe_identifiers_rejected(self):
        for field, value in (
            ("baseline_cycle_id", "../x"),
            ("baseline_cycle_id", ""),
            ("manifest_id", "a/b"),
            ("manifest_id", "x" * 129),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    create_approval_record(
                        cycle_summary(**{field: value}), APPROVER, RATIONALE, UTC
                    )

    def test_catastrophic_marker_rejected(self):
        cycle = cycle_summary(catastrophic=True)
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)
        cycle = cycle_summary()
        cycle["levels"]["500"]["catastrophic"] = True
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)

    def test_manifest_mismatch_across_levels_rejected(self):
        cycle = cycle_summary()
        cycle["levels"]["500"]["manifest_id"] = "a3-20260802-0000000-ubuntu2404-8c16t-32g-999"
        with self.assertRaises(ValueError):
            create_approval_record(cycle, APPROVER, RATIONALE, UTC)


class HumanFieldTests(ApprovalTestBase):
    def test_unicode_approver_accepted(self):
        record = create_approval_record(
            cycle_summary(), "José Öperator 運営", RATIONALE, UTC
        )
        self.assertEqual(record.approver, "José Öperator 運営")

    def test_approver_trimmed(self):
        record = create_approval_record(cycle_summary(), "  J. Operator  ", RATIONALE, UTC)
        self.assertEqual(record.approver, "J. Operator")

    def test_empty_or_whitespace_approver_rejected(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                create_approval_record(cycle_summary(), bad, RATIONALE, UTC)

    def test_approver_control_and_separator_rejected(self):
        for bad in ("a\x00b", "a\x07b", "a/b", "a\\b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                create_approval_record(cycle_summary(), bad, RATIONALE, UTC)

    def test_approver_too_long_rejected(self):
        with self.assertRaises(ValueError):
            create_approval_record(cycle_summary(), "a" * 201, RATIONALE, UTC)

    def test_rationale_too_short_rejected(self):
        with self.assertRaises(ValueError):
            create_approval_record(cycle_summary(), APPROVER, "too short", UTC)

    def test_rationale_whitespace_only_rejected(self):
        with self.assertRaises(ValueError):
            create_approval_record(cycle_summary(), APPROVER, "           ", UTC)

    def test_rationale_too_long_rejected(self):
        with self.assertRaises(ValueError):
            create_approval_record(cycle_summary(), APPROVER, "a" * 4001, UTC)

    def test_multiline_rationale_preserved(self):
        rationale = "Line one of review.\nLine two with detail.\nLine three."
        record = create_approval_record(cycle_summary(), APPROVER, rationale, UTC)
        self.assertIn("\n", record.rationale)

    def test_secret_markers_rejected(self):
        for marker in ("password", "TOKEN", "Secret", "api_key", "PRIVATE_KEY", "authorization", "bearer"):
            with self.subTest(marker=marker):
                with self.assertRaises(ValueError):
                    create_approval_record(
                        cycle_summary(), f"user {marker}", RATIONALE, UTC
                    )
                with self.assertRaises(ValueError):
                    create_approval_record(
                        cycle_summary(), APPROVER, f"rationale mentions {marker} here", UTC
                    )
                cycle = cycle_summary(warnings=[f"has {marker} inside"])
                with self.assertRaises(ValueError):
                    create_approval_record(cycle, APPROVER, RATIONALE, UTC)

    def test_exception_redacts_secret_value(self):
        sentinel = "hunter2s3ntinel"
        with self.assertRaises(ValueError) as ctx:
            create_approval_record(
                cycle_summary(), f"password {sentinel}", RATIONALE, UTC
            )
        self.assertNotIn(sentinel, str(ctx.exception))


class UtcValidationTests(ApprovalTestBase):
    def test_valid_leap_day(self):
        record = create_approval_record(
            cycle_summary(), APPROVER, RATIONALE, "2024-02-29T23:59:59Z"
        )
        self.assertEqual(record.approved_utc, "2024-02-29T23:59:59Z")

    def test_invalid_date_rejected(self):
        for bad in ("2023-02-29T00:00:00Z", "2026-13-01T00:00:00Z", "2026-08-02T25:00:00Z"):
            with self.assertRaises(ValueError, msg=bad):
                create_approval_record(cycle_summary(), APPROVER, RATIONALE, bad)

    def test_fractional_seconds_rejected(self):
        with self.assertRaises(ValueError):
            create_approval_record(
                cycle_summary(), APPROVER, RATIONALE, "2026-08-02T21:00:00.000Z"
            )

    def test_offset_rejected(self):
        with self.assertRaises(ValueError):
            create_approval_record(
                cycle_summary(), APPROVER, RATIONALE, "2026-08-02T21:00:00+07:00"
            )

    def test_missing_z_rejected(self):
        with self.assertRaises(ValueError):
            create_approval_record(
                cycle_summary(), APPROVER, RATIONALE, "2026-08-02T21:00:00"
            )


class ApprovalFilesystemTests(ApprovalTestBase):
    def test_exact_paths_and_sidecar_bytes(self):
        result = approve_baseline(
            self.artifact_root, cycle_summary(), APPROVER, RATIONALE, UTC
        )
        self.assertTrue(result.written)
        approval_dir = self.approval_dir()
        self.assertEqual(result.approval_path, approval_dir / "approved-baseline.json")
        self.assertEqual(
            result.checksum_path, approval_dir / "approved-baseline.sha256"
        )
        self.assertTrue(result.approval_path.is_file())
        sidecar = result.checksum_path.read_bytes()
        expected = (
            f"{result.record.approval_record_sha256}  approved-baseline.json\n"
        ).encode("utf-8")
        self.assertEqual(sidecar, expected)

    def test_hash_field_sidecar_recomputed_agree(self):
        result = approve_baseline(
            self.artifact_root, cycle_summary(), APPROVER, RATIONALE, UTC
        )
        payload = json.loads(result.approval_path.read_text(encoding="utf-8"))
        recorded = payload.pop("approval_record_sha256")
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        recomputed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.assertEqual(recorded, recomputed)
        self.assertEqual(recorded, result.record.approval_record_sha256)
        sidecar = result.checksum_path.read_text(encoding="utf-8")
        self.assertTrue(sidecar.startswith(recomputed))

    def test_deterministic_byte_output(self):
        first = approve_baseline(
            self.artifact_root, cycle_summary(), APPROVER, RATIONALE, UTC
        )
        second_root = Path(self._tmp.name) / "root2"
        second_root.mkdir()
        second = approve_baseline(
            second_root, cycle_summary(), APPROVER, RATIONALE, UTC
        )
        self.assertEqual(
            first.approval_path.read_bytes(), second.approval_path.read_bytes()
        )
        self.assertEqual(
            first.record.approval_record_sha256, second.record.approval_record_sha256
        )

    def test_input_field_order_independence(self):
        first = create_approval_record(cycle_summary(), APPROVER, RATIONALE, UTC)
        reordered = dict(reversed(list(cycle_summary().items())))
        second = create_approval_record(reordered, APPROVER, RATIONALE, UTC)
        self.assertEqual(first, second)

    def test_no_temp_files_after_write(self):
        self.assertEqual(list(self.approval_dir().rglob("*.tmp")), [])

    def test_overwrite_refused_by_default(self):
        approve_baseline(self.artifact_root, cycle_summary(), APPROVER, RATIONALE, UTC)
        with self.assertRaises(ApprovalError):
            approve_baseline(
                self.artifact_root, cycle_summary(), APPROVER, RATIONALE, UTC
            )

    def test_overwrite_true_still_refuses_existing_approved(self):
        approve_baseline(self.artifact_root, cycle_summary(), APPROVER, RATIONALE, UTC)
        with self.assertRaises(ApprovalError):
            approve_baseline(
                self.artifact_root,
                cycle_summary(),
                APPROVER,
                RATIONALE,
                UTC,
                overwrite=True,
            )

    def test_overwrite_true_retries_non_approved_output(self):
        approval_dir = self.approval_dir()
        approval_dir.mkdir(parents=True)
        stale = approval_dir / "approved-baseline.json"
        stale.write_text('{"state": "DRAFT"}\n', encoding="utf-8")
        result = approve_baseline(
            self.artifact_root,
            cycle_summary(),
            APPROVER,
            RATIONALE,
            UTC,
            overwrite=True,
        )
        self.assertTrue(result.written)
        payload = json.loads(stale.read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "APPROVED")

    def test_symlink_rejection(self):
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ApprovalError):
                approve_baseline(
                    self.artifact_root, cycle_summary(), APPROVER, RATIONALE, UTC
                )

    def test_no_sidecar_when_json_write_fails(self):
        from tools.performance.a3 import approval as approval_module

        original = approval_module._atomic_write_bytes
        calls = []

        def flaky(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            return original(path, data)

        with mock.patch.object(approval_module, "_atomic_write_bytes", flaky):
            with self.assertRaises(OSError):
                approve_baseline(
                    self.artifact_root, cycle_summary(), APPROVER, RATIONALE, UTC
                )
        approval_dir = self.approval_dir()
        self.assertFalse((approval_dir / "approved-baseline.sha256").exists())
        self.assertEqual(list(approval_dir.rglob("*.tmp")), [])


class RejectionTests(ApprovalTestBase):
    def test_valid_rejection(self):
        result = reject_baseline(
            self.artifact_root, cycle_summary(), APPROVER, "Does not meet the bar.", "2026-08-03T00:00:00Z"
        )
        self.assertTrue(result.written)
        self.assertIs(result.record.state, ApprovalState.REJECTED)
        approval_dir = self.approval_dir()
        self.assertTrue((approval_dir / "rejected-baseline.json").is_file())
        sidecar = (approval_dir / "rejected-baseline.sha256").read_bytes()
        expected = (
            f"{result.record.approval_record_sha256}  rejected-baseline.json\n"
        ).encode("utf-8")
        self.assertEqual(sidecar, expected)
        payload = json.loads((approval_dir / "rejected-baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "REJECTED")
        self.assertEqual(payload["approved_utc"], "2026-08-03T00:00:00Z")

    def test_rejection_only_from_awaiting(self):
        for state in ("DRAFT", "CI_EVALUATED", "APPROVED", "REJECTED", "SUPERSEDED"):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    reject_baseline(
                        self.artifact_root,
                        cycle_summary(state=state),
                        APPROVER,
                        "Does not meet the bar.",
                        UTC,
                    )

    def test_rejection_writes_no_approved_file(self):
        reject_baseline(
            self.artifact_root, cycle_summary(), APPROVER, "Does not meet the bar.", UTC
        )
        self.assertFalse((self.approval_dir() / "approved-baseline.json").exists())

    def test_rejection_overwrite_protection(self):
        reject_baseline(
            self.artifact_root, cycle_summary(), APPROVER, "Does not meet the bar.", UTC
        )
        with self.assertRaises(ApprovalError):
            reject_baseline(
                self.artifact_root, cycle_summary(), APPROVER, "Does not meet the bar.", UTC
            )

    def test_rejection_cannot_overwrite_approved_record(self):
        approve_baseline(self.artifact_root, cycle_summary(), APPROVER, RATIONALE, UTC)
        with self.assertRaises(ApprovalError):
            reject_baseline(
                self.artifact_root, cycle_summary(), APPROVER, "Does not meet the bar.", UTC
            )

    def test_rejection_requires_human_fields(self):
        with self.assertRaises(ValueError):
            reject_baseline(self.artifact_root, cycle_summary(), "", "Does not meet the bar.", UTC)
        with self.assertRaises(ValueError):
            reject_baseline(self.artifact_root, cycle_summary(), APPROVER, "short", UTC)


class SupersessionTests(ApprovalTestBase):
    def _approvals(self):
        first = approve_baseline(
            self.artifact_root, cycle_summary(), APPROVER, RATIONALE, UTC
        )
        second_cycle = cycle_summary(
            baseline_cycle_id="cycle-2026-09",
            manifest_id="a3-20260901-b2c3d4e-ubuntu2404-8c16t-32g-001",
        )
        second = approve_baseline(
            self.artifact_root, second_cycle, APPROVER, RATIONALE, "2026-09-01T00:00:00Z"
        )
        return first, second

    def _record_payload(self, result):
        return json.loads(result.approval_path.read_text(encoding="utf-8"))

    def test_valid_supersession(self):
        first, second = self._approvals()
        record = supersede_baseline(
            self.artifact_root,
            self._record_payload(first),
            self._record_payload(second),
            APPROVER,
            "Superseding with the September cycle.",
            "2026-09-02T00:00:00Z",
        )
        self.assertIsInstance(record, SupersessionRecord)
        self.assertIs(record.state, ApprovalState.SUPERSEDED)
        self.assertEqual(record.previous_baseline_cycle_id, CYCLE)
        self.assertEqual(record.new_baseline_cycle_id, "cycle-2026-09")
        self.assertEqual(record.previous_approval_sha256, first.record.approval_record_sha256)
        self.assertEqual(record.new_approval_sha256, second.record.approval_record_sha256)
        supersession_path = self.approval_dir("cycle-2026-09") / "supersession.json"
        self.assertTrue(supersession_path.is_file())
        sidecar = (self.approval_dir("cycle-2026-09") / "supersession.sha256").read_bytes()
        expected = (f"{record.supersession_sha256}  supersession.json\n").encode("utf-8")
        self.assertEqual(sidecar, expected)

    def test_old_record_preserved(self):
        first, second = self._approvals()
        before = first.approval_path.read_bytes()
        supersede_baseline(
            self.artifact_root,
            self._record_payload(first),
            self._record_payload(second),
            APPROVER,
            "Superseding with the September cycle.",
            "2026-09-02T00:00:00Z",
        )
        self.assertEqual(first.approval_path.read_bytes(), before)
        payload = self._record_payload(first)
        self.assertEqual(payload["state"], "APPROVED")

    def test_same_cycle_rejected(self):
        first, second = self._approvals()
        payload = self._record_payload(second)
        with self.assertRaises(ValueError):
            supersede_baseline(
                self.artifact_root, payload, payload, APPROVER, "Superseding with detail.", "2026-09-02T00:00:00Z"
            )

    def test_same_manifest_rejected(self):
        first, second = self._approvals()
        new_payload = self._record_payload(second)
        new_payload["manifest_id"] = MANIFEST_ID
        with self.assertRaises(ValueError):
            supersede_baseline(
                self.artifact_root, self._record_payload(first), new_payload, APPROVER, "Superseding with detail.", "2026-09-02T00:00:00Z"
            )

    def test_invalid_checksums_rejected(self):
        first, second = self._approvals()
        old_payload = self._record_payload(first)
        old_payload["approval_record_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            supersede_baseline(
                self.artifact_root, old_payload, self._record_payload(second), APPROVER, "Superseding with detail.", "2026-09-02T00:00:00Z"
            )
        new_payload = self._record_payload(second)
        new_payload["approval_record_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            supersede_baseline(
                self.artifact_root, self._record_payload(first), new_payload, APPROVER, "Superseding with detail.", "2026-09-02T00:00:00Z"
            )

    def test_non_approved_records_rejected(self):
        first, second = self._approvals()
        rejected_payload = self._record_payload(first)
        rejected_payload["state"] = "REJECTED"
        with self.assertRaises(ValueError):
            supersede_baseline(
                self.artifact_root, rejected_payload, self._record_payload(second), APPROVER, "Superseding with detail.", "2026-09-02T00:00:00Z"
            )
        with self.assertRaises(ValueError):
            supersede_baseline(
                self.artifact_root, self._record_payload(first), rejected_payload, APPROVER, "Superseding with detail.", "2026-09-02T00:00:00Z"
            )

    def test_missing_human_fields_rejected(self):
        first, second = self._approvals()
        with self.assertRaises(ValueError):
            supersede_baseline(
                self.artifact_root, self._record_payload(first), self._record_payload(second), "", "Superseding with detail.", "2026-09-02T00:00:00Z"
            )
        with self.assertRaises(ValueError):
            supersede_baseline(
                self.artifact_root, self._record_payload(first), self._record_payload(second), APPROVER, "short", "2026-09-02T00:00:00Z"
            )
        with self.assertRaises(ValueError):
            supersede_baseline(
                self.artifact_root, self._record_payload(first), self._record_payload(second), APPROVER, "Superseding with detail.", "not-a-time"
            )

    def test_secret_markers_rejected(self):
        first, second = self._approvals()
        with self.assertRaises(ValueError):
            supersede_baseline(
                self.artifact_root, self._record_payload(first), self._record_payload(second), APPROVER, "Superseding with token detail.", "2026-09-02T00:00:00Z"
            )

    def test_supersession_hash_deterministic(self):
        first, second = self._approvals()
        args = (
            self._record_payload(first),
            self._record_payload(second),
            APPROVER,
            "Superseding with the September cycle.",
            "2026-09-02T00:00:00Z",
        )
        one = supersede_baseline(self.artifact_root, *args)
        two = supersede_baseline(self.artifact_root, *args)
        self.assertEqual(one, two)


class ImmutabilityTests(ApprovalTestBase):
    def test_cycle_mutation_does_not_alter_record(self):
        cycle = cycle_summary()
        record = create_approval_record(cycle, APPROVER, RATIONALE, UTC)
        cycle["capacity"]["verdict"] = "FAIL"
        cycle["levels"]["500"]["verdict"] = "BLOCKED"
        cycle["warnings"].append("late")
        self.assertIs(record.capacity_verdict, CapacityVerdict.PASS)
        self.assertIs(record.per_level_verdicts[500], MetricVerdict.PASS)
        self.assertEqual(record.warnings, ())

    def test_record_frozen(self):
        record = create_approval_record(cycle_summary(), APPROVER, RATIONALE, UTC)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.approver = "x"
        with self.assertRaises(TypeError):
            record.per_level_verdicts[500] = MetricVerdict.FAIL

    def test_warnings_sorted_deduped(self):
        cycle = warning_cycle()
        cycle["warnings"] = ["b warning", "a warning", "a warning"]
        record = create_approval_record(
            cycle, APPROVER, RATIONALE + " warning acknowledged", UTC
        )
        self.assertEqual(record.warnings, ("a warning", "b warning"))


class SchemaContractTests(ApprovalTestBase):
    def _walk(self, value, node, path="$"):
        if isinstance(value, dict) and isinstance(node, dict) and "properties" in node:
            required = set(node.get("required", []))
            missing = sorted(required - set(value))
            self.assertEqual(missing, [], f"{path}: missing {missing}")
            if node.get("additionalProperties") is False:
                extra = sorted(set(value) - set(node["properties"]))
                self.assertEqual(extra, [], f"{path}: unexpected {extra}")
            for key, item in value.items():
                if key in node["properties"]:
                    self._walk(item, node["properties"][key], f"{path}.{key}")
        elif isinstance(value, list) and isinstance(node, dict) and "items" in node:
            for index, item in enumerate(value):
                self._walk(item, node["items"], f"{path}[{index}]")

    def test_emitted_records_match_schema(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        approved = approve_baseline(
            self.artifact_root, cycle_summary(), APPROVER, RATIONALE, UTC
        )
        payload = json.loads(approved.approval_path.read_text(encoding="utf-8"))
        self._walk(payload, schema)


if __name__ == "__main__":
    unittest.main()
