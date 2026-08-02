"""Strict A3 configuration loading and validation.

Every constant enforced here comes from the approved A3 design
(``docs/superpowers/specs/2026-08-02-a3-baseline-slo-design.md``).
Validation failures raise :class:`ValueError` naming the invalid field.
"""

from pathlib import Path
from typing import Any, Dict, Tuple

from tools.performance.a3.io import read_json
from tools.performance.a3.models import A3Config, LoadLevel

EXPECTED_LOAD_LEVELS: Tuple[int, ...] = tuple(level.value for level in LoadLevel)
EXPECTED_VALID_RUNS_PER_LEVEL = 3
EXPECTED_WEBGL_CLIENTS = 20
EXPECTED_SCRAPE_INTERVAL_SECONDS = 5
WORKLOAD_SUM_TOLERANCE = 1e-9

EXPECTED_WORKLOAD_CATEGORIES = frozenset(
    {
        "movement_direction_changes",
        "idle_heartbeat",
        "combat",
        "npc_interaction",
        "item_inventory",
        "map_change_warp",
        "chat",
        "login_logout_character_select",
    }
)

_REQUIRED_KEYS = frozenset(
    {
        "load_levels",
        "valid_runs_per_level",
        "webgl_clients",
        "preconditioning_seconds",
        "ramp_seconds",
        "steady_state_seconds",
        "cooldown_seconds",
        "scrape_interval_seconds",
        "workload_mix_tolerance_percentage_points",
        "prometheus_missing_data_limit_seconds",
        "target_concurrency_floor_ratio",
        "workload_mix",
    }
)


def load_config(path: Path) -> A3Config:
    """Load and strictly validate an A3 configuration file.

    Raises:
        ValueError: naming the invalid field when the file is missing
            required keys, carries unknown keys, or violates an approved
            A3 constant.
    """
    raw = read_json(Path(path))
    if not isinstance(raw, dict):
        raise ValueError("config root must be a JSON object")

    missing = sorted(_REQUIRED_KEYS - raw.keys())
    if missing:
        raise ValueError(f"missing required configuration key(s): {', '.join(missing)}")
    unknown = sorted(raw.keys() - _REQUIRED_KEYS)
    if unknown:
        raise ValueError(f"unknown configuration key(s): {', '.join(unknown)}")

    load_levels = tuple(raw["load_levels"])
    if load_levels != EXPECTED_LOAD_LEVELS:
        raise ValueError(
            f"load_levels must be exactly {list(EXPECTED_LOAD_LEVELS)}, "
            f"got {list(load_levels)}"
        )

    if raw["valid_runs_per_level"] != EXPECTED_VALID_RUNS_PER_LEVEL:
        raise ValueError(
            f"valid_runs_per_level must be exactly {EXPECTED_VALID_RUNS_PER_LEVEL}, "
            f"got {raw['valid_runs_per_level']}"
        )

    if raw["webgl_clients"] != EXPECTED_WEBGL_CLIENTS:
        raise ValueError(
            f"webgl_clients must be exactly {EXPECTED_WEBGL_CLIENTS}, "
            f"got {raw['webgl_clients']}"
        )

    if raw["scrape_interval_seconds"] != EXPECTED_SCRAPE_INTERVAL_SECONDS:
        raise ValueError(
            f"scrape_interval_seconds must be exactly "
            f"{EXPECTED_SCRAPE_INTERVAL_SECONDS}, "
            f"got {raw['scrape_interval_seconds']}"
        )

    workload_mix = _validate_workload_mix(raw["workload_mix"])

    return A3Config(
        load_levels=load_levels,
        valid_runs_per_level=raw["valid_runs_per_level"],
        webgl_clients=raw["webgl_clients"],
        preconditioning_seconds=raw["preconditioning_seconds"],
        ramp_seconds=raw["ramp_seconds"],
        steady_state_seconds=raw["steady_state_seconds"],
        cooldown_seconds=raw["cooldown_seconds"],
        scrape_interval_seconds=raw["scrape_interval_seconds"],
        workload_mix_tolerance_percentage_points=raw[
            "workload_mix_tolerance_percentage_points"
        ],
        prometheus_missing_data_limit_seconds=raw[
            "prometheus_missing_data_limit_seconds"
        ],
        target_concurrency_floor_ratio=raw["target_concurrency_floor_ratio"],
        workload_mix=workload_mix,
    )


def _validate_workload_mix(workload_mix: Any) -> Dict[str, float]:
    if not isinstance(workload_mix, dict):
        raise ValueError("workload_mix must be a JSON object")

    categories = set(workload_mix)
    missing = sorted(EXPECTED_WORKLOAD_CATEGORIES - categories)
    if missing:
        raise ValueError(
            f"workload_mix is missing categor{'y' if len(missing) == 1 else 'ies'}: "
            f"{', '.join(missing)}"
        )
    unknown = sorted(categories - EXPECTED_WORKLOAD_CATEGORIES)
    if unknown:
        raise ValueError(
            f"workload_mix has unknown categor{'y' if len(unknown) == 1 else 'ies'}: "
            f"{', '.join(unknown)}"
        )

    normalized: Dict[str, float] = {}
    for name, proportion in workload_mix.items():
        if not isinstance(proportion, (int, float)) or isinstance(proportion, bool):
            raise ValueError(
                f"workload_mix.{name} must be a number, got {proportion!r}"
            )
        value = float(proportion)
        if not 0.0 <= value <= 1.0:
            raise ValueError(
                f"workload_mix.{name} must be within [0.0, 1.0], got {value}"
            )
        normalized[name] = value

    total = sum(normalized.values())
    if abs(total - 1.0) > WORKLOAD_SUM_TOLERANCE:
        raise ValueError(
            f"workload_mix proportions must sum to 1.0 within "
            f"{WORKLOAD_SUM_TOLERANCE}, got {total!r}"
        )

    return normalized
