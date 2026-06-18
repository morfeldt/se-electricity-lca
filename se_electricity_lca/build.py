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

from .config import (
    load_config, get_technology_shares, get_scenario_pairs, get_premise_db_pattern,
    get_voltage_level,
)

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
        if name in a["name"] and a.get("location", "") == location
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        warnings.warn(
            f"Multiple matches for '{name}' @ {location} — using first. "
            "Check activity names in config."
        )
    return candidates[0]


_VOLTAGE_MARKETS = {
    "high":   "market for electricity, high voltage",
    "medium": "market for electricity, medium voltage",
    "low":    "market for electricity, low voltage",
}
_VOLTAGE_CHAIN = ["high", "medium", "low"]


def _is_electricity_product(act) -> bool:
    """Return True if this activity's reference product is electricity (not grid infra)."""
    ref = act.get("reference product", "").lower()
    return ref.startswith("electricity,") or ref == "electricity"


def _transformation_input_factor(transformation_act) -> float:
    """Return kWh of upstream electricity input per kWh output for a voltage transformation.

    Voltage transformation activities (e.g. 'electricity voltage transformation
    from high to medium voltage') have a single electricity input at a different
    voltage than their output (e.g. 1.011 kWh HV in → 1 kWh MV out). The market
    exchange amounts tell us how much MV *output* from the transformer we need per
    kWh of MV market output — but to get the T&D factor right we need the upstream
    electricity *input* per kWh of market output, i.e. exchange_amount × this factor.
    """
    out_ref = transformation_act.get("reference product", "").lower()
    for exc in transformation_act.exchanges():
        if exc.get("type") != "technosphere":
            continue
        if not _is_electricity_product(exc.input):
            continue
        in_ref = exc.input.get("reference product", "").lower()
        if in_ref != out_ref:   # different voltage = the primary upstream input
            return exc.get("amount", 1.0)
    return 1.0


def _extract_voltage_overhead(
    db, voltage_level: str, method_key, bw2calc_module
) -> tuple[float, float]:
    """Derive T&D loss factor and grid infrastructure GWP from the ecoinvent market chain.

    Traverses SE market activities from high voltage down to `voltage_level`.
    At each level the electricity input exchange amount encodes the stage loss
    (e.g. 1.025 kWh HV per kWh MV = 2.5% HV-to-MV loss). All non-electricity
    exchanges (cables, transformers, sf6, …) are LCA'd and accumulated as grid
    infrastructure, cascaded to the target voltage basis.

    Returns
    -------
    t_d_factor : float
        kWh of generation per kWh at the target voltage — multiply each
        technology contribution by this to convert plant-gate → consumption basis.
    grid_infra_gwp : float
        Infrastructure GWP (kgCO2eq) per kWh at the target voltage.
    """
    target_idx = _VOLTAGE_CHAIN.index(voltage_level)
    levels = _VOLTAGE_CHAIN[:target_idx + 1]

    elec_amounts: dict[str, float] = {}  # kWh electricity input per kWh output at each level
    direct_infra: dict[str, float] = {}  # infrastructure GWP per kWh output at each level

    for level in levels:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            market_act = _find_activity(db, _VOLTAGE_MARKETS[level], "SE")
        if market_act is None:
            warnings.warn(f"Could not find '{_VOLTAGE_MARKETS[level]}' @ SE — T&D and infra incomplete")
            elec_amounts[level] = 1.0
            direct_infra[level] = 0.0
            continue

        level_elec = 0.0
        level_infra = 0.0
        for exc in market_act.exchanges():
            if exc.get("type") != "technosphere":
                continue
            amount = exc.get("amount", 0.0)
            if amount <= 0:
                continue
            if _is_electricity_product(exc.input):
                if "voltage transformation" in exc.input.get("name", "").lower():
                    # Exchange amount is transformer OUTPUT; multiply by input factor
                    # to get upstream electricity INPUT per kWh of market output.
                    level_elec += amount * _transformation_input_factor(exc.input)
                else:
                    level_elec += amount
            else:
                lca = bw2calc_module.LCA({exc.input: amount}, method_key)
                lca.lci()
                lca.lcia()
                level_infra += lca.score

        elec_amounts[level] = level_elec if level_elec > 0 else 1.0
        direct_infra[level] = level_infra

    # T&D factor = product of all stage electricity-input amounts
    t_d_factor = 1.0
    for level in levels:
        t_d_factor *= elec_amounts[level]

    # Cascade infrastructure to target-voltage basis.
    # direct_infra[level] is per kWh AT that level; scale up by the product of
    # electricity amounts of all stages between it and the target (consumer side).
    grid_infra_gwp = 0.0
    for i, level in enumerate(levels):
        scale = 1.0
        for downstream in levels[i + 1:]:
            scale *= elec_amounts[downstream]
        grid_infra_gwp += direct_infra[level] * scale

    return t_d_factor, grid_infra_gwp


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
        if tech == "grid_infrastructure":
            # Derived from SE market exchanges at runtime — verify the market activity instead
            market_act = _find_activity(db, "market for electricity, high voltage", "SE")
            if market_act is not None:
                n = sum(
                    1 for exc in market_act.exchanges()
                    if exc.get("type") == "technosphere"
                    and "transmission network" in exc.input.get("name", "").lower()
                )
                print(f"  [OK — derived]  grid_infrastructure: {n} transmission network exchange(s) in SE HV market")
            else:
                print(f"  [!! NOT FOUND]  grid_infrastructure: 'market for electricity, high voltage' @ SE missing")
                all_ok = False
            continue

        act, loc_used = _resolve_activity(db, tech_cfg)
        if act is None:
            status = "!! NOT FOUND !!"
            all_ok = False
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
    source_db: str | None = None,
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
    voltage_level = get_voltage_level(cfg)
    print(f"LCIA method: {method_key}")
    print(f"Voltage level: {voltage_level}")

    # T&D factor and grid infra derived from base ecoinvent db (SE voltage market chain).
    # Premise databases replace SE markets with EUR market groups so the chain is not
    # traversable there. T&D losses are physical grid properties; grid infra emissions
    # are taken from the same source-db baseline.
    source_db_name = source_db or cfg.get("settings", {}).get("source_db")
    if not source_db_name:
        raise ValueError(
            "source_db not provided and not set in config settings. "
            "Pass source_db='ecoinvent-3.10-cutoff' (or equivalent)."
        )
    _source_db_for_td = bw2data.Database(source_db_name)
    t_d_factor, grid_infra_gwp = _extract_voltage_overhead(
        _source_db_for_td, voltage_level, method_key, bw2calc
    )
    print(f"T&D factor: {t_d_factor:.4f}  grid infra: {grid_infra_gwp:.5f} kgCO2eq/kWh  (from {source_db_name})")

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

            total_gwp_consumed = 0.0
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
                        "voltage_level": voltage_level,
                        "note": "activity not found",
                    })
                    continue

                lca = bw2calc.LCA({act: 1}, method_key)
                lca.lci()
                lca.lcia()
                gwp_tech     = lca.score
                contribution = share * gwp_tech * t_d_factor
                total_gwp_consumed += contribution

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
                    "voltage_level": voltage_level,
                    "note": "",
                })

            print(f"  {'grid_infrastructure':<20} share=1.000  gwp={grid_infra_gwp:.5f}  contrib={grid_infra_gwp:.5f}")
            rows.append({
                "se_scenario": se_sc,
                "premise_scenario": premise_sc,
                "year": year,
                "technology": "grid_infrastructure",
                "location_used": f"SE ({voltage_level} voltage chain)",
                "share": 1.0,
                "gwp100_per_kwh_tech": grid_infra_gwp,
                "gwp100_per_kwh_contribution": grid_infra_gwp,
                "voltage_level": voltage_level,
                "note": f"grid infrastructure from SE {voltage_level}-voltage market chain",
            })

            total_with_infra = total_gwp_consumed + grid_infra_gwp

            rows.append({
                "se_scenario": se_sc,
                "premise_scenario": premise_sc,
                "year": year,
                "technology": "TOTAL",
                "location_used": None,
                "share": 1.0,
                "gwp100_per_kwh_tech": total_with_infra,
                "gwp100_per_kwh_contribution": total_with_infra,
                "voltage_level": voltage_level,
                "note": "T&D-adjusted + grid infrastructure",
            })
            print(f"  → SE grid GWP100 ({voltage_level} voltage, consumed): {total_with_infra:.5f} kgCO2eq/kWh"
                  f"  [T&D factor={t_d_factor:.4f}, grid infra={grid_infra_gwp:.5f}]")

    df = pd.DataFrame(rows)

    out = pathlib.Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    df[df.technology != "TOTAL"].to_csv(
        out.with_suffix(".csv"), index=False
    )
    print(f"\nSaved {len(df)} rows to {out}")
    return df


def benchmark_against_ecoinvent(
    bw2_project: str,
    source_db: str,
    config_path: str | pathlib.Path | None = None,
    benchmark_years: list[int] | None = None,
) -> None:
    """Compare our computed historical grid GWP against the ecoinvent SE market activity.

    Runs for each requested benchmark year, using the historical technology shares
    (which are identical across scenarios). T&D factor, grid infrastructure GWP,
    and the ecoinvent reference value are derived from the source db and are the
    same for all years.

    Parameters
    ----------
    bw2_project:      brightway2 project name
    source_db:        base ecoinvent database (e.g. "ecoinvent-3.10-cutoff")
    config_path:      path to electricity_mix.yaml (default: repo config/)
    benchmark_years:  years to report; defaults to [2015, 2020, 2023]
    """
    import bw2data
    import bw2calc

    if benchmark_years is None:
        benchmark_years = [2015, 2020, 2023]

    cfg = load_config(config_path)
    bw2data.projects.set_current(bw2_project)
    method_key = _find_lcia_method(bw2data)
    voltage_level = get_voltage_level(cfg)
    db = bw2data.Database(source_db)
    tech_configs = cfg["technology_activities"]

    first_sc = next(iter(cfg["scenarios"]))
    available_hist = sorted(
        y for y in cfg["scenarios"][first_sc]["years"].keys() if y <= 2023
    )

    print(f"\nBenchmark  |  Voltage level: {voltage_level}")
    print(f"Source database: {source_db}")
    print(f"LCIA method: {method_key}")

    # T&D factor and grid infra are db-level constants — compute once
    t_d_factor, grid_infra_gwp = _extract_voltage_overhead(
        db, voltage_level, method_key, bw2calc
    )
    print(f"T&D factor: {t_d_factor:.4f}  ({(t_d_factor - 1) * 100:.1f}% losses)  "
          f"grid infra: {grid_infra_gwp:.5f} kgCO2eq/kWh\n")

    # LCA each technology once — shares change per year but activities do not
    tech_gwp: dict[str, float] = {}
    for tech, tech_cfg in tech_configs.items():
        if tech == "grid_infrastructure":
            continue
        act, _ = _resolve_activity(db, tech_cfg)
        if act is None:
            continue
        lca = bw2calc.LCA({act: 1}, method_key)
        lca.lci()
        lca.lcia()
        tech_gwp[tech] = lca.score

    # ecoinvent SE market reference value — single LCA, same for all years
    market_name = _VOLTAGE_MARKETS[voltage_level]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        ecoinvent_act = _find_activity(db, market_name, "SE")
    if ecoinvent_act is None:
        print(f"Could not find '{market_name}' @ SE in source db.")
        return
    lca_ref = bw2calc.LCA({ecoinvent_act: 1}, method_key)
    lca_ref.lci()
    lca_ref.lcia()
    ecoinvent_value = lca_ref.score

    # Header
    col = 14
    print(f"  {'Year':<6}  {'Ours (kgCO2eq/kWh)':>{col}}  {'ecoinvent (kgCO2eq/kWh)':>{col}}  {'Diff':>7}")
    print(f"  {'----':<6}  {'-'*col}  {'-'*col}  {'----':>7}")

    for year in benchmark_years:
        if year not in available_hist:
            nearest = min(available_hist, key=lambda y: abs(y - year))
            print(f"  {year:<6}  (not in data — nearest available: {nearest})")
            continue
        shares = get_technology_shares(cfg, first_sc, year, interpolate=False)
        total_gwp = sum(
            shares.get(tech, 0.0) * gwp * t_d_factor
            for tech, gwp in tech_gwp.items()
        )
        our_value = total_gwp + grid_infra_gwp
        diff_pct  = (our_value - ecoinvent_value) / ecoinvent_value * 100
        print(f"  {year:<6}  {our_value:>{col}.5f}  {ecoinvent_value:>{col}.5f}  {diff_pct:>+6.1f}%")

    print()
    print("Note: ecoinvent's SE market reflects an average over recent years, not a")
    print("specific year. Differences are expected — our mix uses measured EM statistics.")


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SE electricity LCA builder")
    parser.add_argument("--check", action="store_true",
                        help="Check activity names in source ecoinvent DB and exit")
    parser.add_argument("--build", action="store_true",
                        help="Build full GWP intensity table (runs benchmark automatically)")
    parser.add_argument("--benchmark", action="store_true",
                        help="Run benchmark only — compare 2023 against ecoinvent SE market")
    parser.add_argument("--bw2-project",   default="ict-pilot-3.10")
    parser.add_argument("--source-db",     default="ecoinvent-3.10-cutoff")
    parser.add_argument("--db-pattern",    default="ecoinvent-3.10-{pathway}-{year}")
    parser.add_argument("--config",        default=None)
    args = parser.parse_args()

    if not any([args.check, args.build, args.benchmark]):
        parser.print_help()
        raise SystemExit(0)

    if args.check:
        check_activity_names(args.bw2_project, args.source_db, args.config)

    if args.benchmark:
        benchmark_against_ecoinvent(args.bw2_project, args.source_db, args.config)

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
            source_db          = args.source_db,
        )
        print("\n── Benchmark ─────────────────────────────────────────────────────────────")
        benchmark_against_ecoinvent(args.bw2_project, args.source_db, args.config)
