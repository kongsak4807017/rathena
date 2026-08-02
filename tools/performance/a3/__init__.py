"""A3 baseline and SLO tooling for rAthena performance baselines.

This package implements the approved A3 design in
``docs/superpowers/specs/2026-08-02-a3-baseline-slo-design.md``.
Task 1 establishes the package, typed models, strict configuration
loading, atomic JSON helpers, and committed JSON schemas.
"""

from tools.performance.a3.config import load_config
from tools.performance.a3.io import read_json, sha256_file, write_json_atomic
from tools.performance.a3.models import (
    A3Config,
    CapacityVerdict,
    LoadLevel,
    MetricVerdict,
    RunPhase,
    RunStatus,
)

__all__ = [
    "A3Config",
    "CapacityVerdict",
    "LoadLevel",
    "MetricVerdict",
    "RunPhase",
    "RunStatus",
    "load_config",
    "read_json",
    "sha256_file",
    "write_json_atomic",
]
