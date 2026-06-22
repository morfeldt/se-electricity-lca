"""Build data/electricity_mix_shares.csv from Energimyndigheten ER 2025:13.

Source : inputs/figurunderlag-langsiktiga-scenarier-2025.xlsx
          Fig. 17 (K4.3) — electricity generation by technology (TWh)
          Fig. 60 (A4.1) — blast furnace gas fuel input to industrial CHP (TWh)
Output : data/electricity_mix_shares.csv
          columns: scenario, year, technology, share

Fig. 17 reports electricity generation in TWh (already in electricity units).

Blast furnace gas (from Fig. 60, "Ind mottryck Hyttgaser") is carved out of
'Industriell kraftvärme' before the CHP split is applied. Fuel input is
converted to electricity via blast_furnace_gas_electrical_efficiency (YAML).
For historical years (2010–2023), the 2020 base-year fuel value is used.

CHP remainder (Kraftvärme + Industriell kraftvärme − blast furnace gas) is
split into biomass_chp / waste_chp / natural_gas using per-scenario fractions
defined in config/electricity_mix.yaml under settings.cogeneration_split.

Historical years ("Statistik" rows, 2010–2023) are replicated across all three
SE scenarios. Year 2024 is not yet available — add manually to the CSV if needed.

Run:
  python scripts/build_electricity_mix.py
"""

import pathlib
import yaml
import pandas as pd

REPO_ROOT    = pathlib.Path(__file__).parent.parent
XLSX_PATH    = REPO_ROOT / "inputs" / "figurunderlag-langsiktiga-scenarier-2025.xlsx"
SHEET        = "Fig. 17 (K4.3)"
SHEET_FIG60  = "Fig. 60 (A4.1)"
CONFIG       = REPO_ROOT / "config" / "electricity_mix.yaml"
OUT_CSV      = REPO_ROOT / "data" / "electricity_mix_shares.csv"

SE_SCENARIOS = ["lokal_miljohansyn", "globalt_miljoperspektiv", "regional_forsorjning"]

SERIES_MAP = {
    "Statistik":               "historical",
    "Lokal miljöhänsyn":       "lokal_miljohansyn",
    "Globalt miljöperspektiv": "globalt_miljoperspektiv",
    "Regional försörjning":    "regional_forsorjning",
}

# Fig. 17 column name → intermediate key
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

# ── Load config ───────────────────────────────────────────────────────────────
with open(CONFIG) as f:
    cfg = yaml.safe_load(f)

chp_split     = cfg["settings"]["cogeneration_split"]
BFG_EFF       = cfg["settings"].get("blast_furnace_gas_electrical_efficiency", 0.26)
hydro_split   = cfg["settings"].get("hydro_split", {"run_of_river": 0.80, "reservoir": 0.20})
nuclear_split = cfg["settings"].get("nuclear_split", {"pwr": 0.30, "bwr": 0.70})

# ── Read blast furnace gas fuel input from Fig. 60 ────────────────────────────
# "Ind mottryck Hyttgaser" = industrial backpressure, blast furnace gas (TWh fuel input).
# Fig. 60 header is at Excel row 3 (0-indexed row 2 when read without header).
df60_raw = pd.read_excel(XLSX_PATH, sheet_name=SHEET_FIG60, header=None)
# Row 2 contains column names; rows 3+ contain data
_col_names = df60_raw.iloc[2].tolist()
df60 = df60_raw.iloc[3:].copy()
df60.columns = range(len(df60.columns))

# Identify the "Ind mottryck Hyttgaser" column by name in the header row
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

# Build lookup: {scenario_key: {year: fuel_TWh}}
bfg_lookup: dict[str, dict[int, float]] = {}
for sc in SE_SCENARIOS:
    sc_rows = df60[df60["scenario"] == sc].set_index("year")["bfg_fuel_twh"].to_dict()
    bfg_lookup[sc] = sc_rows

# 2020 base-year fuel value (identical across all scenarios — used for historical years)
_bfg_2020_fuel = df60.loc[df60["year"] == 2020, "bfg_fuel_twh"].mean()

# ── Read Excel ────────────────────────────────────────────────────────────────
df = pd.read_excel(XLSX_PATH, sheet_name=SHEET, header=31)

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

# ── Map and aggregate columns ─────────────────────────────────────────────────
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
df["net_imports"]   = df["net_imports"].clip(lower=0)

# ── Blast furnace gas electricity (carved out of chp_industrial) ──────────────
def _bfg_electricity(row) -> float:
    """Look up blast furnace gas fuel (TWh) for this scenario/year and convert to electricity."""
    sc   = row["scenario"]
    year = row["year"]
    if sc == "historical":
        fuel = _bfg_2020_fuel
    else:
        # Use scenario value if available; fall back to 2020 base year
        fuel = bfg_lookup.get(sc, {}).get(year, _bfg_2020_fuel)
    return fuel * BFG_EFF

df["blast_furnace_gas"] = df.apply(_bfg_electricity, axis=1)
# Ensure blast furnace gas does not exceed the industrial CHP column
df["blast_furnace_gas"] = df["blast_furnace_gas"].clip(upper=df["chp_industrial"])
# Industrial CHP remainder → 100% biomass (pulp/paper residues); no further split needed
df["biomass_chp_industrial"] = df["chp_industrial"] - df["blast_furnace_gas"]
# NOTE: net export years (negative values) are clipped to 0 — exports are not
# handled. From a consumption-based perspective, exported electricity could be
# discounted from the domestic mix (Sweden exports surplus hydro/nuclear to
# dirtier grids, which may affect the attributional vs. consequential framing).
# Left as a future methodological refinement.

# ── Apply district CHP split (Kraftvärme only) with linear interpolation ───────
# Industriell kraftvärme is already handled above (100% biomass_chp_industrial).
# Historical years use the "historical" split fractions.
# Future years interpolate linearly from the historical values (at last_hist_year)
# to the scenario-specific values (at INTERP_TARGET_YEAR), then held constant.
INTERP_TARGET_YEAR = 2050
last_hist_year = df[df["scenario"] == "historical"]["year"].max()

df["_frac"] = ((df["year"] - last_hist_year) / (INTERP_TARGET_YEAR - last_hist_year)).clip(0, 1)
df.loc[df["scenario"] == "historical", "_frac"] = 0.0

for tech in ["biomass_chp", "waste_chp", "natural_gas"]:
    hist_val = chp_split["historical"][tech]
    sc_vals  = df["scenario"].map({sc: v[tech] for sc, v in chp_split.items()}).fillna(hist_val)
    df[tech] = df["chp_district"] * (hist_val * (1 - df["_frac"]) + sc_vals * df["_frac"])

df = df.drop(columns=["_frac"])

# ── Compute shares ────────────────────────────────────────────────────────────
TECH_COLS = [
    "hydro_ror", "hydro_reservoir", "nuclear_pwr", "nuclear_bwr",
    "wind_onshore", "wind_offshore",
    "solar_pv", "biomass_chp_industrial", "biomass_chp", "waste_chp", "natural_gas",
    "blast_furnace_gas", "net_imports",
]

df["total_twh"] = df[TECH_COLS].sum(axis=1)
for col in TECH_COLS:
    df[col] = df[col] / df["total_twh"]

# ── Reshape to tidy ───────────────────────────────────────────────────────────
df_tidy = df[["scenario", "year"] + TECH_COLS].melt(
    id_vars=["scenario", "year"], var_name="technology", value_name="share"
)

# ── Replicate historical across scenarios; drop overlap years from scenarios ──
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

# ── Sanity check ─────────────────────────────────────────────────────────────
totals = df_out.groupby(["scenario", "year"])["share"].sum()
bad = totals[abs(totals - 1.0) > 0.02]
if not bad.empty:
    print("WARNING: share sums deviate from 1.0:")
    print(bad)

# ── Write ─────────────────────────────────────────────────────────────────────
OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
df_out.to_csv(OUT_CSV, index=False)
print(f"Written: {OUT_CSV}  ({len(df_out)} rows)")
print(f"Years:     {sorted(df_out['year'].unique())}")
print(f"Scenarios: {sorted(df_out['scenario'].unique())}")
