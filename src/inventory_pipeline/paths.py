"""
paths.py
========
Default project paths, centralized in a single module.

The package modules live in src/inventory_pipeline/, but the
configuration (config/), the user inputs (input/), the generated
artifacts (output/) and the .env file reside at the repository root.
Resolving those paths relative to each script would force repeating the same
directory arithmetic in every stage; here it is done only once.

All paths can be overridden from the command line of each
stage: these constants only define the default values.
"""

from pathlib import Path
from typing import Final

# src/inventory_pipeline/paths.py -> parents[0]=inventory_pipeline, [1]=src, [2]=root
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

CONFIG_DIR: Final[Path] = PROJECT_ROOT / "config"
INPUT_DIR: Final[Path] = PROJECT_ROOT / "input"
OUTPUT_DIR: Final[Path] = PROJECT_ROOT / "output"

DEFAULT_HEADER_LIST_PATH: Final[Path] = CONFIG_DIR / "rundeck_header_list.txt"
DEFAULT_MAPPING_PATH: Final[Path] = CONFIG_DIR / "netbox_mapping.yaml"
DOTENV_PATH: Final[Path] = PROJECT_ROOT / ".env"

PARSED_CSV_NAME: Final[str] = "parsed_job_output.csv"
PREPARED_CSV_NAME: Final[str] = "prepared_master_inventory.csv"
MERGED_CSV_NAME: Final[str] = "merged_inventory.csv"
