# se-electricity-lca

Computes Sweden-specific prospective electricity GHG intensities (kgCO2eq/kWh), as well as individual gases, by full LCI reconstruction from technology mix, using premise-modified ecoinvent databases for scenario-consistent upstream emissions.

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

Two tabs are used from this file:

- **Fig. 17 (K4.3)** — electricity generation by technology (TWh), historical and scenario years
- **Fig. 60 (A4.1)** — blast furnace gas fuel input to industrial backpressure CHP (TWh), used to derive the blast furnace gas electricity share per scenario

**Build the electricity mix shares (required before building)**

Run the mix builder to generate `data/electricity_mix_shares.csv`:

```bash
python scripts/build_electricity_mix.py
```

Historical years (2010–2023) come from the "Statistik" rows in Fig. 17. Year 2024 is not yet in the source — add it manually to the CSV once SCB publishes it, or re-run after the next edition of ER is released.

### 2. Python environment

Requires brightway2 (`bw2data`, `bw2calc`) and a brightway project that already has ecoinvent (tested with `ecoinvent-3.10-cutoff`) and any premise-modified databases imported. The project name is passed at runtime via `--bw2-project`. See `pyproject.toml` for dependencies.

Install in editable mode:

```bash
pip install -e .
```

## Usage

**Check that all ecoinvent activities resolve before building:**

```bash
python -m se_electricity_lca --check \
  --bw2-project <your-brightway-project> \
  --source-db ecoinvent-3.10-cutoff
```

**Build the full GWP intensity table:**

```bash
python -m se_electricity_lca --build \
  --bw2-project <your-brightway-project> \
  --source-db ecoinvent-3.10-cutoff
```

Output is written to `data/outputs/se_electricity_gwp.parquet` (and `.csv`). Historical years (2019–2023) are included automatically using the source ecoinvent database and appear as `se_scenario="historical"` rows.

**Benchmark against the ecoinvent SE market activity:**

```bash
python -m se_electricity_lca --benchmark \
  --bw2-project <your-brightway-project> \
  --source-db ecoinvent-3.10-cutoff
```

Compares our computed historical grid intensity for multiple years (2015, 2020, 2023) against ecoinvent's SE market activity at the configured voltage level.

**Programmatic use:**

```python
from se_electricity_lca import build_se_electricity

df = build_se_electricity(
    bw2_project        = "<your-brightway-project>",
    premise_db_pattern = "ecoinvent-3.10-{pathway}-{year}",
    se_scenarios       = ["lokal_miljohansyn", "globalt_miljoperspektiv", "regional_forsorjning"],
    premise_scenarios  = ["SSP2-PkBudg650", "SSP2-PkBudg1000", "SSP2-NPi"],
    years              = [2025, 2030, 2035, 2040, 2045, 2050],
    source_db          = "ecoinvent-3.10-cutoff",
    historical_years   = [2019, 2020, 2021, 2022, 2023],  # default; set [] to skip
)
```

## Configuration

All scenario definitions, technology–activity mappings, and background scenario pairings are in `config/electricity_mix.yaml`. Key configurable assumptions:

| Setting | Default | Description |
|---|---|---|
| `voltage_level` | `"low"` | Output voltage level (`"high"`, `"medium"`, `"low"`) |
| `hydro_split` | `{run_of_river: 0.80, reservoir: 0.20}` | Fraction of Vattenkraft by sub-type |
| `nuclear_split` | `{pwr: 0.30, bwr: 0.70}` | Fraction of Kärnkraft by reactor type |
| `waste_chp_electricity_burden_fraction` | `0.0` | Fraction of gross fossil combustion GHG from waste incineration allocated to electricity (0 = ecoinvent system expansion; electricity GWP = 0) |
| `waste_chp_electricity_yield_kwh_per_kg` | `0.18` | kWh electricity per kg MSW incinerated; used only when burden fraction > 0 |
| `blast_furnace_gas_electrical_efficiency` | `0.26` | Converts Fig. 60 fuel input (TWh) to electricity |
| `cogeneration_split` | see YAML | Kraftvärme fuel mix by scenario (biomass/waste/gas fractions) |

Edit `premise_scenario` under each SE scenario to change which premise pathway it is paired with. Edit `settings.premise_db_pattern` to point to a different ecoinvent version.

## Known limitations

**Electricity trade**: Net imports are included as a technology share using the SE high-voltage market activity. Net export years are clipped to zero — exports are not handled. From a consumption-based perspective, Sweden's surplus clean electricity exports to dirtier grids could warrant a discount on the domestic mix, but this is left as a future methodological refinement.

**Waste CHP**: Ecoinvent uses system expansion for municipal waste incineration — all GHG burden is allocated to the waste treatment functional unit and the electricity by-product carries zero GWP. Setting `waste_chp_electricity_burden_fraction > 0` overrides this with a partial allocation of the direct fossil combustion CO2 (~0.476 kgCO2/kg MSW for SE; biogenic fraction excluded). The denominator is `waste_chp_electricity_yield_kwh_per_kg`. For energy allocation, set the fraction to approximately the electricity share of total energy output (~0.40–0.45 for Swedish MSWI).

**Industrial biomass CHP**: No dedicated ecoinvent activity exists for SE pulp/paper industrial CHP (bark, black liquor). The wood chips 6667 kW activity is used as a proxy; see YAML notes for caveats.

## Output columns

| Column | Unit | Description |
|---|---|---|
| `se_scenario` | — | SE electricity scenario key (e.g. `lokal_miljohansyn`) or `historical` for base years |
| `premise_scenario` | — | Premise pathway (e.g. `SSP2-PkBudg650`), or source db name for historical rows |
| `year` | — | Calculation year |
| `technology` | — | Technology key or `TOTAL` |
| `location_used` | — | ecoinvent location actually used |
| `share` | fraction | Generation share (fraction of total) |
| `gwp100_per_kwh_tech` | kgCO2eq/kWh | GWP100 for this technology at plant gate |
| `gwp100_per_kwh_contribution` | kgCO2eq/kWh | Consumption-based GWP100: `share × gwp_tech × t_d_factor` |
| `CO2_per_kwh_tech` | kgCO2/kWh | Fossil + land-use CO2 for this technology at plant gate |
| `CO2_per_kwh_contribution` | kgCO2/kWh | Consumption-based fossil CO2: `share × CO2_tech × t_d_factor` |
| `CH4_per_kwh_tech` | kgCH4/kWh | Total methane for this technology at plant gate |
| `CH4_per_kwh_contribution` | kgCH4/kWh | Consumption-based CH4: `share × CH4_tech × t_d_factor` |
| `N2O_per_kwh_tech` | kgN2O/kWh | N2O for this technology at plant gate |
| `N2O_per_kwh_contribution` | kgN2O/kWh | Consumption-based N2O: `share × N2O_tech × t_d_factor` |
| `CO2_bio_per_kwh_tech` | kgCO2eq/kWh | Biogenic GWP100 for this technology at plant gate (IPCC 2021 biogenic method) |
| `CO2_bio_per_kwh_contribution` | kgCO2eq/kWh | Consumption-based biogenic CO2eq: `share × CO2_bio_tech × t_d_factor` |
| `voltage_level` | — | Output voltage level (`high`, `medium`, or `low`) — set in YAML |

T&D loss factors and grid infrastructure GWP are derived from ecoinvent's voltage market exchange structure at runtime, not from exogenous assumptions. The voltage chain HV→MV→LV (for `voltage_level: "low"`) is traversed in the base ecoinvent database; losses and non-electricity infrastructure exchanges are accumulated at each stage and expressed per kWh consumed at the target voltage. Premise databases replace SE voltage markets with EUR market groups, so T&D and grid infrastructure are always taken from the base ecoinvent database.

`TOTAL` rows include the grid infrastructure addend (non-electricity exchanges in the voltage chain, LCA'd via the base ecoinvent database). All `_contribution` values are per kWh **consumed** at the configured voltage level. Individual gas columns (CO2, CH4, N2O) are physical mass per kWh (kg/kWh); GWP100 and CO2_bio columns are in kgCO2eq/kWh.

`TOTAL` rows are included in the parquet only (excluded from the `.csv`).

Historical rows (`se_scenario="historical"`, years 2019–2023 by default) use the source ecoinvent database for all technology LCAs and are computed once, independent of the premise scenario loop.

**Benchmark vs ecoinvent**: running `--benchmark` compares historical years (2015, 2020, 2023) against ecoinvent's SE market at the configured voltage level. Differences reflect genuine methodological choices: our mix uses measured Energimyndigheten statistics (more recent and wind-heavy than ecoinvent's embedded SE mix), and settings such as `hydro_split`, `nuclear_split`, and `waste_chp_electricity_burden_fraction` affect the result. At default settings (burden fraction = 0, ecoinvent system expansion for waste), our values run below ecoinvent's; increasing the waste burden fraction raises the total and can overshoot ecoinvent depending on the chosen fraction.
