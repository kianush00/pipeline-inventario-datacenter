"""
paths.py
========
Rutas por defecto del proyecto, centralizadas en un único módulo.

Los módulos del paquete viven en src/inventory_pipeline/, pero la
configuración (config/), las entradas del usuario (input/), los artefactos
generados (output/) y el archivo .env residen en la raíz del repositorio.
Resolver esas rutas relativas a cada script obligaría a repetir la misma
aritmética de directorios en cada etapa; aquí se hace una sola vez.

Todas las rutas pueden sobrescribirse desde la línea de comandos de cada
etapa: estas constantes solo definen los valores por defecto.
"""

from pathlib import Path
from typing import Final

# src/inventory_pipeline/paths.py -> parents[0]=inventory_pipeline, [1]=src, [2]=raíz
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
