"""Main build logic — full LCI reconstruction for Swedish electricity.

Workflow
--------
For each (se_scenario, premise_scenario, year):
  1. Load the premise-modified ecoinvent database for (premise_scenario, year).
  2. For each technology defined in the config:
       a. Find the matching ecoinvent activity (with SE > RER > GLO fallback).
       b. Run LCA to get GWP100 per kWh for that technology.
  3. Weight technology GWP100 values by shares from config.
  4. Record result: se_scenario, premise_scenario, year, technology, share, gwp100.

Output
------
Parquet file with columns:
  se_scenario, premise_scenario, year, technology, share,
  gwp100_per_kwh_tech, gwp100_per_kwh_contribution
Plus a summary row per (se_scenario, premise_scenario, year) with technology="TOTAL".
"""

from __future__ import annotations

import pathlib
import warnings
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv(pathlib.Path(__file__).parent.parent / ".env")
except ImportError:
    pass

import pandas as pd

from .config import load_config, get_technology_shares, get_scenario_pairs, get_premise_db_pattern

LCIA_METHOD_KEYWORDS = ("climate", "global warming potential (GWP100)", "IPCC 2021")
LCIA_METHOD_EXCLUDES  = ("no LT", ":")


def _find_lcia_method(bw2data_module) -> tuple:
    """Locate the IPCC 2021 GWP100 method in the current brightway project."""
    for m in bw2data_module.methods:
        m_str = str(m)
        if (all(kw in m_str for kw in LCIA_METHOD_KEYWORDS)
                and not any(ex in m_str for ex in LCIA_METHOD_EXCLUDES)):
            return m
    raise RuntimeError(
        "Could not find IPCC 2021 GWP100 method. "
        "Check that the brightway project has LCIA methods imported."
    )


def _find_activity(db, name: str, location: str) -> Any | None:
    """Search db for an activity matching name (substring) and location."""
    candidates = [
        a for a in db
        if name in a["name"] and location in a.get("location", "")
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        warnings.warn(
            f"Multiple matches for '{name}' @ {location} — using first. "
            "Check activity names in config."
        )
    return candidates[0]


def _resolve_activity(db, tech_cfg: dict) -> tuple[Any, str] | tuple[None, None]:
    """Try preferred location, then fallback_location. Returns (activity, location_used)."""
    for loc_key in ("location", "fallback_location"):
        loc = tech_cfg.get(loc_key)
        if not loc:
            continue
        act = _find_activity(db, tech_cfg["name"], loc)
        if act is not None:
            return act, loc
    return None, None


def check_activity_names(
    bw2_project: str,
    source_db: str,
    config_path: str | pathlib.Path | None = None,
) -> None:
    """Verify all technology activities exist in the source ecoinvent database.

    Run this before building to catch name mismatches early.

    Parameters
    ----------
    bw2_project: brightway2 project name (e.g. "ict-pilot-3.10")
    source_db:   base ecoinvent database name (e.g. "ecoinvent-3.10-cutoff")
    config_path: path to electricity_mix.yaml (default: repo config/)
    """
    import bw2data

    cfg = load_config(config_path)
    bw2data.projects.set_current(bw2_project)
    db = bw2data.Database(source_db)

    print(f"Checking technology activities in '{source_db}':\n")
    all_ok = True
    for tech, tech_cfg in cfg["technology_activities"].items():
        act, loc_used = _resolve_activity(db, tech_cfg)
        if act is None:
            status = "!! NOT FOUND !!"
            all_ok = False
            # Suggest alternatives
            fuzzy = [
                a["name"] for a in db
                if any(w in a["name"].lower()
                       for w in tech_cfg["name"].lower().split()[:3])
            ][:4]
            print(f"  [{status}] {tech}: '{tech_cfg['name']}'")
            if fuzzy:
                print(f"    Suggestions: {fuzzy}")
        else:
            print(f"  [OK @ {loc_used}]  {tech}: '{act['name']}'")

    print()
    if all_ok:
        print("All activities found. Safe to run build_se_electricity().")
    else:
        print("Some activities not found — update config/electricity_mix.yaml before building.")


def build_se_electricity(
    bw2_project: str,
    premise_db_pattern: str,
    se_scenarios: list[str],
    premise_scenarios: list[str],
    years: list[int],
    config_path: str | pathlib.Path | None = None,
    output_path: str | pathlib.Path = "data/outputs/se_electricity_gwp.parquet",
) -> pd.DataFrame:
    """Build Sweden-specific prospective electricity GWP intensity table.

    Parameters
    ----------
    bw2_project:
        brightway2 project containing the premise-modified databases.
    premise_db_pattern:
        f-string pattern for database names. Must contain {pathway} and {year}.
        Example: "ecoinvent-3.10-{pathway}-{year}"
    se_scenarios:
        List of SE electricity scenario names from config (e.g. ["high_ambition", "moderate"]).
    premise_scenarios:
        List of premise pathway names (e.g. ["SSP2-PkBudg650", "SSP2-NPi"]).
        Must correspond positionally to se_scenarios.
    years:
        List of years to calculate (e.g. [2025, 2030, 2035, 2040, 2045, 2050]).
    config_path:
        Path to electricity_mix.yaml. Defaults to repo config/.
    output_path:
        Where to write the parquet output.

    Returns
    -------
    pd.DataFrame with columns:
        se_scenario, premise_scenario, year, technology, share,
        gwp100_per_kwh_tech, gwp100_per_kwh_contribution
    """
    import bw2data
    import bw2calc

    if len(se_scenarios) != len(premise_scenarios):
        raise ValueError("se_scenarios and premise_scenarios must have the same length.")

    cfg = load_config(config_path)
    bw2data.projects.set_current(bw2_project)
    method_key = _find_lcia_method(bw2data)
    print(f"LCIA method: {method_key}")

    rows: list[dict] = []
    scenario_pairs = list(zip(se_scenarios, premise_scenarios))

    for se_sc, premise_sc in scenario_pairs:
        for year in years:
            db_name = premise_db_pattern.format(pathway=premise_sc, year=year)

            if db_name not in bw2data.databases:
                print(f"  Skipping (not built): {db_name}")
                continue

            db = bw2data.Database(db_name)
            print(f"\n[{se_sc} / {premise_sc} / {year}] — {db_name}")

            shares = get_technology_shares(cfg, se_sc, year, interpolate=True)
            tech_configs = cfg["technology_activities"]

            total_gwp = 0.0
            for tech, share in shares.items():
                if share <= 0:
                    continue

                tech_cfg = tech_configs.get(tech)
                if tech_cfg is None:
                    warnings.warn(f"Technology '{tech}' in shares but not in technology_activities. Skipping.")
                    continue

                act, loc_used = _resolve_activity(db, tech_cfg)
                if act is None:
                    print(f"  !! NOT FOUND: {tech} ('{tech_cfg['name']}')")
                    rows.append({
                        "se_scenario": se_sc,
                        "premise_scenario": premise_sc,
                        "year": year,
                        "technology": tech,
                        "location_used": None,
                        "share": share,
                        "gwp100_per_kwh_tech": None,
                        "gwp100_per_kwh_contribution": None,
                        "note": "activity not found",
                    })
                    continue

                lca = bw2calc.LCA({act: 1}, method_key)
                lca.lci()
                lca.lcia()
                gwp_tech = lca.score
                contribution = share * gwp_tech
                total_gwp += contribution

                print(f"  {tech:<20} share={share:.3f}  gwp={gwp_tech:.5f}  contrib={contribution:.5f}")

                rows.append({
                    "se_scenario": se_sc,
                    "premise_scenario": premise_sc,
                    "year": year,
                    "technology": tech,
                    "location_used": loc_used,
                    "share": share,
                    "gwp100_per_kwh_tech": gwp_tech,
                    "gwp100_per_kwh_contribution": contribution,
                    "note": "",
                })

            # Summary row
            rows.append({
                "se_scenario": se_sc,
                "premise_scenario": premise_sc,
                "year": year,
                "technology": "TOTAL",
                "location_used": None,
                "share": 1.0,
                "gwp100_per_kwh_tech": total_gwp,
                "gwp100_per_kwh_contribution": total_gwp,
                "note": "weighted sum",
            })
            print(f"  → SE grid GWP100: {total_gwp:.5f} kgCO2eq/kWh")

    df = pd.DataFrame(rows)

    out = pathlib.Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    df[df.technology != "TOTAL"].to_csv(
        out.with_suffix(".csv"), index=False
    )
    print(f"\nSaved {len(df)} rows to {out}")
    return df


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SE electricity LCA builder")
    parser.add_argument("--check", action="store_true",
                        help="Check activity names in source ecoinvent DB and exit")
    parser.add_argument("--build", action="store_true",
                        help="Build full GWP intensity table")
    parser.add_argument("--bw2-project",   default="ict-pilot-3.10")
    parser.add_argument("--source-db",     default="ecoinvent-3.10-cutoff")
    parser.add_argument("--db-pattern",    default="ecoinvent-3.10-{pathway}-{year}")
    parser.add_argument("--config",        default=None)
    args = parser.parse_args()

    if not any([args.check, args.build]):
        parser.print_help()
        raise SystemExit(0)

    if args.check:
        check_activity_names(args.bw2_project, args.source_db, args.config)

    if args.build:
        cfg = load_config(args.config)
        pairs = get_scenario_pairs(cfg)
        se_scenarios      = [p[0] for p in pairs]
        premise_scenarios = [p[1] for p in pairs]
        db_pattern        = get_premise_db_pattern(cfg, override=args.db_pattern)
        build_se_electricity(
            bw2_project        = args.bw2_project,
            premise_db_pattern = db_pattern,
            se_scenarios       = se_scenarios,
            premise_scenarios  = premise_scenarios,
            years              = [2025, 2030, 2035, 2040, 2045, 2050],
            config_path        = args.config,
        )
