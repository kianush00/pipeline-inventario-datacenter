"""Pruebas de integridad de las rutas por defecto definidas en paths.py.

Protegen contra refactors que muevan paths.py (rompiendo el cálculo de
PROJECT_ROOT con parents[2]) o renombren los archivos versionados de config/.
input/ y output/ no se exige que existan: están ignorados por git y el
orquestador crea output/ al ejecutarse.
"""

from pathlib import Path

import pytest

from inventory_pipeline.paths import (
    CONFIG_DIR,
    DEFAULT_HEADER_LIST_PATH,
    DEFAULT_MAPPING_PATH,
    DOTENV_PATH,
    INPUT_DIR,
    OUTPUT_DIR,
    PROJECT_ROOT,
)


def test_project_root_is_repository_root() -> None:
    """PROJECT_ROOT debe apuntar a la raíz del repo, no a src/ ni al paquete."""
    assert (PROJECT_ROOT / "pyproject.toml").is_file()
    assert (PROJECT_ROOT / "src" / "inventory_pipeline" / "paths.py").is_file()


@pytest.mark.parametrize(
    "versioned_file",
    [DEFAULT_MAPPING_PATH, DEFAULT_HEADER_LIST_PATH],
    ids=["netbox_mapping", "rundeck_header_list"],
)
def test_versioned_config_files_exist(versioned_file: Path) -> None:
    """Los archivos de configuración versionados deben existir en config/."""
    assert versioned_file.is_file(), f"No existe el archivo esperado: {versioned_file}"
    assert versioned_file.parent == CONFIG_DIR


@pytest.mark.parametrize(
    "project_path",
    [CONFIG_DIR, INPUT_DIR, OUTPUT_DIR, DOTENV_PATH],
    ids=["config", "input", "output", "dotenv"],
)
def test_default_paths_are_direct_children_of_project_root(
    project_path: Path,
) -> None:
    """Las rutas por defecto cuelgan directamente de la raíz del repo, aunque aún no existan."""
    assert project_path.parent == PROJECT_ROOT
