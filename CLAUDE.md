---
description: Global rules and context of the Datacenter Inventory Pipeline project
trigger: always_on
---

# Project Context

**Datacenter Inventory Pipeline**: idempotent ETL that collects physical servers and VMs from Rundeck and master spreadsheets (CSV/ODS/XLSX) and synchronizes them with **NetBox v4.6+**.

Flow:

1. Collection (Rundeck -> `collectors/asset_information.sh`).
2. Parsing (`parse_job_output.py`).
3. Master preparation (`prepare_master_inventory.py`).
4. Consolidation using a unique UUID as the key (`merge_inventories.py`).
5. Idempotent export to NetBox via the REST API (`export_to_netbox.py` + `config/netbox_mapping.yaml`).

The Python modules live in `src/inventory_pipeline/` (src layout, `pip install -e .`). The orchestrator `run_pipeline.py` (`inventory-pipeline` command) runs stages 2 to 5 with fail-fast, reads from `input/` and writes to `output/`. The default paths are centralized in `paths.py`. The architecture and the data contracts are documented in `docs/`.

## 1. Typing

- The code (tests included) must pass Pyright without warnings. `Any` only when there is no reasonable alternative.
- Model structures and payloads with the constructs the project already uses (`NamedTuple`, `TypedDict`, `dataclass`, Pydantic models). Reuse the types already declared before creating new ones.

## 2. Design

- **No hidden side effects:** do not mutate received arguments (caches, dicts, lists). If a function produces new state, it returns it explicitly.
- **Consistency:** replicate the existing patterns of the module (exception handling, control structure, utilities). If a transformation, parsing or validation is repeated, extract it into a reusable utility instead of duplicating it.
- **Errors:** never fail silently; log with `logging`, with context and the right level.
- **Scale:** assume thousands of nodes/rows. Avoid quadratic costs; use dicts/sets for lookups. Prefer fail-fast and early returns.

## 3. Idempotency

- Everything that writes to NetBox or to master files must be idempotent: re-running with the same input generates neither duplicates nor state changes.
- Every write operation honors `--dry-run`.

## 4. Language and Documentation

- Everything is written in English: identifiers, comments, docstrings, logs, user-facing messages, tests and documentation (`docs/*.md`).
- Docstrings in modules, classes and critical functions, explaining the "why" of the business decisions.
- If a change affects behavior, architecture or contracts (e.g. `netbox_mapping.yaml`), update `docs/` in the same change.

## 5. Communication

Direct and technical, without preambles or praise.

## 6. Verification

Before considering a change finished, run the following on the modified files and leave it clean (on Windows, the binaries are in `.venv\Scripts\`):

- **Python:** `.venv/bin/pyright <file>`, `.venv/bin/ruff check <file> --fix`, `.venv/bin/ruff format <file>`.
- **Bash (`collectors/*.sh`):** `bash -n <script>` and `shellcheck <script>`. Suppress false positives only with `# shellcheck disable=SCxxxx` and a comment that justifies it. The `.sh` files must keep LF line endings.
- **Pipeline:** if test data is available, validate with `--dry-run`.
- **Tests:** `.venv/bin/pytest tests/` at the end, always.

## 7. Tests

- Cover the new code and review the existing tests touched by the change: update them if their contract changed or if they became blind to new edge cases.
- Do not add tests that are redundant with scenarios already covered.
- All `import` statements go at the top of the test file, never inside functions.

## 8. Mandatory Review (Skills)

If the change touches parsing logic, `netbox_mapping.yaml`, the interaction with the API in `export_to_netbox.py`, or exceeds ~30 modified lines, do not consider it finished without going through:

1. `qa-tester` (`.agents/skills/qa-tester/SKILL.md`).
2. `code-reviewer` (`.agents/skills/code-reviewer/SKILL.md`), on the final diff.

The final report must include a summary of the findings of both reviews.
