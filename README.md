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

Two tabs are used from this file:

- **Fig. 17 (K4.3)** — electricity generation by technology (TWh), historical and scenario years
- **Fig. 60 (A4.1)** — blast furnace gas fuel input to industrial backpressure CHP (TWh), used to
  derive the blast furnace gas electricity share per scenario

**Build the electricity mix shares (required before building)**

Run the mix builder to generate `data/electricity_mix_shares.csv`:

```bash
python scripts/build_electricity_mix.py
```

Historical years (2010–2023) come from the "Statistik" rows in Fig. 17. Year 2024 is not yet
in the source — add it manually to the CSV once SCB publishes it, or re-run after the next
edition of ER is released.

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
  --bw2-project ict-pilot-3.10 \
  --source-db ecoinvent-3.10-cutoff
```

Output is written to `data/outputs/se_electricity_gwp.parquet` (and `.csv`).

**Benchmark against the ecoinvent SE market activity:**

```bash
python -m se_electricity_lca.build --benchmark \
  --bw2-project ict-pilot-3.10 \
  --source-db ecoinvent-3.10-cutoff
```

Compares our computed historical grid intensity for multiple years (2015, 2020, 2023) against
ecoinvent's SE market activity at the configured voltage level. T&D losses and grid infrastructure
are derived from the ecoinvent voltage chain. Differences are expected because our mix uses
measured EM statistics; large divergence warrants investigation.

**Programmatic use:**

```python
from se_electricity_lca import build_se_electricity

df = build_se_electricity(
    bw2_project        = "ict-pilot-3.10",
    premise_db_pattern = "ecoinvent-3.10-{pathway}-{year}",
    se_scenarios       = ["lokal_miljohansyn", "globalt_miljoperspektiv", "regional_forsorjning"],
    premise_scenarios  = ["SSP2-PkBudg650", "SSP2-PkBudg1000", "SSP2-NPi"],
    years              = [2025, 2030, 2035, 2040, 2045, 2050],
    source_db          = "ecoinvent-3.10-cutoff",
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
| `gwp100_per_kwh_tech` | GWP100 for this technology at plant gate (kgCO2eq/kWh generated) |
| `gwp100_per_kwh_contribution` | Consumption-based contribution: `share × gwp_tech × t_d_factor` |
| `voltage_level` | Output voltage level (`high`, `medium`, or `low`) — set in YAML |

T&D loss factors and grid infrastructure GWP are derived from ecoinvent's voltage market
exchange structure at runtime, not from exogenous assumptions. The voltage chain HV→MV→LV
(for `voltage_level: "low"`) is traversed in the base ecoinvent database; losses and
non-electricity infrastructure exchanges are accumulated at each stage and expressed per kWh
consumed at the target voltage. Premise databases replace SE voltage markets with EUR market
groups, so T&D and grid infrastructure are always taken from the base ecoinvent database.

`TOTAL` rows include the grid infrastructure addend (non-electricity exchanges in the voltage
chain, LCA'd via the base ecoinvent database). All GWP values are kgCO2eq/kWh **consumed**
at the configured voltage level.

`TOTAL` rows are included in the parquet only (excluded from the `.csv`).

**Benchmark vs ecoinvent**: running `--build` automatically compares historical years (2015,
2020, 2023) against ecoinvent's SE market at the configured voltage level. Our values are
typically 30–40% below ecoinvent's because ecoinvent's default SE mix is older (less wind)
and uses a mix of run-of-river and reservoir hydro (the latter has ~10× higher GWP than
run-of-river due to reservoir methane). Blast furnace gas from steel plant CHP is explicitly
modelled here but is phased out in the more ambitious scenarios by 2030.
