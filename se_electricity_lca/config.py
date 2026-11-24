"""Config loading and validation for electricity_mix.yaml."""

from __future__ import annotations

import pathlib
import warnings
from importlib.resources import files
from typing import Any

import yaml

# electricity_mix.yaml is bundled as package data and resolved against the
# installed package, so it works in both editable and wheel/git installs.
DEFAULT_CONFIG = pathlib.Path(files("se_electricity_lca") / "config" / "electricity_mix.yaml")

SHARE_SUM_TOLERANCE = 0.02


def load_config(
    path: str | pathlib.Path | None = None,
    shares_csv: str | pathlib.Path | None = None,
) -> dict[str, Any]:
    """Load electricity_mix.yaml and merge in shares from CSV.

    Parameters
    ----------
    path:
        Path to config YAML. Defaults to config/electricity_mix.yaml.
    shares_csv:
        Path to a pre-built shares CSV. If omitted, the shares table is built
        on first use by downloading the Energimyndigheten scenario Excel and
        caching the result (see se_electricity_lca.shares).

    Returns
    -------
    dict with keys: ``settings``, ``technology_activities``, ``scenarios``
        Each scenario will have a ``years`` sub-dict populated from the CSV.
    """
    config_path = pathlib.Path(path) if path else DEFAULT_CONFIG
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    if shares_csv:
        csv_path = pathlib.Path(shares_csv)
        if not csv_path.exists():
            raise FileNotFoundError(f"Shares CSV not found: {csv_path}")
    else:
        from .shares import ensure_shares_csv
        csv_path = ensure_shares_csv(cfg)
    _merge_shares_csv(cfg, csv_path)

    _validate(cfg)
    return cfg


def _merge_shares_csv(cfg: dict, csv_path: pathlib.Path) -> None:
    """Read shares CSV and populate cfg['scenarios'][s]['years'] in place."""
    import pandas as pd

    df = pd.read_csv(csv_path)
    for (scenario, year), grp in df.groupby(["scenario", "year"]):
        if scenario not in cfg["scenarios"]:
            warnings.warn(
                f"CSV contains scenario '{scenario}' not found in config — skipping."
            )
            continue
        cfg["scenarios"][scenario].setdefault("years", {})[int(year)] = dict(
            zip(grp["technology"], grp["share"])
        )


def get_scenario_pairs(cfg: dict) -> list[tuple[str, str]]:
    """Return list of (se_scenario_key, premise_scenario) from config.

    Reads premise_scenario from each scenario definition. Scenarios without
    a premise_scenario field are skipped with a warning.
    """
    pairs = []
    for name, sc in cfg["scenarios"].items():
        if "premise_scenario" not in sc:
            warnings.warn(
                f"Scenario '{name}' has no premise_scenario — skipping. "
                "Add premise_scenario to electricity_mix.yaml to include it."
            )
            continue
        pairs.append((name, sc["premise_scenario"]))
    return pairs


def get_premise_db_pattern(cfg: dict, override: str | None = None) -> str:
    """Return the premise_db_pattern from settings, or override if provided."""
    if override:
        return override
    pattern = cfg.get("settings", {}).get("premise_db_pattern")
    if not pattern:
        raise ValueError(
            "premise_db_pattern not set in config settings and no override provided."
        )
    return pattern


def _validate(cfg: dict) -> None:
    """Warn on missing keys and shares that don't sum to ~1.0."""
    required_top = {"technology_activities", "scenarios"}
    missing = required_top - set(cfg.keys())
    if missing:
        raise ValueError(f"Config missing top-level keys: {missing}")

    tech_keys = set(cfg["technology_activities"].keys())

    for scenario_name, scenario in cfg["scenarios"].items():
        if "years" not in scenario:
            continue  # year data loaded from CSV after this call
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



def get_voltage_level(cfg: dict) -> str:
    """Return the configured output voltage level ('high', 'medium', or 'low')."""
    level = cfg.get("settings", {}).get("voltage_level", "low")
    if level not in ("high", "medium", "low"):
        raise ValueError(f"Invalid voltage_level '{level}' in config. Must be 'high', 'medium', or 'low'.")
    return level


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
