# Development and Testing

This document describes how to contribute to the project, run tests, and the code quality tools used.

---

## Run Tests

The repository includes a comprehensive unit test suite that validates logic, edge cases, and API interactions (using mocks). To run the tests:

```bash
pytest tests/
```

Pytest is configured under `[tool.pytest.ini_options]` in [`pyproject.toml`](../pyproject.toml): `pythonpath = ["src"]` lets the tests import the `inventory_pipeline` package even without the editable install, so `pytest` works from the repository root or from inside `tests/`. Patch targets must use the full module path (e.g. `patch("inventory_pipeline.export_to_netbox.ensure_prefix")`).

## Project Configuration (`pyproject.toml`)

[`pyproject.toml`](../pyproject.toml) centralizes the project metadata, packaging and the development tool configuration (Ruff, Pyright and Pytest). `requires-python` is the single source of the minimum Python version.

The project follows the **src layout**: the code lives in `src/inventory_pipeline/` and is installed in editable mode with `pip install -e .`, which also registers the `inventory-pipeline` console command (`[project.scripts]`). Pyright and `python -m inventory_pipeline.<module>` rely on that install to resolve the package. `[project] dependencies` declares the runtime dependencies with lower bounds; exact versions remain pinned in [`requirements.txt`](../requirements.txt), which works as the reproducible lockfile.

Default paths (configuration, inputs, outputs, `.env`) are defined once in `src/inventory_pipeline/paths.py`; do not resolve paths relative to a module's `__file__`.

Before committing new code or deploying changes, make sure all tests pass successfully.

---

## Code Quality Tools

### Static Typing (Pyright)

```bash
.venv/bin/pyright <file>
```

Checks type annotations, missing arguments, and unjustified use of `Any`.

Pyright targets Python 3.12 (`pythonVersion` under `[tool.pyright]` in [`pyproject.toml`](../pyproject.toml)).

### Linting (Ruff)

```bash
# Automatically detect and fix errors
.venv/bin/ruff check <file> --fix

# Format according to PEP 8
.venv/bin/ruff format <file>
```

Ruff infers its target (Python 3.12) from `requires-python` in [`pyproject.toml`](../pyproject.toml), so its modernization rules expect 3.12 syntax.

### Syntax Validation

```bash
.venv/bin/python3 -m py_compile <file>
```

---

## Code Conventions

- **Strong typing:** All Python code uses annotations from the `typing` module. Unjustified `Any` is avoided.
- **Source code in English:** Variables, functions, and classes in English. Comments, logs, and user messages in Spanish.
- **Single Responsibility Principle (SRP):** Functions should do one thing and do it well.
- **Pure Functions & No Silent Side-Effects:** Functions should be as pure as possible, especially in an ETL context. Avoid mutating arguments passed by reference (like caches, dicts, or lists) silently. If a function modifies state or external data structures, it must explicitly return the modified structure (or a tuple containing it) to make the data flow predictable, testable, and to prevent side-effect bugs.
- **Early Returns / Fail-Fast:** Nesting is reduced by returning early in error cases.
- **Idempotency:** Scripts that modify external systems (NetBox) or master files must be strictly idempotent.

For complete engineering guidelines, refer to `CLAUDE.md` at the root of the project.

---

## Data Quality and Security

The pipeline validates required headers, rejects duplicate columns and malformed fields, detects invalid or duplicate merge keys, and validates the required mapping columns before contacting NetBox. Temporary output files are atomically replaced where supported by the individual script.

Given that the inventory contains infrastructure, network, hardware, and operating system information, access must be restricted to:

- Rundeck jobs
- NetBox credentials
- Source spreadsheets
- Generated files and logs

Avoid committing execution artifacts or sensitive inventory data.
