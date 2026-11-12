"""Shared fixtures for the export_to_netbox unit tests."""

from pathlib import Path

import pytest

from inventory_pipeline.export_to_netbox import (
    MockNetBoxRecord,
    NetBoxMappingConfig,
    load_config,
)
from inventory_pipeline.paths import DEFAULT_MAPPING_PATH

# ============================================================
# REAL YAML CONFIGURATION
# ============================================================


@pytest.fixture(scope="session")
def yaml_path() -> Path:
    """Path to the project's real YAML file."""
    path = DEFAULT_MAPPING_PATH
    assert path.exists(), f"YAML file not found at {path}"
    return path


@pytest.fixture(scope="session", autouse=True)
def mock_env_vars():
    """Mocks the environment variables required by the YAML before loading it."""
    import os

    os.environ["NETBOX_SITE_NAME"] = "Test Site"
    yield
    os.environ.pop("NETBOX_SITE_NAME", None)


@pytest.fixture(scope="session")
def config(yaml_path: Path) -> NetBoxMappingConfig:
    """Configuration instance loaded from the real YAML.

    scope='session' is used because the YAML does not change between tests,
    avoiding re-parsing ~600 lines of YAML on every test.
    """
    return load_config(yaml_path)


# ============================================================
# NETBOX MOCK OBJECTS
# ============================================================


@pytest.fixture
def mock_record() -> MockNetBoxRecord:
    """Simulated NetBox object with a valid ID for generic tests."""
    return MockNetBoxRecord(id=42, name="test-object")


@pytest.fixture
def mock_record_no_id() -> MockNetBoxRecord:
    """Simulated NetBox object without an ID (id=0 by default)."""
    return MockNetBoxRecord(name="no-id")
