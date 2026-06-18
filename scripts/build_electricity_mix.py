"""Build data/electricity_mix_shares.csv from Energimyndigheten ER 2025:13.

Source : inputs/figurunderlag-langsiktiga-scenarier-2025.xlsx, tab "Fig. 17 (K4.3)"
Output : data/electricity_mix_shares.csv
          columns: scenario, year, technology, share

Fig. 17 reports electricity generation by technology in TWh. All values are
already in electricity units — no efficiency conversion needed.

CHP (Kraftvärme + Industriell kraftvärme) is reported as a lump sum. It is
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

REPO_ROOT  = pathlib.Path(__file__).parent.parent
XLSX_PATH  = REPO_ROOT / "inputs" / "figurunderlag-langsiktiga-scenarier-2025.xlsx"
SHEET      = "Fig. 17 (K4.3)"
CONFIG     = REPO_ROOT / "config" / "electricity_mix.yaml"
OUT_CSV    = REPO_ROOT / "data" / "electricity_mix_shares.csv"

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

# ── Load CHP split fractions from YAML ───────────────────────────────────────
with open(CONFIG) as f:
    cfg = yaml.safe_load(f)

chp_split = cfg["settings"]["cogeneration_split"]

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

df["nuclear"]  = df["nuclear_existing"] + df["nuclear_new"]
df["solar_pv"] = df["solar_roof"]       + df["solar_ground"]
df["chp_total"] = df["chp_industrial"]  + df["chp_district"]
df["net_imports"] = df["net_imports"].clip(lower=0)
# NOTE: net export years (negative values) are clipped to 0 — exports are not
# handled. From a consumption-based perspective, exported electricity could be
# discounted from the domestic mix (Sweden exports surplus hydro/nuclear to
# dirtier grids, which may affect the attributional vs. consequential framing).
# Left as a future methodological refinement.

# ── Apply CHP split with linear interpolation ─────────────────────────────────
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
    df[tech] = df["chp_total"] * (hist_val * (1 - df["_frac"]) + sc_vals * df["_frac"])

df = df.drop(columns=["_frac"])

# ── Compute shares ────────────────────────────────────────────────────────────
TECH_COLS = [
    "hydro", "nuclear", "wind_onshore", "wind_offshore",
    "solar_pv", "biomass_chp", "waste_chp", "natural_gas", "net_imports",
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
