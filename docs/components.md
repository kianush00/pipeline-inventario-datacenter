# Pipeline Components

This document describes each component of the pipeline, its purpose, inputs, outputs, and dependencies. The Python stages live in the `inventory_pipeline` package (`src/inventory_pipeline/`); configuration lives in `config/`, user inputs in `input/`, and generated artifacts in `output/`.

---

## Orchestrator

### `run_pipeline.py` (`inventory-pipeline`)

Runs every stage end to end and stops at the first one that exits with a non-zero code.

| Aspect | Detail |
| --------- | --------- |
| **Inputs** | The only `*.log` and the only `*.ods`/`*.xlsx` in `input/` (or `--job-log` / `--master`) |
| **Outputs** | All intermediate artifacts in `output/`; optionally `output/<master>_updated.<ext>` |
| **Sequence** | parse → prepare → merge → export `--dry-run` → confirmation → export → (`--update-master`) |
| **Isolation** | Each stage runs as `python -m inventory_pipeline.<module>` with the same interpreter. Stages exit through `sys.exit()` and the exporter configures logging globally, so a subprocess keeps every stage identical to a manual run and makes its exit code the only success signal |
| **Confirmation** | Interactive `[s/N]` prompt after the dry-run; `--yes` skips it. Without a terminal and without `--yes` it aborts before the first stage instead of guessing |
| **Exit codes** | `0` completed or declined, `1` orchestrator error, code of the first failed stage, `130` on Ctrl+C |

See [Usage & Configuration](usage_and_config.md#orchestrated-run-recommended) for the options.

---

## Rundeck Collection

### `collectors/asset_information.sh`

Bash script executed on each target node by Rundeck. Collects:

- Machine identity (UUID, hostname, serial)
- Operating system and version
- Virtualization type
- Network interfaces (name, state, IP, mask, MAC)
- CPU (model, cores, threads, sockets)
- RAM memory
- BIOS (version, date)
- DMI and hardware metadata
- Storage (disks, RAID, capacity)

Its output is intended to be saved as the result of a Rundeck job (`.log` file). The script is not executed from the repository: its content is pasted into the job as an inline script.

---

## Schema Contract

### `config/rundeck_header_list.txt`

Defines the column contract for collection, parsing, master preparation, and merging. Each entry uses the format:

```text
COLUMN_NAME|FLAG
```

Where `FLAG` controls the merge behavior:

- `0`: Preserve the master value without overwriting.
- `1`: Allow update with non-empty values from parsing.
- `2`: Merge key (exactly one must exist).

See [Architecture](architecture.md) for more details on the merge rules.

---

## Output Parser

### `parse_job_output.py`

Converts the raw Rundeck output into a normalized CSV.

| Aspect | Detail |
| --------- | --------- |
| **Input** | Rundeck output `.log` file |
| **Output** | `output/parsed_job_output.csv` (default) |
| **Validations** | Validates keys against `rundeck_header_list.txt`, rejects malformed or duplicate fields |
| **Flag `0` columns** | Left empty in the output (the master value is the source of truth) |
| **Missing values** | Flag `1`/`2` columns absent from a line are filled with `N/A` |
| **Unknown keys** | A key that is not in `rundeck_header_list.txt` is fatal (exit code `1`) |
| **Invalid lines** | Silently ignored |

---

## Master Preparation

### `prepare_master_inventory.py`

Accepts a master spreadsheet (ODS or XLSX) and converts it to a clean CSV for the merge stage.

| Aspect | Detail |
| --------- | --------- |
| **Input** | Master spreadsheet (`.ods` / `.xlsx`) |
| **Output** | `output/prepared_master_inventory.csv` (default) |
| **External dependency** | LibreOffice (for ODS/XLSX → CSV conversion) |
| **Behavior** | Locates and validates the actual header, preserves additional columns |
| **Header detection** | The master may have a grouped category row above the header. The first row containing every `rundeck_header_list.txt` name exactly once is the real header; earlier rows are discarded. Column order is irrelevant |

---

## Consolidation (Merge)

### `merge_inventories.py`

Overlays the parsed inventory onto the prepared master using the column marked with flag `2` (UUID).

| Aspect | Detail |
| --------- | --------- |
| **Inputs** | `--parsed`/`-p parsed_job_output.csv` + `--master`/`-m prepared_master_inventory.csv` (required named options) |
| **Output** | `--output`/`-o merged_inventory.csv` (optional; defaults to `output/merged_inventory.csv`) |
| **Column schema** | `--header-list`/`-l rundeck_header_list.txt` (optional; defaults to `config/rundeck_header_list.txt`) |
| **Preservation** | Keeps all extra rows and columns from the master |
| **Update** | Only updates fields with flag `1`, and only with parsed values that are not empty or `N/A` |
| **Join semantics** | Left join on the master: every master row is kept. A master row whose key is empty/`N/A`, duplicated, or absent from the parsed inventory is kept **unmerged** |
| **Security** | Never overwrites the merge key. Parsed rows with an invalid or duplicated key are not merged |
| **Argument-order checks** | The inventories are named options (not positional) so swapping the files cannot silently invert the merge. Additionally, aborts if the parsed inventory has columns beyond `rundeck_header_list` (it is likely the master, i.e. swapped arguments); warns if no master key matches the parsed inventory |

---

## Master Update (Optional)

### `update_master_inventory.py`

Writes the consolidated values back to a copy of the original master spreadsheet.

| Aspect | Detail |
| --------- | --------- |
| **Input** | `--merged`/`-i merged_inventory.csv` + `--master`/`-m` original master spreadsheet (required named options) |
| **Output** | `--output`/`-o` updated copy of the master spreadsheet (same extension; defaults to `<master>_updated.<ext>`) |
| **Validation** | Aborts unless the master holds a row identical (names and positions) to the merged CSV header, and the number of data rows below it equals the merged CSV's. Positional row correspondence is never assumed |
| **Writes** | Only the cells whose value changed (numeric formatting differences such as `16` vs `16.0` are not changes). `.xlsx` is edited in place with `openpyxl`; `.ods` goes through an `.ods` → `.xlsx` → `.ods` LibreOffice round trip |
| **Atomicity** | Works on a temporary file and replaces the output with `os.replace()` only on success |
| **Security** | The original master spreadsheet is **never** modified |
| **Dependency** | LibreOffice (for ODS files) |

---

## NetBox Export

### `export_to_netbox.py`

Downstream ETL stage. Reads the consolidated CSV and creates or updates Devices and Virtual Machines in NetBox, along with all dependent entities.

| Aspect | Detail |
| --------- | --------- |
| **Input** | `merged_inventory.csv` + `config/netbox_mapping.yaml` (default) |
| **Output** | Direct synchronization with NetBox via REST API |
| **Idempotency** | Identifies records by UUID (primary) or name (fallback) |
| **Dry-Run** | Supports `--dry-run` for inspection without writing |
| **Scope** | Operates against a **single Site** defined by `NETBOX_SITE_NAME` |

**Synchronized entities:**

- Sites, Manufacturers, Device Types
- Cluster Types (one per distinct hypervisor OS, plus the YAML default as fallback), Clusters
- Platforms, Locations, Racks, Device Roles
- Custom Fields and their Choice Sets, Interfaces, MAC Addresses, Prefixes, IP Addresses
- Automatic primary IPv4 and primary MAC assignment (when the node has exactly one IPv4 candidate)
- Interface renames and, with `--prune-network-orphans`, removal of network orphans

**Exit codes:**

- `0`: No errors.
- `1`: At least one error (row, interface or discarded CSV record), or the run aborted on a configuration or NetBox API error.

---

## Support Modules

### `base_inventory.py`

Shared helpers reused by the stages: `error`/`usage` (abort with exit code `1`), `load_rundeck_header_list` (parses and validates `rundeck_header_list.txt`), quote-aware CSV line splitting (`split_quoted_csv_line`, `strip_quotes`) and value cleaning (`clean_value`, `is_empty_or_na`).

### `paths.py`

Single source of the default project paths (`PROJECT_ROOT`, `config/`, `input/`, `output/`, `.env`, default artifact names). The modules live under `src/` while configuration and data live at the repository root, so these paths are resolved once here instead of relative to each script. Every stage still accepts explicit paths on its command line.
