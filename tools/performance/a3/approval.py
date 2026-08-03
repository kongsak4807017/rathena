"""A3 approval governance and approved-baseline lifecycle.

Manual, human-driven approval of baseline cycles. CI must never create an
APPROVED record automatically: every approval, rejection, and supersession
requires an explicit approver, rationale, and caller-supplied RFC 3339 UTC
timestamp. Records are canonically hashed (UTF-8, sorted keys,
separators (",", ":"), SHA-256) and written atomically with a sidecar
checksum file. No timestamps, hostname, PID, or randomness are generated.
"""

import dataclasses
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from tools.performance.a3.models import CapacityVerdict, MetricVerdict

LOAD_LEVELS = (500, 1000, 2500, 5000)
APPROVER_MAX_LENGTH = 200
RATIONALE_MIN_LENGTH = 10
RATIONALE_MAX_LENGTH = 4000

SECRET_MARKERS = (
    "password",
    "token",
    "secret",
    "api_key",
    "private_key",
    "authorization",
    "bearer",
)

_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_APPROVABLE_VERDICTS = (CapacityVerdict.PASS, CapacityVerdict.PASS_WITH_WARNING)
_LEVEL_VERDICTS = (
    MetricVerdict.PASS,
    MetricVerdict.PASS_WITH_WARNING,
    MetricVerdict.FAIL,
    MetricVerdict.BLOCKED,
)


class ApprovalError(Exception):
    """Raised for governance or filesystem conflicts during approval."""


class ApprovalState(str, Enum):
    DRAFT = "DRAFT"
    CI_EVALUATED = "CI_EVALUATED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


_ALLOWED_TRANSITIONS = {
    ApprovalState.DRAFT: frozenset({ApprovalState.CI_EVALUATED}),
    ApprovalState.CI_EVALUATED: frozenset({ApprovalState.AWAITING_APPROVAL}),
    ApprovalState.AWAITING_APPROVAL: frozenset(
        {ApprovalState.APPROVED, ApprovalState.REJECTED}
    ),
    ApprovalState.APPROVED: frozenset({ApprovalState.SUPERSEDED}),
    ApprovalState.REJECTED: frozenset(),
    ApprovalState.SUPERSEDED: frozenset(),
}


def is_transition_allowed(source: ApprovalState, target: ApprovalState) -> bool:
    return target in _ALLOWED_TRANSITIONS[source]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ApprovalRecord:
    version: int
    state: ApprovalState
    baseline_cycle_id: str
    manifest_id: str
    git_sha: str
    approved_utc: str
    approver: str
    rationale: str
    capacity_verdict: CapacityVerdict
    safe_capacity: Optional[int]
    conditional_capacity: Optional[int]
    tested_ceiling: Optional[int]
    first_degradation_level: Optional[int]
    per_level_verdicts: Mapping[int, MetricVerdict]
    warnings: Tuple[str, ...]
    report_checksums_sha256: str
    approval_record_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "per_level_verdicts", MappingProxyType(dict(self.per_level_verdicts))
        )
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclasses.dataclass(frozen=True)
class SupersessionRecord:
    version: int
    state: ApprovalState
    previous_baseline_cycle_id: str
    previous_manifest_id: str
    previous_approval_sha256: str
    new_baseline_cycle_id: str
    new_manifest_id: str
    new_approval_sha256: str
    superseded_utc: str
    approver: str
    rationale: str
    supersession_sha256: str


@dataclasses.dataclass(frozen=True)
class ApprovalResult:
    approval_path: Path
    checksum_path: Path
    record: ApprovalRecord
    written: bool


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > 128 or "\x00" in value or ".." in value:
        raise ValueError(f"{field} is not a safe identifier")
    if not all(c.isascii() and (c.isalnum() or c in ".-_") for c in value):
        raise ValueError(f"{field} is not a safe identifier")
    return value


def _check_secret_markers(text: str, field: str) -> None:
    lowered = text.lower()
    for marker in SECRET_MARKERS:
        if marker in lowered:
            raise ValueError(f"{field} contains a forbidden secret marker")


def _validate_approver(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("approver must be a string")
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("approver must be non-empty")
    if len(trimmed) > APPROVER_MAX_LENGTH:
        raise ValueError(f"approver exceeds {APPROVER_MAX_LENGTH} characters")
    for char in trimmed:
        code = ord(char)
        if code == 0 or code < 32 or code == 127:
            raise ValueError("approver contains a control character")
        if char in "/\\":
            raise ValueError("approver contains a path separator")
    _check_secret_markers(trimmed, "approver")
    return trimmed


def _validate_rationale(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("rationale must be a string")
    trimmed = value.strip()
    if len(trimmed) < RATIONALE_MIN_LENGTH:
        raise ValueError(
            f"rationale must be at least {RATIONALE_MIN_LENGTH} characters"
        )
    if len(trimmed) > RATIONALE_MAX_LENGTH:
        raise ValueError(f"rationale exceeds {RATIONALE_MAX_LENGTH} characters")
    for char in trimmed:
        code = ord(char)
        if code == 0 or (code < 32 and char not in "\n\t") or code == 127:
            raise ValueError("rationale contains a forbidden control character")
    _check_secret_markers(trimmed, "rationale")
    return trimmed


def _validate_utc(value: Any, field: str = "utc") -> str:
    if not isinstance(value, str) or not _UTC_RE.match(value):
        raise ValueError(
            f"{field} must be RFC 3339 UTC (YYYY-MM-DDTHH:MM:SSZ), got {value!r}"
        )
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ValueError(f"{field} is not a valid calendar date/time: {value!r}") from None
    return value


def _reject_symlink(path: Path, description: str) -> None:
    if path.is_symlink():
        raise ApprovalError(f"{description} must not be a symlink: {path}")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Canonical hashing and serialization
# ---------------------------------------------------------------------------


def _canonical_sha256(payload: Mapping) -> str:
    text = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record_to_dict(record: ApprovalRecord) -> Dict[str, Any]:
    return {
        "version": record.version,
        "state": record.state.value,
        "baseline_cycle_id": record.baseline_cycle_id,
        "manifest_id": record.manifest_id,
        "git_sha": record.git_sha,
        "approved_utc": record.approved_utc,
        "approver": record.approver,
        "rationale": record.rationale,
        "capacity_verdict": record.capacity_verdict.value,
        "safe_capacity": record.safe_capacity,
        "conditional_capacity": record.conditional_capacity,
        "tested_ceiling": record.tested_ceiling,
        "first_degradation_level": record.first_degradation_level,
        "per_level_verdicts": {
            str(level): record.per_level_verdicts[level].value
            for level in sorted(record.per_level_verdicts)
        },
        "warnings": list(record.warnings),
        "report_checksums_sha256": record.report_checksums_sha256,
        "approval_record_sha256": record.approval_record_sha256,
    }


def _supersession_to_dict(record: SupersessionRecord) -> Dict[str, Any]:
    return {
        "version": record.version,
        "state": record.state.value,
        "previous_baseline_cycle_id": record.previous_baseline_cycle_id,
        "previous_manifest_id": record.previous_manifest_id,
        "previous_approval_sha256": record.previous_approval_sha256,
        "new_baseline_cycle_id": record.new_baseline_cycle_id,
        "new_manifest_id": record.new_manifest_id,
        "new_approval_sha256": record.new_approval_sha256,
        "superseded_utc": record.superseded_utc,
        "approver": record.approver,
        "rationale": record.rationale,
        "supersession_sha256": record.supersession_sha256,
    }


def _write_record_with_sidecar(
    approval_dir: Path,
    filename: str,
    payload: Dict[str, Any],
    record_sha256: str,
    overwrite: bool,
) -> Tuple[Path, Path]:
    destination = approval_dir / filename
    sidecar = approval_dir / (filename.replace(".json", ".sha256"))
    for path, description in (
        (approval_dir, "approval directory"),
        (destination, "approval record destination"),
        (sidecar, "approval record sidecar"),
    ):
        _reject_symlink(path, description)
    if destination.exists():
        existing_state = None
        try:
            existing_state = json.loads(
                destination.read_text(encoding="utf-8")
            ).get("state")
        except (OSError, ValueError):
            existing_state = None
        if existing_state in (
            ApprovalState.APPROVED.value,
            ApprovalState.SUPERSEDED.value,
        ):
            raise ApprovalError(
                f"refusing to replace a finalized record ({existing_state}): {destination}"
            )
        if not overwrite:
            raise ApprovalError(f"approval record already exists: {destination}")
    if sidecar.exists():
        # A sidecar proves a finalized (or orphaned) checksum exists; never
        # overwrite it, regardless of the retry flag.
        raise ApprovalError(
            f"refusing to overwrite an existing sidecar: {sidecar}"
        )
    approval_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
    )
    _atomic_write_bytes(destination, (text + "\n").encode("utf-8"))
    _atomic_write_bytes(
        sidecar, f"{record_sha256}  {filename}\n".encode("utf-8")
    )
    return destination, sidecar


# ---------------------------------------------------------------------------
# Cycle-summary validation and record construction
# ---------------------------------------------------------------------------


def _capacity_value(capacity: Mapping, field: str, required: bool) -> Optional[int]:
    value = capacity.get(field)
    if value is None:
        if required:
            raise ValueError(f"capacity.{field} must not be null")
        return None
    if value not in LOAD_LEVELS or isinstance(value, bool):
        raise ValueError(f"capacity.{field} must be an approved load level or null")
    return value


def _build_record(
    cycle_summary: Mapping,
    approver: str,
    rationale: str,
    utc: str,
    target_state: ApprovalState,
) -> ApprovalRecord:
    if not isinstance(cycle_summary, Mapping):
        raise ValueError("cycle_summary must be a mapping")
    state = cycle_summary.get("state")
    if state != ApprovalState.AWAITING_APPROVAL.value:
        raise ValueError(
            f"cycle state must be AWAITING_APPROVAL, got {state!r}"
        )

    baseline_cycle_id = _validate_identifier(
        cycle_summary.get("baseline_cycle_id"), "baseline_cycle_id"
    )
    manifest_id = _validate_identifier(cycle_summary.get("manifest_id"), "manifest_id")
    git_sha = cycle_summary.get("git_sha")
    if not isinstance(git_sha, str) or not _GIT_SHA_RE.match(git_sha):
        raise ValueError("git_sha must be 40 lowercase hexadecimal characters")
    report_sha = cycle_summary.get("report_checksums_sha256")
    if not isinstance(report_sha, str) or not _SHA256_RE.match(report_sha):
        raise ValueError(
            "report_checksums_sha256 must be 64 lowercase hexadecimal characters"
        )

    approver = _validate_approver(approver)
    rationale = _validate_rationale(rationale)
    utc = _validate_utc(utc)

    warnings_raw = cycle_summary.get("warnings", [])
    if not isinstance(warnings_raw, (list, tuple)):
        raise ValueError("warnings must be a list")
    warnings: List[str] = []
    for entry in warnings_raw:
        if not isinstance(entry, str):
            raise ValueError("warnings entries must be strings")
        _check_secret_markers(entry, "warnings")
        warnings.append(entry)
    warnings = sorted(set(warnings))

    capacity = cycle_summary.get("capacity")
    if not isinstance(capacity, Mapping):
        raise ValueError("capacity must be a mapping")
    verdict_raw = capacity.get("verdict")
    try:
        capacity_verdict = CapacityVerdict(verdict_raw)
    except ValueError:
        raise ValueError(f"capacity.verdict is invalid: {verdict_raw!r}") from None

    safe_capacity = _capacity_value(capacity, "safe_capacity", required=False)
    conditional_capacity = _capacity_value(
        capacity, "conditional_capacity", required=False
    )
    tested_ceiling = _capacity_value(capacity, "tested_ceiling", required=True)
    first_degradation = _capacity_value(
        capacity, "first_degradation_level", required=False
    )

    levels = cycle_summary.get("levels")
    if not isinstance(levels, Mapping):
        raise ValueError("levels must be a mapping")
    per_level: Dict[int, MetricVerdict] = {}
    for key, entry in levels.items():
        try:
            level = int(key)
        except (TypeError, ValueError):
            raise ValueError(f"levels key is not a load level: {key!r}") from None
        if level not in LOAD_LEVELS:
            raise ValueError(f"levels contains unexpected load level: {level}")
        if not isinstance(entry, Mapping):
            raise ValueError(f"levels.{level} must be a mapping")
        try:
            verdict = MetricVerdict(entry.get("verdict"))
        except ValueError:
            raise ValueError(
                f"levels.{level}.verdict is invalid: {entry.get('verdict')!r}"
            ) from None
        if verdict not in _LEVEL_VERDICTS:
            raise ValueError(f"levels.{level}.verdict not permitted: {verdict}")
        entry_manifest = entry.get("manifest_id")
        if entry_manifest is not None and entry_manifest != manifest_id:
            raise ValueError(f"levels.{level} manifest_id mismatch")
        per_level[level] = verdict

    catastrophic = cycle_summary.get("catastrophic") or any(
        isinstance(entry, Mapping) and entry.get("catastrophic")
        for entry in levels.values()
    )

    if target_state is ApprovalState.APPROVED:
        _validate_approval_eligibility(
            cycle_summary,
            capacity_verdict,
            safe_capacity,
            conditional_capacity,
            tested_ceiling,
            per_level,
            warnings,
            rationale,
            catastrophic,
        )

    payload = {
        "version": 1,
        "state": target_state.value,
        "baseline_cycle_id": baseline_cycle_id,
        "manifest_id": manifest_id,
        "git_sha": git_sha,
        "approved_utc": utc,
        "approver": approver,
        "rationale": rationale,
        "capacity_verdict": capacity_verdict.value,
        "safe_capacity": safe_capacity,
        "conditional_capacity": conditional_capacity,
        "tested_ceiling": tested_ceiling,
        "first_degradation_level": first_degradation,
        "per_level_verdicts": {
            str(level): per_level[level].value for level in sorted(per_level)
        },
        "warnings": warnings,
        "report_checksums_sha256": report_sha,
    }
    record_sha = _canonical_sha256(payload)
    return ApprovalRecord(
        version=1,
        state=target_state,
        baseline_cycle_id=baseline_cycle_id,
        manifest_id=manifest_id,
        git_sha=git_sha,
        approved_utc=utc,
        approver=approver,
        rationale=rationale,
        capacity_verdict=capacity_verdict,
        safe_capacity=safe_capacity,
        conditional_capacity=conditional_capacity,
        tested_ceiling=tested_ceiling,
        first_degradation_level=first_degradation,
        per_level_verdicts=per_level,
        warnings=tuple(warnings),
        report_checksums_sha256=report_sha,
        approval_record_sha256=record_sha,
    )


def _validate_approval_eligibility(
    cycle_summary: Mapping,
    capacity_verdict: CapacityVerdict,
    safe_capacity: Optional[int],
    conditional_capacity: Optional[int],
    tested_ceiling: Optional[int],
    per_level: Dict[int, MetricVerdict],
    warnings: List[str],
    rationale: str,
    catastrophic: bool,
) -> None:
    ci = cycle_summary.get("ci")
    if not isinstance(ci, Mapping) or ci.get("evaluated") is not True:
        raise ValueError("ci.evaluated must be exactly true")
    if ci.get("status") != "success":
        raise ValueError("ci.status must be success")
    if capacity_verdict not in _APPROVABLE_VERDICTS:
        raise ValueError(
            f"capacity verdict {capacity_verdict.value} is not approvable"
        )
    if safe_capacity is None:
        raise ValueError("safe_capacity must not be null for approval")
    if tested_ceiling is not None and safe_capacity > tested_ceiling:
        raise ValueError("safe_capacity must not exceed tested_ceiling")
    if conditional_capacity is not None and conditional_capacity < safe_capacity:
        raise ValueError("conditional_capacity must not be below safe_capacity")
    if 500 not in per_level:
        raise ValueError("levels must include the 500-user level")
    for level, verdict in per_level.items():
        if verdict is MetricVerdict.BLOCKED:
            raise ValueError(f"levels.{level} verdict BLOCKED is not approvable")
    if catastrophic:
        raise ValueError("catastrophic marker present; cycle is not approvable")
    if capacity_verdict is CapacityVerdict.PASS_WITH_WARNING:
        if not warnings:
            raise ValueError(
                "PASS_WITH_WARNING approval requires recorded warnings"
            )
        if "warning" not in rationale.lower():
            raise ValueError(
                "PASS_WITH_WARNING approval requires rationale acknowledging warnings"
            )


def create_approval_record(
    cycle_summary: Mapping,
    approver: str,
    rationale: str,
    approved_utc: str,
) -> ApprovalRecord:
    """Build a validated, hashed APPROVED record (no filesystem writes)."""
    return _build_record(
        cycle_summary, approver, rationale, approved_utc, ApprovalState.APPROVED
    )


# ---------------------------------------------------------------------------
# Filesystem operations
# ---------------------------------------------------------------------------


def _approval_dir(artifact_root: Path, baseline_cycle_id: str) -> Path:
    return (
        Path(artifact_root)
        / "artifacts"
        / "performance"
        / "a3"
        / baseline_cycle_id
        / "approval"
    )


def approve_baseline(
    artifact_root: Path,
    cycle_summary: Mapping,
    approver: str,
    rationale: str,
    approved_utc: str,
    overwrite: bool = False,
) -> ApprovalResult:
    """Write approved-baseline.json plus its sidecar checksum, atomically."""
    artifact_root = Path(artifact_root)
    _reject_symlink(artifact_root, "artifact root")
    record = create_approval_record(cycle_summary, approver, rationale, approved_utc)
    approval_dir = _approval_dir(artifact_root, record.baseline_cycle_id)
    _reject_symlink(
        approval_dir.parent, "cycle directory"
    )
    destination, sidecar = _write_record_with_sidecar(
        approval_dir,
        "approved-baseline.json",
        _record_to_dict(record),
        record.approval_record_sha256,
        overwrite,
    )
    return ApprovalResult(
        approval_path=destination,
        checksum_path=sidecar,
        record=record,
        written=True,
    )


def reject_baseline(
    artifact_root: Path,
    cycle_summary: Mapping,
    approver: str,
    rationale: str,
    rejected_utc: str,
    overwrite: bool = False,
) -> ApprovalResult:
    """Write rejected-baseline.json plus its sidecar checksum, atomically."""
    artifact_root = Path(artifact_root)
    _reject_symlink(artifact_root, "artifact root")
    record = _build_record(
        cycle_summary, approver, rationale, rejected_utc, ApprovalState.REJECTED
    )
    approval_dir = _approval_dir(artifact_root, record.baseline_cycle_id)
    _reject_symlink(approval_dir.parent, "cycle directory")
    approved_file = approval_dir / "approved-baseline.json"
    if approved_file.exists():
        raise ApprovalError(
            f"rejection must not overwrite an approved record: {approved_file}"
        )
    destination, sidecar = _write_record_with_sidecar(
        approval_dir,
        "rejected-baseline.json",
        _record_to_dict(record),
        record.approval_record_sha256,
        overwrite,
    )
    return ApprovalResult(
        approval_path=destination,
        checksum_path=sidecar,
        record=record,
        written=True,
    )


def supersede_baseline(
    artifact_root: Path,
    previous_approval: Mapping,
    new_approval: Mapping,
    approver: str,
    rationale: str,
    superseded_utc: str,
) -> SupersessionRecord:
    """Link two approved records in a supersession record.

    The previous approval file is never modified or deleted.
    """
    artifact_root = Path(artifact_root)
    _reject_symlink(artifact_root, "artifact root")
    for label, record in (("previous", previous_approval), ("new", new_approval)):
        if not isinstance(record, Mapping):
            raise ValueError(f"{label} approval must be a mapping")
        if record.get("state") != ApprovalState.APPROVED.value:
            raise ValueError(f"{label} approval state must be APPROVED")
        recorded = record.get("approval_record_sha256")
        preimage = {key: value for key, value in record.items() if key != "approval_record_sha256"}
        if not isinstance(recorded, str) or _canonical_sha256(preimage) != recorded:
            raise ValueError(f"{label} approval checksum is invalid")

    previous_cycle = previous_approval["baseline_cycle_id"]
    new_cycle = new_approval["baseline_cycle_id"]
    if previous_cycle == new_cycle:
        raise ValueError("previous and new baseline_cycle_id must differ")
    if previous_approval["manifest_id"] == new_approval["manifest_id"]:
        raise ValueError("previous and new manifest_id must differ")

    approver = _validate_approver(approver)
    rationale = _validate_rationale(rationale)
    superseded_utc = _validate_utc(superseded_utc, "superseded_utc")

    payload = {
        "version": 1,
        "state": ApprovalState.SUPERSEDED.value,
        "previous_baseline_cycle_id": previous_cycle,
        "previous_manifest_id": previous_approval["manifest_id"],
        "previous_approval_sha256": previous_approval["approval_record_sha256"],
        "new_baseline_cycle_id": new_cycle,
        "new_manifest_id": new_approval["manifest_id"],
        "new_approval_sha256": new_approval["approval_record_sha256"],
        "superseded_utc": superseded_utc,
        "approver": approver,
        "rationale": rationale,
    }
    supersession_sha = _canonical_sha256(payload)
    record = SupersessionRecord(
        version=1,
        state=ApprovalState.SUPERSEDED,
        previous_baseline_cycle_id=previous_cycle,
        previous_manifest_id=previous_approval["manifest_id"],
        previous_approval_sha256=previous_approval["approval_record_sha256"],
        new_baseline_cycle_id=new_cycle,
        new_manifest_id=new_approval["manifest_id"],
        new_approval_sha256=new_approval["approval_record_sha256"],
        superseded_utc=superseded_utc,
        approver=approver,
        rationale=rationale,
        supersession_sha256=supersession_sha,
    )
    approval_dir = _approval_dir(artifact_root, new_cycle)
    _reject_symlink(approval_dir.parent, "cycle directory")
    # Supersession records are append-only final records.
    _write_record_with_sidecar(
        approval_dir,
        "supersession.json",
        _supersession_to_dict(record),
        supersession_sha,
        overwrite=False,
    )
    return record
