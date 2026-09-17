"""Fixtures compartidas para las pruebas unitarias de export_to_netbox."""

from pathlib import Path

import pytest

from export_to_netbox import (
    MockNetBoxRecord,
    NetBoxMappingConfig,
    load_config,
)

# ============================================================
# RUTAS
# ============================================================

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
"""Raíz del repositorio, un nivel arriba de tests/."""


# ============================================================
# CONFIGURACIÓN YAML REAL
# ============================================================


@pytest.fixture(scope="session")
def yaml_path() -> Path:
    """Ruta al archivo YAML real del proyecto."""
    path = REPO_ROOT / "netbox_mapping.yaml"
    assert path.exists(), f"No se encontró el archivo YAML en {path}"
    return path


@pytest.fixture(scope="session")
def config(yaml_path: Path) -> NetBoxMappingConfig:
    """Instancia de configuración cargada desde el YAML real.

    Se usa scope='session' porque el YAML no cambia entre tests,
    evitando re-parsear ~600 líneas de YAML en cada test.
    """
    return load_config(yaml_path)


# ============================================================
# OBJETOS MOCK DE NETBOX
# ============================================================


@pytest.fixture
def mock_record() -> MockNetBoxRecord:
    """Objeto simulado de NetBox con ID válido para tests genéricos."""
    return MockNetBoxRecord(id=42, name="test-object")


@pytest.fixture
def mock_record_no_id() -> MockNetBoxRecord:
    """Objeto simulado de NetBox sin ID (por defecto id=0)."""
    return MockNetBoxRecord(name="sin-id")
