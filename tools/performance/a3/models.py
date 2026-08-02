"""Typed, immutable models shared by all A3 modules."""

from dataclasses import dataclass
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Mapping, Tuple


class LoadLevel(IntEnum):
    """Approved synthetic concurrency levels for A3."""

    USERS_500 = 500
    USERS_1000 = 1000
    USERS_2500 = 2500
    USERS_5000 = 5000


class RunPhase(str, Enum):
    """Lifecycle phases of one A3 run, including the abort path."""

    ENVIRONMENT_CHECK = "ENVIRONMENT_CHECK"
    SERVICE_START = "SERVICE_START"
    PRECONDITIONING = "PRECONDITIONING"
    RAMP_UP = "RAMP_UP"
    STEADY_STATE = "STEADY_STATE"
    COOL_DOWN = "COOL_DOWN"
    VALIDATION = "VALIDATION"
    REPORTING = "REPORTING"
    ABORTED = "ABORTED"
    ARTIFACT_CAPTURE = "ARTIFACT_CAPTURE"
    ROOT_CAUSE_ANALYSIS = "ROOT_CAUSE_ANALYSIS"


class RunStatus(str, Enum):
    """Outcome status of a single run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    VALID = "VALID"
    INVALID = "INVALID"
    ABORTED = "ABORTED"


class MetricVerdict(str, Enum):
    """Verdict for one metric or one load level after SLO evaluation."""

    PASS = "PASS"
    PASS_WITH_WARNING = "PASS_WITH_WARNING"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class CapacityVerdict(str, Enum):
    """Capacity classification of one load level for the final report."""

    PASS = "PASS"
    PASS_WITH_WARNING = "PASS_WITH_WARNING"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"


@dataclass(frozen=True)
class A3Config:
    """Validated, immutable A3 cycle configuration.

    Construct via :func:`tools.performance.a3.config.load_config`, which
    enforces every approved constant and rejects malformed input.
    """

    load_levels: Tuple[int, ...]
    valid_runs_per_level: int
    webgl_clients: int
    preconditioning_seconds: int
    ramp_seconds: int
    steady_state_seconds: int
    cooldown_seconds: int
    scrape_interval_seconds: int
    workload_mix_tolerance_percentage_points: int
    prometheus_missing_data_limit_seconds: int
    target_concurrency_floor_ratio: float
    workload_mix: Mapping[str, float]

    def __post_init__(self) -> None:
        # Freeze the workload mapping so the dataclass stays immutable.
        object.__setattr__(
            self, "workload_mix", MappingProxyType(dict(self.workload_mix))
        )
