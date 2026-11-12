"""Integrity tests for the default paths defined in paths.py.

They protect against refactors that move paths.py (breaking the
PROJECT_ROOT computation with parents[2]) or rename the versioned files in config/.
input/ and output/ are not required to exist: they are git-ignored and the
orchestrator creates output/ when it runs.
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
    """PROJECT_ROOT must point to the repo root, not to src/ or the package."""
    assert (PROJECT_ROOT / "pyproject.toml").is_file()
    assert (PROJECT_ROOT / "src" / "inventory_pipeline" / "paths.py").is_file()


@pytest.mark.parametrize(
    "versioned_file",
    [DEFAULT_MAPPING_PATH, DEFAULT_HEADER_LIST_PATH],
    ids=["netbox_mapping", "rundeck_header_list"],
)
def test_versioned_config_files_exist(versioned_file: Path) -> None:
    """The versioned configuration files must exist in config/."""
    assert versioned_file.is_file(), f"Expected file does not exist: {versioned_file}"
    assert versioned_file.parent == CONFIG_DIR


@pytest.mark.parametrize(
    "project_path",
    [CONFIG_DIR, INPUT_DIR, OUTPUT_DIR, DOTENV_PATH],
    ids=["config", "input", "output", "dotenv"],
)
def test_default_paths_are_direct_children_of_project_root(
    project_path: Path,
) -> None:
    """The default paths hang directly off the repo root, even if they do not exist yet."""
    assert project_path.parent == PROJECT_ROOT
