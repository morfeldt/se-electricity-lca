"""Config loading and validation for electricity_mix.yaml."""

from __future__ import annotations

import pathlib
import warnings
from typing import Any

import yaml

DEFAULT_CONFIG = pathlib.Path(__file__).parent.parent / "config" / "electricity_mix.yaml"

# Tolerance for share sums — warn if any year deviates by more than this.
SHARE_SUM_TOLERANCE = 0.02


def load_config(path: str | pathlib.Path | None = None) -> dict[str, Any]:
    """Load and validate electricity_mix.yaml.

    Parameters
    ----------
    path:
        Path to config file. Defaults to config/electricity_mix.yaml in the repo root.

    Returns
    -------
    dict with keys: ``technology_activities``, ``scenarios``
    """
    config_path = pathlib.Path(path) if path else DEFAULT_CONFIG
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    """Warn on missing keys and shares that don't sum to ~1.0."""
    required_top = {"technology_activities", "scenarios"}
    missing = required_top - set(cfg.keys())
    if missing:
        raise ValueError(f"Config missing top-level keys: {missing}")

    tech_keys = set(cfg["technology_activities"].keys())

    for scenario_name, scenario in cfg["scenarios"].items():
        if "years" not in scenario:
            raise ValueError(f"Scenario '{scenario_name}' has no 'years' key.")
        for year, shares in scenario["years"].items():
            unknown = set(shares.keys()) - tech_keys
            if unknown:
                warnings.warn(
                    f"Scenario '{scenario_name}' year {year}: unknown technology keys {unknown}. "
                    "These will be ignored. Check spelling against technology_activities."
                )
            total = sum(shares.get(t, 0.0) for t in tech_keys)
            if abs(total - 1.0) > SHARE_SUM_TOLERANCE:
                warnings.warn(
                    f"Scenario '{scenario_name}' year {year}: shares sum to {total:.4f}, "
                    f"expected 1.0 ± {SHARE_SUM_TOLERANCE}. Review config."
                )


def get_technology_shares(
    cfg: dict,
    scenario: str,
    year: int,
    interpolate: bool = True,
) -> dict[str, float]:
    """Return technology shares for a given scenario and year.

    If the exact year is not in the config and interpolate=True, linearly
    interpolates between the two nearest bracketing years.

    Parameters
    ----------
    cfg:        Output of load_config().
    scenario:   Scenario name (must exist in cfg["scenarios"]).
    year:       Target year.
    interpolate: Whether to interpolate between defined years.
    """
    if scenario not in cfg["scenarios"]:
        available = list(cfg["scenarios"].keys())
        raise KeyError(f"Scenario '{scenario}' not found. Available: {available}")

    years_data = cfg["scenarios"][scenario]["years"]
    defined_years = sorted(years_data.keys())

    if year in years_data:
        return dict(years_data[year])

    if not interpolate:
        raise KeyError(f"Year {year} not defined for scenario '{scenario}' and interpolate=False.")

    # Linear interpolation
    lower = max((y for y in defined_years if y <= year), default=None)
    upper = min((y for y in defined_years if y >= year), default=None)

    if lower is None:
        raise ValueError(f"Year {year} is before the earliest defined year {defined_years[0]}.")
    if upper is None:
        raise ValueError(f"Year {year} is after the latest defined year {defined_years[-1]}.")
    if lower == upper:
        return dict(years_data[lower])

    frac = (year - lower) / (upper - lower)
    shares_lower = years_data[lower]
    shares_upper = years_data[upper]
    tech_keys = set(shares_lower) | set(shares_upper)

    return {
        t: shares_lower.get(t, 0.0) * (1 - frac) + shares_upper.get(t, 0.0) * frac
        for t in tech_keys
    }
