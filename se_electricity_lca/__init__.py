"""se-electricity-lca: Sweden-specific prospective electricity LCA.

Full LCI reconstruction from technology mix using premise-modified ecoinvent databases.
Technology shares are user-configurable via config/electricity_mix.yaml.
"""

from .build import build_se_electricity, check_activity_names
from .config import load_config

__all__ = ["build_se_electricity", "check_activity_names", "load_config"]
