# se-electricity-lca

Computes Sweden-specific prospective electricity GWP intensities (kgCO2eq/kWh)
by full LCI reconstruction from technology mix, using premise-modified ecoinvent
databases for scenario-consistent upstream emissions.

## Prerequisites

### 1. Input files

**Energimyndigheten scenario data (required before building)**

Download the scenario data workbook from Energimyndigheten:

> Scenarier över Sveriges energisystem, ER 2025:13.
> *Figurunderlag* (Excel workbook).

Place the file at:

```
inputs/figurunderlag-langsiktiga-scenarier-2025.xlsx
```

The technology mix data used in `config/electricity_mix.yaml` comes from
**tab "Fig. 8 (K3.1)"** in that file. The year entries in the config are
currently placeholders — update them from the workbook before use in
published work.

**Build the electricity mix shares (required before building)**

Run the mix builder to generate `data/electricity_mix_shares.csv` from the
Excel file above:

```bash
python scripts/build_electricity_mix.py
```

Historical years (2010–2023) are read from the "Statistik" rows in the Excel.
Year 2024 is not yet in the source — add it manually to the CSV once SCB
publishes it, or re-run after the next edition of ER is released.

### 2. Python environment

Requires brightway2 (`bw2data`, `bw2calc`) and premise databases already built
in a brightway project. See `pyproject.toml` for dependencies.

Install in editable mode:

```bash
pip install -e .
```

## Usage

**Check that all ecoinvent activities resolve before building:**

```bash
python -m se_electricity_lca.build --check \
  --bw2-project ict-pilot-3.10 \
  --source-db ecoinvent-3.10-cutoff
```

**Build the full GWP intensity table:**

```bash
python -m se_electricity_lca.build --build \
  --bw2-project ict-pilot-3.10
```

Output is written to `data/outputs/se_electricity_gwp.parquet` (and `.csv`).

**Programmatic use:**

```python
from se_electricity_lca import build_se_electricity

df = build_se_electricity(
    bw2_project        = "ict-pilot-3.10",
    premise_db_pattern = "ecoinvent-3.10-{pathway}-{year}",
    se_scenarios       = ["lokal_miljohansyn", "globalt_miljoperspektiv", "regional_forsorjning"],
    premise_scenarios  = ["SSP2-PkBudg650", "SSP2-PkBudg1000", "SSP2-NPi"],
    years              = [2025, 2030, 2035, 2040, 2045, 2050],
)
```

## Configuration

All scenario definitions, technology–activity mappings, and background scenario
pairings are in `config/electricity_mix.yaml`. Edit `premise_scenario` under
each SE scenario to change which premise pathway it is paired with. Edit
`settings.premise_db_pattern` to point to a different ecoinvent version.

## Known limitations

**Electricity trade**: Net imports are included as a technology share using the
SE high-voltage market activity. Net export years are clipped to zero — exports
are not handled. From a consumption-based perspective, Sweden's surplus
clean electricity exports to dirtier grids could warrant a discount on the
domestic mix, but this is left as a future methodological refinement.

## Output columns

| Column | Description |
|---|---|
| `se_scenario` | SE electricity scenario key (e.g. `lokal_miljohansyn`) |
| `premise_scenario` | Premise pathway (e.g. `SSP2-PkBudg650`) |
| `year` | Calculation year |
| `technology` | Technology key or `TOTAL` |
| `location_used` | ecoinvent location actually used |
| `share` | Generation share (fraction of total) |
| `gwp100_per_kwh_tech` | GWP100 for this technology (kgCO2eq/kWh) |
| `gwp100_per_kwh_contribution` | share × gwp100_per_kwh_tech |

`TOTAL` rows (weighted grid average) are included in the parquet only.
