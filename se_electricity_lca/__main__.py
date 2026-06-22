"""CLI entry point — invoked via `python -m se_electricity_lca`."""

import argparse

from .build import (
    build_se_electricity,
    benchmark_against_ecoinvent,
    check_activity_names,
)
from .config import load_config, get_scenario_pairs, get_premise_db_pattern

parser = argparse.ArgumentParser(description="SE electricity LCA builder")
parser.add_argument("--check",     action="store_true",
                    help="Check activity names in source ecoinvent DB and exit")
parser.add_argument("--build",     action="store_true",
                    help="Build full GWP intensity table (runs benchmark automatically)")
parser.add_argument("--benchmark", action="store_true",
                    help="Run benchmark only — compare historical years against ecoinvent SE market")
parser.add_argument("--bw2-project", default="ict-pilot-3.10")
parser.add_argument("--source-db",   default="ecoinvent-3.10-cutoff")
parser.add_argument("--db-pattern",  default="ecoinvent-3.10-{pathway}-{year}")
parser.add_argument("--config",      default=None)
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
