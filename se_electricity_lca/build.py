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
  gwp100_per_kwh_tech, gwp100_per_kwh_contribution,
  CO2_per_kwh_tech, CO2_per_kwh_contribution,    (kgCO2/kWh, fossil + land use)
  CH4_per_kwh_tech, CH4_per_kwh_contribution,    (kgCH4/kWh, total methane)
  N2O_per_kwh_tech, N2O_per_kwh_contribution,    (kgN2O/kWh)
  CO2_bio_per_kwh_tech, CO2_bio_per_kwh_contribution  (kgCO2eq/kWh, biogenic GWP100)
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

from .config import load_config, get_technology_shares, get_voltage_level

# Suppress UMFPACK near-singular matrix warnings from scipy's sparse solver.
# These are a known benign artifact of ecoinvent's matrix structure and do not
# affect LCA result correctness.
try:
    from scikits.umfpack import UmfpackWarning as _UmfpackWarning
    warnings.filterwarnings("ignore", category=_UmfpackWarning)
except ImportError:
    pass

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


def _find_indicator_methods(bw2data_module) -> dict[str, tuple]:
    """Find LCIA methods for individual gas tracking and biogenic CO2.

    Returns a dict mapping short names to method tuples:
      CO2     — fossil+LU CO2 in kg (Inventory results/emissions to air)
      CH4     — total methane in kg (Inventory results/emissions to air)
      N2O     — N2O in kg (Inventory results/emissions to air)
      CO2_bio — biogenic GWP100 in kgCO2eq (IPCC 2021 climate change: biogenic)
    """
    searches = {
        "CO2":     ("Inventory results and indicators", "emissions to air", "carbon dioxide, fossil and land use"),
        "CH4":     ("Inventory results and indicators", "emissions to air", "total methane"),
        "N2O":     ("Inventory results and indicators", "emissions to air", "N2O"),
        "CO2_bio": ("IPCC 2021", "climate change: biogenic", "global warming potential (GWP100)"),
    }
    found: dict[str, tuple] = {}
    for name, keywords in searches.items():
        for m in bw2data_module.methods:
            if all(k in m for k in keywords) and "no LT" not in str(m):
                found[name] = m
                break
        else:
            warnings.warn(f"Could not find LCIA method for indicator '{name}' — it will be omitted.")
    return found


def _lcia_all(lca, indicator_methods: dict) -> dict[str, float]:
    """Compute LCIA scores for GWP100 and all additional indicators on an already-solved LCI.

    Uses switch_method to reuse the inventory matrix — avoids recomputing the
    expensive LCI factorisation for each indicator.
    """
    scores: dict[str, float] = {"gwp100": lca.score}
    for ind_name, method_key in indicator_methods.items():
        lca.switch_method(method_key)
        lca.lcia()
        scores[ind_name] = lca.score
    return scores


def _find_activity(db, name: str, location: str) -> Any | None:
    """Search db for an activity matching name and location.

    Tries exact name match first; falls back to substring only when no exact
    match exists. When multiple activities share the same name and location
    (e.g. CHP plants with separate electricity and heat reference products),
    the one whose reference product starts with 'electricity' is preferred.
    If that narrows to exactly one candidate, it is returned silently;
    otherwise the first candidate is used with a warning.
    """
    candidates = [
        a for a in db
        if a["name"] == name and a.get("location", "") == location
    ]
    if not candidates:
        candidates = [
            a for a in db
            if name in a["name"] and a.get("location", "") == location
        ]
    if not candidates:
        return None
    if len(candidates) > 1:
        elec_candidates = [
            a for a in candidates
            if a.get("reference product", "").lower().startswith("electricity")
        ]
        if len(elec_candidates) == 1:
            return elec_candidates[0]
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
    db, voltage_level: str, method_key, bw2calc_module,
    indicator_methods: dict | None = None,
) -> tuple[float, dict[str, float]]:
    """Derive T&D loss factor and grid infrastructure impacts from the ecoinvent market chain.

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
    infra_scores : dict[str, float]
        Keys: 'gwp100' plus any keys in indicator_methods.
        Infrastructure impact per kWh consumed at the target voltage.
    """
    _ind = indicator_methods or {}
    target_idx = _VOLTAGE_CHAIN.index(voltage_level)
    levels = _VOLTAGE_CHAIN[:target_idx + 1]

    elec_amounts: dict[str, float] = {}
    direct_infra: dict[str, dict[str, float]] = {}  # per level: {indicator: value}

    for level in levels:
        market_name = _VOLTAGE_MARKETS[level]
        market_act = next(
            (a for a in db
             if a.get("name") == market_name and a.get("location", "") == "SE"),
            None,
        )
        if market_act is None:
            warnings.warn(f"Could not find '{_VOLTAGE_MARKETS[level]}' @ SE — T&D and infra incomplete")
            elec_amounts[level] = 1.0
            direct_infra[level] = {"gwp100": 0.0, **{k: 0.0 for k in _ind}}
            continue

        level_elec = 0.0
        level_infra: dict[str, float] = {"gwp100": 0.0, **{k: 0.0 for k in _ind}}
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
                for ind_name, score in _lcia_all(lca, _ind).items():
                    level_infra[ind_name] = level_infra.get(ind_name, 0.0) + score

        elec_amounts[level] = level_elec if level_elec > 0 else 1.0
        direct_infra[level] = level_infra

    # T&D factor = product of all stage electricity-input amounts
    t_d_factor = 1.0
    for level in levels:
        t_d_factor *= elec_amounts[level]

    # Cascade infrastructure to target-voltage basis.
    # direct_infra[level] is per kWh AT that level; scale up by the product of
    # electricity amounts of all stages between it and the target (consumer side).
    infra_scores: dict[str, float] = {"gwp100": 0.0, **{k: 0.0 for k in _ind}}
    for i, level in enumerate(levels):
        scale = 1.0
        for downstream in levels[i + 1:]:
            scale *= elec_amounts[downstream]
        for ind_name, score in direct_infra[level].items():
            infra_scores[ind_name] = infra_scores.get(ind_name, 0.0) + score * scale

    return t_d_factor, infra_scores


def _waste_chp_gwp_allocated(
    db,
    burden_fraction: float,
    electricity_yield_kwh_per_kg: float,
) -> float:
    """Return waste CHP electricity GWP (kgCO2eq/kWh) via burden-fraction allocation.

    Looks up the direct fossil CO2 biosphere exchange from the main waste
    incineration activity (reference product = 'municipal solid waste').
    CF for Carbon dioxide, fossil is 1.0 kgCO2eq/kgCO2 (IPCC GWP100).
    Material recovery credits remain with the waste treatment functional unit.
    """
    waste_acts = [
        a for a in db
        if a.get("name") == "treatment of municipal solid waste, municipal incineration"
        and a.get("location", "") == "SE"
        and a.get("reference product", "") == "municipal solid waste"
    ]
    if not waste_acts:
        warnings.warn(
            "Could not find waste incineration activity @ SE with ref product "
            "'municipal solid waste'. Returning 0 GWP for waste_chp."
        )
        return 0.0

    fossil_co2 = sum(
        exc["amount"]
        for exc in waste_acts[0].exchanges()
        if exc.get("type") == "biosphere"
        and "Carbon dioxide, fossil" in exc.input.get("name", "")
    )
    if fossil_co2 <= 0:
        warnings.warn("No fossil CO2 biosphere exchange found in waste incineration activity.")
        return 0.0

    return fossil_co2 * burden_fraction / electricity_yield_kwh_per_kg


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
    years: list[int],
    scenario_mapping: dict[str, str | list[str]] | None = None,
    se_scenarios: list[str] | None = None,
    premise_scenarios: list[str] | None = None,
    config_path: str | pathlib.Path | None = None,
    output_path: str | pathlib.Path = "data/outputs/se_electricity_gwp.parquet",
    source_db: str | None = None,
    historical_years: list[int] | None = None,
) -> pd.DataFrame:
    """Build Sweden-specific prospective electricity GWP intensity table.

    Parameters
    ----------
    bw2_project:
        brightway2 project containing the premise-modified databases.
    premise_db_pattern:
        f-string pattern for database names. Must contain {pathway} and {year}.
        Example: "ecoinvent-3.10-{pathway}-{year}"
    years:
        List of years to calculate (e.g. [2025, 2030, 2035, 2040, 2045, 2050]).
    scenario_mapping:
        Dict mapping SE scenario names to premise pathway(s).
        Each value may be a single string or a list of strings (when you want
        the same SE electricity mix paired with multiple premise backgrounds).
        Example: {"lokal_miljohansyn": "SSP1-PkBudg650",
                  "regional_forsorjning": ["SSP2-NPi", "SSP3-rollBack"]}
        Mutually exclusive with se_scenarios / premise_scenarios.
    se_scenarios:
        List of SE electricity scenario names from config (e.g. ["high_ambition", "moderate"]).
        Must be paired positionally with premise_scenarios.
        Use scenario_mapping instead for a more explicit API.
    premise_scenarios:
        List of premise pathway names (e.g. ["SSP2-PkBudg650", "SSP2-NPi"]).
        Must correspond positionally to se_scenarios.
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

    if scenario_mapping is not None:
        scenario_pairs: list[tuple[str, str]] = []
        for se_sc, premise in scenario_mapping.items():
            if isinstance(premise, str):
                scenario_pairs.append((se_sc, premise))
            else:
                for p in premise:
                    scenario_pairs.append((se_sc, p))
    elif se_scenarios is not None and premise_scenarios is not None:
        if len(se_scenarios) != len(premise_scenarios):
            raise ValueError("se_scenarios and premise_scenarios must have the same length.")
        scenario_pairs = list(zip(se_scenarios, premise_scenarios))
    else:
        raise ValueError(
            "Provide either scenario_mapping or both se_scenarios and premise_scenarios."
        )

    cfg = load_config(config_path)
    bw2data.projects.set_current(bw2_project)
    method_key = _find_lcia_method(bw2data)
    indicator_methods = _find_indicator_methods(bw2data)
    voltage_level = get_voltage_level(cfg)
    print(f"LCIA method: {method_key}")
    print(f"Voltage level: {voltage_level}")
    print(f"Indicator methods found: {list(indicator_methods.keys())}")

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
    t_d_factor, infra_scores = _extract_voltage_overhead(
        _source_db_for_td, voltage_level, method_key, bw2calc,
        indicator_methods=indicator_methods,
    )
    grid_infra_gwp = infra_scores["gwp100"]
    print(f"T&D factor: {t_d_factor:.4f}  grid infra: {grid_infra_gwp:.5f} kgCO2eq/kWh  (from {source_db_name})")

    # Waste CHP burden allocation (optional; default = 0, i.e. ecoinvent system expansion)
    _settings = cfg.get("settings", {})
    _waste_burden = _settings.get("waste_chp_electricity_burden_fraction", 0.0)
    _waste_yield  = _settings.get("waste_chp_electricity_yield_kwh_per_kg", 0.18)
    waste_chp_gwp_override: float | None = None
    if _waste_burden > 0:
        waste_chp_gwp_override = _waste_chp_gwp_allocated(
            _source_db_for_td, _waste_burden, _waste_yield
        )
        print(f"Waste CHP GWP override (burden_fraction={_waste_burden:.2f}, "
              f"yield={_waste_yield:.2f} kWh/kg): {waste_chp_gwp_override:.5f} kgCO2eq/kWh")

    if historical_years is None:
        historical_years = [2019, 2020, 2021, 2022, 2023]

    tech_configs = cfg["technology_activities"]
    rows: list[dict] = []

    # ── Historical years (source db, same across all SE scenarios) ────────────
    if historical_years:
        print(f"\n── Historical years (source db: {source_db_name}) ──")
        first_sc = next(iter(cfg["scenarios"]))

        # LCA each technology once in source db — compute all indicators simultaneously
        _zero_scores: dict[str, float] = {"gwp100": 0.0, **{k: 0.0 for k in indicator_methods}}
        hist_tech_info: dict[str, tuple[dict[str, float], str | None]] = {}
        for tech, tech_cfg in tech_configs.items():
            if tech == "grid_infrastructure":
                continue
            act, loc_used = _resolve_activity(_source_db_for_td, tech_cfg)
            if act is None:
                hist_tech_info[tech] = (dict(_zero_scores), None)
                continue
            if tech == "waste_chp" and waste_chp_gwp_override is not None:
                scores = dict(_zero_scores)
                scores["gwp100"] = waste_chp_gwp_override
                if "CO2" in scores:
                    scores["CO2"] = waste_chp_gwp_override
                hist_tech_info[tech] = (scores, loc_used)
            else:
                lca = bw2calc.LCA({act: 1}, method_key)
                lca.lci()
                lca.lcia()
                hist_tech_info[tech] = (_lcia_all(lca, indicator_methods), loc_used)

        for year in historical_years:
            try:
                shares = get_technology_shares(cfg, first_sc, year, interpolate=False)
            except KeyError:
                print(f"  Skipping {year}: not in historical data")
                continue

            print(f"\n[historical / {source_db_name} / {year}]")
            total_consumed: dict[str, float] = {"gwp100": 0.0, **{k: 0.0 for k in indicator_methods}}

            for tech, share in shares.items():
                if share <= 0:
                    continue
                if tech_configs.get(tech) is None:
                    warnings.warn(f"Technology '{tech}' in shares but not in technology_activities. Skipping.")
                    continue

                tech_scores, loc_used = hist_tech_info.get(tech, (dict(_zero_scores), None))
                if loc_used is None:
                    print(f"  !! NOT FOUND: {tech}")
                    rows.append({
                        "se_scenario": "historical", "premise_scenario": source_db_name,
                        "year": year, "technology": tech, "location_used": None,
                        "share": share, "gwp100_per_kwh_tech": None,
                        "gwp100_per_kwh_contribution": None,
                        **{f"{k}_per_kwh_tech": None for k in indicator_methods},
                        **{f"{k}_per_kwh_contribution": None for k in indicator_methods},
                        "voltage_level": voltage_level, "note": "activity not found",
                    })
                    continue

                gwp_tech = tech_scores["gwp100"]
                contribution = share * gwp_tech * t_d_factor
                total_consumed["gwp100"] += contribution
                print(f"  {tech:<20} share={share:.3f}  gwp={gwp_tech:.5f}  contrib={contribution:.5f}")
                row: dict = {
                    "se_scenario": "historical", "premise_scenario": source_db_name,
                    "year": year, "technology": tech, "location_used": loc_used,
                    "share": share, "gwp100_per_kwh_tech": gwp_tech,
                    "gwp100_per_kwh_contribution": contribution,
                    "voltage_level": voltage_level, "note": "",
                }
                for ind_name in indicator_methods:
                    ind_score = tech_scores.get(ind_name, 0.0)
                    ind_contrib = share * ind_score * t_d_factor
                    row[f"{ind_name}_per_kwh_tech"] = ind_score
                    row[f"{ind_name}_per_kwh_contribution"] = ind_contrib
                    total_consumed[ind_name] += ind_contrib
                rows.append(row)

            print(f"  {'grid_infrastructure':<20} share=1.000  gwp={grid_infra_gwp:.5f}  contrib={grid_infra_gwp:.5f}")
            infra_row: dict = {
                "se_scenario": "historical", "premise_scenario": source_db_name,
                "year": year, "technology": "grid_infrastructure",
                "location_used": f"SE ({voltage_level} voltage chain)", "share": 1.0,
                "gwp100_per_kwh_tech": grid_infra_gwp,
                "gwp100_per_kwh_contribution": grid_infra_gwp,
                "voltage_level": voltage_level,
                "note": f"grid infrastructure from SE {voltage_level}-voltage market chain",
            }
            total_consumed["gwp100"] += grid_infra_gwp
            for ind_name in indicator_methods:
                ind_infra = infra_scores.get(ind_name, 0.0)
                infra_row[f"{ind_name}_per_kwh_tech"] = ind_infra
                infra_row[f"{ind_name}_per_kwh_contribution"] = ind_infra
                total_consumed[ind_name] += ind_infra
            rows.append(infra_row)

            total_with_infra = total_consumed["gwp100"]
            total_row: dict = {
                "se_scenario": "historical", "premise_scenario": source_db_name,
                "year": year, "technology": "TOTAL", "location_used": None, "share": 1.0,
                "gwp100_per_kwh_tech": total_with_infra,
                "gwp100_per_kwh_contribution": total_with_infra,
                "voltage_level": voltage_level, "note": "T&D-adjusted + grid infrastructure",
            }
            for ind_name in indicator_methods:
                total_row[f"{ind_name}_per_kwh_tech"] = total_consumed[ind_name]
                total_row[f"{ind_name}_per_kwh_contribution"] = total_consumed[ind_name]
            rows.append(total_row)
            print(f"  → SE grid GWP100 ({voltage_level} voltage, consumed): {total_with_infra:.5f} kgCO2eq/kWh"
                  f"  [T&D factor={t_d_factor:.4f}, grid infra={grid_infra_gwp:.5f}]")

    for se_sc, premise_sc in scenario_pairs:
        for year in years:
            db_name = premise_db_pattern.format(pathway=premise_sc, year=year)

            if db_name not in bw2data.databases:
                print(f"  Skipping (not built): {db_name}")
                continue

            db = bw2data.Database(db_name)
            print(f"\n[{se_sc} / {premise_sc} / {year}] — {db_name}")

            shares = get_technology_shares(cfg, se_sc, year, interpolate=True)

            total_consumed_sc: dict[str, float] = {"gwp100": 0.0, **{k: 0.0 for k in indicator_methods}}
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
                        **{f"{k}_per_kwh_tech": None for k in indicator_methods},
                        **{f"{k}_per_kwh_contribution": None for k in indicator_methods},
                        "voltage_level": voltage_level,
                        "note": "activity not found",
                    })
                    continue

                if tech == "waste_chp" and waste_chp_gwp_override is not None:
                    tech_scores_sc: dict[str, float] = {k: 0.0 for k in indicator_methods}
                    tech_scores_sc["gwp100"] = waste_chp_gwp_override
                    if "CO2" in tech_scores_sc:
                        tech_scores_sc["CO2"] = waste_chp_gwp_override
                else:
                    lca = bw2calc.LCA({act: 1}, method_key)
                    lca.lci()
                    lca.lcia()
                    tech_scores_sc = _lcia_all(lca, indicator_methods)

                gwp_tech = tech_scores_sc["gwp100"]
                contribution = share * gwp_tech * t_d_factor
                total_consumed_sc["gwp100"] += contribution
                print(f"  {tech:<20} share={share:.3f}  gwp={gwp_tech:.5f}  contrib={contribution:.5f}")

                sc_row: dict = {
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
                }
                for ind_name in indicator_methods:
                    ind_score = tech_scores_sc.get(ind_name, 0.0)
                    ind_contrib = share * ind_score * t_d_factor
                    sc_row[f"{ind_name}_per_kwh_tech"] = ind_score
                    sc_row[f"{ind_name}_per_kwh_contribution"] = ind_contrib
                    total_consumed_sc[ind_name] += ind_contrib
                rows.append(sc_row)

            print(f"  {'grid_infrastructure':<20} share=1.000  gwp={grid_infra_gwp:.5f}  contrib={grid_infra_gwp:.5f}")
            sc_infra_row: dict = {
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
            }
            total_consumed_sc["gwp100"] += grid_infra_gwp
            for ind_name in indicator_methods:
                ind_infra = infra_scores.get(ind_name, 0.0)
                sc_infra_row[f"{ind_name}_per_kwh_tech"] = ind_infra
                sc_infra_row[f"{ind_name}_per_kwh_contribution"] = ind_infra
                total_consumed_sc[ind_name] += ind_infra
            rows.append(sc_infra_row)

            total_with_infra = total_consumed_sc["gwp100"]
            sc_total_row: dict = {
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
            }
            for ind_name in indicator_methods:
                sc_total_row[f"{ind_name}_per_kwh_tech"] = total_consumed_sc[ind_name]
                sc_total_row[f"{ind_name}_per_kwh_contribution"] = total_consumed_sc[ind_name]
            rows.append(sc_total_row)
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
    t_d_factor, infra_scores_bench = _extract_voltage_overhead(
        db, voltage_level, method_key, bw2calc
    )
    grid_infra_gwp = infra_scores_bench["gwp100"]
    print(f"T&D factor: {t_d_factor:.4f}  ({(t_d_factor - 1) * 100:.1f}% losses)  "
          f"grid infra: {grid_infra_gwp:.5f} kgCO2eq/kWh\n")

    # Waste CHP burden allocation override (same logic as build_se_electricity)
    _settings = cfg.get("settings", {})
    _waste_burden = _settings.get("waste_chp_electricity_burden_fraction", 0.0)
    _waste_yield  = _settings.get("waste_chp_electricity_yield_kwh_per_kg", 0.18)
    waste_chp_gwp_override: float | None = None
    if _waste_burden > 0:
        waste_chp_gwp_override = _waste_chp_gwp_allocated(db, _waste_burden, _waste_yield)

    # LCA each technology once — shares change per year but activities do not
    tech_gwp: dict[str, float] = {}
    for tech, tech_cfg in tech_configs.items():
        if tech == "grid_infrastructure":
            continue
        act, _ = _resolve_activity(db, tech_cfg)
        if act is None:
            continue
        if tech == "waste_chp" and waste_chp_gwp_override is not None:
            tech_gwp[tech] = waste_chp_gwp_override
        else:
            lca = bw2calc.LCA({act: 1}, method_key)
            lca.lci()
            lca.lcia()
            tech_gwp[tech] = lca.score

    # ecoinvent SE market reference value — single LCA, same for all years
    market_name = _VOLTAGE_MARKETS[voltage_level]
    ecoinvent_act = next(
        (a for a in db if a.get("name") == market_name and a.get("location", "") == "SE"),
        None,
    )
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

