"""Regenerate data/electricity_mix_shares.csv from the Energimyndigheten Excel.

This is a dev helper for regenerating the committed/working CSV from a locally
downloaded copy of the scenario report. At runtime the package itself downloads
the Excel and builds the shares table on demand (see se_electricity_lca.shares);
this script just lets you (re)build the CSV into the repo without a network call
if you already have the Excel locally.

Source : inputs/figurunderlag-langsiktiga-scenarier-2025.xlsx
          (download from se_electricity_lca.shares.EXCEL_URL if missing)
Output : data/electricity_mix_shares.csv

The build logic lives in se_electricity_lca.shares.build_shares_df; split
parameters (cogeneration_split, hydro/nuclear split, blast furnace gas
efficiency) come from se_electricity_lca/config/electricity_mix.yaml.

Run:
  python scripts/build_electricity_mix.py
"""

import pathlib

import yaml

from se_electricity_lca.config import DEFAULT_CONFIG
from se_electricity_lca.shares import EXCEL_FILENAME, build_shares_df

REPO_ROOT = pathlib.Path(__file__).parent.parent
XLSX_PATH = REPO_ROOT / "inputs" / EXCEL_FILENAME
OUT_CSV   = REPO_ROOT / "data" / "electricity_mix_shares.csv"

if not XLSX_PATH.exists():
    raise FileNotFoundError(
        f"Source Excel not found: {XLSX_PATH}\n"
        "Download it from se_electricity_lca.shares.EXCEL_URL and place it in inputs/."
    )

with open(DEFAULT_CONFIG) as f:
    cfg = yaml.safe_load(f)

df_out = build_shares_df(XLSX_PATH, cfg)

OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
df_out.to_csv(OUT_CSV, index=False)
print(f"Written: {OUT_CSV}  ({len(df_out)} rows)")
print(f"Years:     {sorted(df_out['year'].unique())}")
print(f"Scenarios: {sorted(df_out['scenario'].unique())}")
