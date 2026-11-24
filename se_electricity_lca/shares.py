"""Build the electricity-mix shares table from the Energimyndigheten scenario Excel.

The shares table (scenario, year, technology, share) is derived from
Energimyndigheten's long-term scenarios report (ER 2025:13):

  Fig. 17 (K4.3) — electricity generation by technology (TWh)
  Fig. 60 (A4.1) — blast furnace gas fuel input to industrial CHP (TWh)

combined with per-scenario split parameters from ``electricity_mix.yaml``
(cogeneration_split, hydro_split, nuclear_split, blast furnace gas efficiency).

The source Excel is a ~1.6 MB file that is deliberately NOT committed to the
repository. At runtime :func:`ensure_shares_csv` downloads it once from the
public Energimyndigheten URL, builds the shares table, and caches the result in
the user cache directory so subsequent calls are offline and instant. Callers
that pass an explicit ``shares_csv`` path bypass all of this.

The dev helper ``scripts/build_electricity_mix.py`` reuses :func:`build_shares_df`
to regenerate the committed CSV from a locally-downloaded Excel.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import urllib.request
import warnings

# Public source for the scenario figure data (Energimyndigheten ER 2025:13).
EXCEL_URL = (
    "https://www.energimyndigheten.se/495b9a/globalassets/energisystem-och-analys/"
    "langsiktiga-scenarier/figurunderlag-langsiktiga-scenarier-2025.xlsx"
)
EXCEL_FILENAME = "figurunderlag-langsiktiga-scenarier-2025.xlsx"

SHEET        = "Fig. 17 (K4.3)"
SHEET_FIG60  = "Fig. 60 (A4.1)"

SE_SCENARIOS = ["lokal_miljohansyn", "globalt_miljoperspektiv", "regional_forsorjning"]

SERIES_MAP = {
    "Statistik":               "historical",
    "Lokal miljöhänsyn":       "lokal_miljohansyn",
    "Globalt miljöperspektiv": "globalt_miljoperspektiv",
    "Regional försörjning":    "regional_forsorjning",
}

# Fig. 17 column name → intermediate key.
# Nuclear and solar each have two sub-columns that are summed.
COL_MAP = {
    "Vattenkraft":            "hydro",
    "Vindkraft, land":        "wind_onshore",
    "Vindkraft, hav":         "wind_offshore",
    "Kärnkraft, befintlig":   "nuclear_existing",
    "Kärnkraft, ny":          "nuclear_new",
    "Solkraft, tak":          "solar_roof",
    "Solkraft, mark":         "solar_ground",
    "Industriell kraftvärme": "chp_industrial",
    "Kraftvärme":             "chp_district",
    "Import minus export":    "net_imports",
}

INTERP_TARGET_YEAR = 2050

TECH_COLS = [
    "hydro_ror", "hydro_reservoir", "nuclear_pwr", "nuclear_bwr",
    "wind_onshore", "wind_offshore",
    "solar_pv", "biomass_chp_industrial", "biomass_chp", "waste_chp", "natural_gas",
    "blast_furnace_gas", "net_imports",
]


def build_shares_df(xlsx_path: str | pathlib.Path, cfg: dict):
    """Build the tidy shares DataFrame from the scenario Excel and config splits.

    Parameters
    ----------
    xlsx_path:
        Path to ``figurunderlag-langsiktiga-scenarier-2025.xlsx``.
    cfg:
        Parsed ``electricity_mix.yaml`` (must contain ``settings``).

    Returns
    -------
    pandas.DataFrame with columns: scenario, year, technology, share.
    """
    import pandas as pd

    xlsx_path = pathlib.Path(xlsx_path)

    chp_split     = cfg["settings"]["cogeneration_split"]
    BFG_EFF       = cfg["settings"].get("blast_furnace_gas_electrical_efficiency", 0.26)
    hydro_split   = cfg["settings"].get("hydro_split", {"run_of_river": 0.80, "reservoir": 0.20})
    nuclear_split = cfg["settings"].get("nuclear_split", {"pwr": 0.30, "bwr": 0.70})

    # ── Read blast furnace gas fuel input from Fig. 60 ────────────────────────
    # "Ind mottryck Hyttgaser" = industrial backpressure, blast furnace gas
    # (TWh fuel input). Fig. 60 header is at Excel row 3 (0-indexed row 2 when
    # read without header).
    df60_raw = pd.read_excel(xlsx_path, sheet_name=SHEET_FIG60, header=None)
    _col_names = df60_raw.iloc[2].tolist()
    df60 = df60_raw.iloc[3:].copy()
    df60.columns = range(len(df60.columns))

    _bfg_col_idx = next(
        (i for i, name in enumerate(_col_names) if isinstance(name, str) and "Hyttgaser" in name),
        None,
    )
    if _bfg_col_idx is None:
        raise ValueError("Could not find 'Hyttgaser' column in Fig. 60 (A4.1). Check sheet structure.")

    df60 = df60[[0, 1, _bfg_col_idx]].copy()
    df60.columns = ["series", "year", "bfg_fuel_twh"]
    df60["series"] = df60["series"].ffill()
    df60 = df60[pd.to_numeric(df60["year"], errors="coerce").notna()].copy()
    df60["year"] = df60["year"].astype(int)
    df60["scenario"] = df60["series"].map(SERIES_MAP)
    df60 = df60[df60["scenario"].notna()].copy()
    df60["bfg_fuel_twh"] = pd.to_numeric(df60["bfg_fuel_twh"], errors="coerce").fillna(0)

    bfg_lookup: dict[str, dict[int, float]] = {}
    for sc in SE_SCENARIOS:
        sc_rows = df60[df60["scenario"] == sc].set_index("year")["bfg_fuel_twh"].to_dict()
        bfg_lookup[sc] = sc_rows

    _bfg_2020_fuel = df60.loc[df60["year"] == 2020, "bfg_fuel_twh"].mean()

    # ── Read Fig. 17 (electricity generation by technology) ───────────────────
    df = pd.read_excel(xlsx_path, sheet_name=SHEET, header=31)

    df = df.iloc[:, 1:]                                       # drop first (empty) column
    df = df.rename(columns={df.columns[0]: "series",
                            df.columns[1]: "year"})
    df = df.dropna(subset=["series", "year"], how="all")      # drop fully empty rows
    df["series"] = df["series"].ffill()
    df = df[pd.to_numeric(df["year"], errors="coerce").notna()].copy()
    df["year"] = df["year"].astype(int)

    df["scenario"] = df["series"].map(SERIES_MAP)
    df = df[df["scenario"].notna()].copy()
    df = df.iloc[3:].reset_index(drop=True)  # drop duplicate 2015/2020/2023 rows at top of Statistik

    # ── Map and aggregate columns ─────────────────────────────────────────────
    for col, key in COL_MAP.items():
        if col in df.columns:
            df[key] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        else:
            df[key] = 0.0

    df["nuclear"]         = df["nuclear_existing"] + df["nuclear_new"]
    df["nuclear_pwr"]     = df["nuclear"] * nuclear_split["pwr"]
    df["nuclear_bwr"]     = df["nuclear"] * nuclear_split["bwr"]
    df["solar_pv"]        = df["solar_roof"]       + df["solar_ground"]
    df["hydro_ror"]       = df["hydro"] * hydro_split["run_of_river"]
    df["hydro_reservoir"] = df["hydro"] * hydro_split["reservoir"]
    df["net_imports"]     = df["net_imports"].clip(lower=0)

    # ── Blast furnace gas electricity (carved out of chp_industrial) ──────────
    def _bfg_electricity(row) -> float:
        sc   = row["scenario"]
        year = row["year"]
        if sc == "historical":
            fuel = _bfg_2020_fuel
        else:
            fuel = bfg_lookup.get(sc, {}).get(year, _bfg_2020_fuel)
        return fuel * BFG_EFF

    df["blast_furnace_gas"] = df.apply(_bfg_electricity, axis=1)
    df["blast_furnace_gas"] = df["blast_furnace_gas"].clip(upper=df["chp_industrial"])
    df["biomass_chp_industrial"] = df["chp_industrial"] - df["blast_furnace_gas"]

    # ── Apply district CHP split (Kraftvärme only) with linear interpolation ──
    last_hist_year = df[df["scenario"] == "historical"]["year"].max()

    df["_frac"] = ((df["year"] - last_hist_year) / (INTERP_TARGET_YEAR - last_hist_year)).clip(0, 1)
    df.loc[df["scenario"] == "historical", "_frac"] = 0.0

    for tech in ["biomass_chp", "waste_chp", "natural_gas"]:
        hist_val = chp_split["historical"][tech]
        sc_vals  = df["scenario"].map({sc: v[tech] for sc, v in chp_split.items()}).fillna(hist_val)
        df[tech] = df["chp_district"] * (hist_val * (1 - df["_frac"]) + sc_vals * df["_frac"])

    df = df.drop(columns=["_frac"])

    # ── Compute shares ────────────────────────────────────────────────────────
    df["total_twh"] = df[TECH_COLS].sum(axis=1)
    for col in TECH_COLS:
        df[col] = df[col] / df["total_twh"]

    # ── Reshape to tidy ───────────────────────────────────────────────────────
    df_tidy = df[["scenario", "year"] + TECH_COLS].melt(
        id_vars=["scenario", "year"], var_name="technology", value_name="share"
    )

    # ── Replicate historical across scenarios; drop overlap years ─────────────
    df_hist = df_tidy[df_tidy["scenario"] == "historical"].drop(columns="scenario")
    last_hist_year = df_hist["year"].max()

    df_hist_all = pd.concat(
        [df_hist.assign(scenario=sc) for sc in SE_SCENARIOS],
        ignore_index=True,
    )
    df_scenarios = df_tidy[
        (df_tidy["scenario"] != "historical") & (df_tidy["year"] > last_hist_year)
    ]

    df_out = (
        pd.concat([df_hist_all, df_scenarios], ignore_index=True)
        .sort_values(["scenario", "year", "technology"])
        .reset_index(drop=True)
    )

    # ── Sanity check ───────────────────────────────────────────────────────────
    totals = df_out.groupby(["scenario", "year"])["share"].sum()
    bad = totals[abs(totals - 1.0) > 0.02]
    if not bad.empty:
        warnings.warn(f"Share sums deviate from 1.0 for:\n{bad}")

    return df_out


def _cache_dir() -> pathlib.Path:
    """Return the cache directory for downloaded/derived data.

    Honours ``SE_ELECTRICITY_LCA_CACHE`` if set; otherwise uses the per-user
    cache dir (via ``platformdirs`` when available, else ``~/.cache``).
    """
    env = os.environ.get("SE_ELECTRICITY_LCA_CACHE")
    if env:
        return pathlib.Path(env)
    try:
        from platformdirs import user_cache_dir
        return pathlib.Path(user_cache_dir("se_electricity_lca"))
    except ImportError:
        return pathlib.Path.home() / ".cache" / "se_electricity_lca"


def _config_hash(cfg: dict) -> str:
    """Short, stable hash of the config settings that affect the shares table."""
    settings = cfg.get("settings", {})
    keys = (
        "cogeneration_split",
        "blast_furnace_gas_electrical_efficiency",
        "hydro_split",
        "nuclear_split",
    )
    relevant = {k: settings.get(k) for k in keys}
    blob = json.dumps(relevant, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def _ssl_context():
    """TLS context using certifi's CA bundle when available.

    Avoids the common macOS "certificate verify failed: unable to get local
    issuer certificate" error with python.org Python installs. Returns None to
    fall back to the stdlib default context when certifi is not installed.
    """
    try:
        import ssl

        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _download_excel(dest: pathlib.Path) -> None:
    """Download the scenario Excel to ``dest`` (atomically)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(EXCEL_URL, headers={"User-Agent": "se-electricity-lca"})
    try:
        with urllib.request.urlopen(req, context=_ssl_context()) as resp, open(tmp, "wb") as f:  # noqa: S310 (trusted URL)
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as exc:  # pragma: no cover - network dependent
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"Failed to download electricity-mix source data from {EXCEL_URL}.\n"
            "Provide a pre-built shares CSV via the shares_csv argument, or set "
            "SE_ELECTRICITY_LCA_CACHE to a directory containing "
            f"'{EXCEL_FILENAME}'."
        ) from exc
    tmp.replace(dest)


def ensure_shares_csv(cfg: dict) -> pathlib.Path:
    """Return the path to a cached shares CSV, building it on first use.

    Downloads the source Excel once (cached in the user cache dir), then builds
    and caches the shares CSV keyed on the config split parameters so that
    changing those settings regenerates the table.
    """
    cache = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)

    csv_path = cache / f"electricity_mix_shares-{_config_hash(cfg)}.csv"
    if csv_path.exists():
        return csv_path

    xlsx_path = cache / EXCEL_FILENAME
    if not xlsx_path.exists():
        _download_excel(xlsx_path)

    df = build_shares_df(xlsx_path, cfg)

    tmp = csv_path.with_suffix(".csv.part")
    df.to_csv(tmp, index=False)
    tmp.replace(csv_path)
    return csv_path
