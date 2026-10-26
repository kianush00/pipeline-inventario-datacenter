# Usage and Configuration

This document describes how to configure the environment, the required variables, and how to execute the pipeline, either end to end with the orchestrator or one stage at a time.

---

## Prerequisites

- Python 3.12 or higher
- `pip`
- LibreOffice available as `libreoffice` or `soffice` in `PATH` (for ODS/XLSX conversion and ODS updates)
- NetBox 4.6 or higher (for the export stage)

Python dependencies are pinned in [`requirements.txt`](../requirements.txt). The NetBox exporter uses `pynetbox`, `pydantic`, `PyYAML`, `requests`, and `urllib3`; spreadsheet processing uses `openpyxl`.

---

## Virtual Environment Setup

Create and activate a local virtual environment from the root of the repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
```

The editable install (`-e .`) registers the `inventory_pipeline` package from `src/` and the `inventory-pipeline` console command. It is required: the stages import each other as `inventory_pipeline.<module>`.

Activate the environment again when a new terminal is opened:

```bash
source .venv/bin/activate
```

The terminal prompt normally shows `(.venv)` while the environment is active. To deactivate it:

```bash
deactivate
```

---

## NetBox Configuration

The exporter requires the following environment variables. They can also be defined in a `.env` file at the repository root (see [`.env.example`](../.env.example)); variables already set in the environment take precedence:

```bash
export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="<write-enabled-token>"
export NETBOX_VERIFY_SSL="true"
```

The default `config/netbox_mapping.yaml` expects the Site name via the environment:

```bash
export NETBOX_SITE_NAME="Main Datacenter"
```

> If you hardcode the site name in the YAML, `NETBOX_SITE_NAME` is no longer required.

### Security Notes

- `NETBOX_VERIFY_SSL` is `true` by default; set it to `false` only when the deployment explicitly requires it.
- The token must have write permissions for the NetBox areas used by the exporter: `dcim`, `virtualization`, `ipam`, `extras`, and `core`.
- **Never** store `NETBOX_TOKEN` in `netbox_mapping.yaml`, version control, command history, or generated inventory files.

---

## Pipeline Execution

### Orchestrated Run (recommended)

1. Copy the raw Rundeck job output (`*.log`) and the master spreadsheet (`*.ods` or `*.xlsx`) into `input/`. The contents of `input/` and `output/` are git-ignored.
2. Run the orchestrator with the virtual environment active:

```bash
inventory-pipeline [--update-master] [--prune-network-orphans] [--yes] [--verbose]
```

The orchestrator (`src/inventory_pipeline/run_pipeline.py`) runs each stage as a subprocess and **stops at the first stage that exits with a non-zero code**, propagating that code:

| # | Stage | Output |
| - | ----- | ------ |
| 1 | `parse_job_output` | `output/parsed_job_output.csv` |
| 2 | `prepare_master_inventory` | `output/prepared_master_inventory.csv` |
| 3 | `merge_inventories` | `output/merged_inventory.csv` |
| 4 | `export_to_netbox --dry-run` | NetBox preview (no writes) |
| — | Confirmation: `¿Aplicar los cambios en NetBox? [s/N]` | — |
| 5 | `export_to_netbox` | Changes applied to NetBox |
| 6 | `update_master_inventory` (only with `--update-master`) | `output/<master>_updated.<ext>` |

| Option | Description |
| ------ | ----------- |
| `--job-log PATH` | Rundeck job output. Defaults to the **only** `*.log` in `input/` |
| `--master PATH` | Master spreadsheet. Defaults to the **only** `*.ods`/`*.xlsx` in `input/` |
| `--update-master` | Writes the updated master copy at the end, only if the real export succeeded, so it always reflects a cycle consistent with NetBox |
| `--prune-network-orphans` | Passed to **both** exports, so the dry-run previews exactly what the real run deletes |
| `-y`, `--yes` | Applies the changes without the interactive confirmation. **Required** when stdin is not a terminal; without it the pipeline aborts before running any stage instead of guessing |
| `--verbose` | DEBUG logs in the orchestrator and the exporter |

Input discovery fails fast when `input/` holds zero or more than one candidate of a kind (hidden files such as `.gitkeep` and LibreOffice `.~lock.*#` files are ignored), so an outdated spreadsheet is never processed by accident. The original master in `input/` is never modified.

Exit codes: `0` (completed, or changes declined at the confirmation), `1` (orchestrator error: missing/ambiguous inputs, or no terminal without `--yes`), the code of the first failed stage, `130` (interrupted with Ctrl+C).

### Running a Single Stage

Each stage can still be run on its own from the repository root. Default paths resolve to `config/` and `output/` (see `src/inventory_pipeline/paths.py`):

```bash
# 1. Parse the Rundeck job output
python -m inventory_pipeline.parse_job_output input/job_output.log [output/parsed_job_output.csv]

# 2. Prepare the master spreadsheet
python -m inventory_pipeline.prepare_master_inventory input/master_inventory.ods [output/prepared_master_inventory.csv]

# 3. Consolidate the parsed inventory with the master
python -m inventory_pipeline.merge_inventories --parsed output/parsed_job_output.csv --master output/prepared_master_inventory.csv [--output output/merged_inventory.csv]

# 4. Preview the synchronization with NetBox
python -m inventory_pipeline.export_to_netbox output/merged_inventory.csv --dry-run

# 5. Apply the synchronization to NetBox
python -m inventory_pipeline.export_to_netbox output/merged_inventory.csv

# 6. (Optional) Update a copy of the master sheet
python -m inventory_pipeline.update_master_inventory --merged output/merged_inventory.csv --master input/master_inventory.ods [--output output/master_inventory_updated.ods]
```

### Merge Options

`merge_inventories.py` takes its inventories as **named options** instead of positional arguments, so an accidental swap of the parsed and master files cannot silently invert the merge:

| Option | Required | Description |
| ------ | -------- | ----------- |
| `-p`, `--parsed` | Yes | Parsed inventory CSV (`parsed_job_output.csv`) |
| `-m`, `--master` | Yes | Prepared master inventory CSV (`prepared_master_inventory.csv`) |
| `-o`, `--output` | No | Output CSV. Defaults to `output/merged_inventory.csv` |
| `-l`, `--header-list` | No | Path to `rundeck_header_list.txt`. Defaults to `config/rundeck_header_list.txt` |

### Master Update Options

`update_master_inventory.py` also uses **named options**, so the merged CSV and the master spreadsheet cannot be swapped by position:

| Option | Required | Description |
| ------ | -------- | ----------- |
| `-i`, `--merged` | Yes | Merged inventory CSV (`merged_inventory.csv`) |
| `-m`, `--master` | Yes | Original master spreadsheet (`.ods` or `.xlsx`) |
| `-o`, `--output` | No | Updated copy of the master. Defaults to `<master>_updated.<ext>` next to the master |

### Shared IP Addresses (Node and Replica)

NetBox rejects an IP address that already exists on another interface (`Duplicate IP address found in global table`) when `ENFORCE_GLOBAL_UNIQUE` is enabled, which is its default. The check ignores the status of the IP or of the interface, so a powered-off original and its replica cannot both hold the same IP.

When the exporter finds an IP already assigned to another interface it **skips that IP and logs a `WARNING`** naming the owning interface and node. The first node processed in the CSV keeps the IP. This is not counted as a synchronization error.

To model nodes that genuinely share an IP, a NetBox administrator must change NetBox itself (set `ENFORCE_GLOBAL_UNIQUE = False`, give the addresses a `vip`/`anycast` role, or place them in different VRFs); the exporter does not do this.

### Additional Exporter Options

Each script supports optional paths for its input and output files as described in its help text. The mapping path is optional; by default, `export_to_netbox` loads `config/netbox_mapping.yaml`. A custom mapping can be provided as the second positional argument:

```bash
python -m inventory_pipeline.export_to_netbox output/merged_inventory.csv [custom_netbox_mapping.yaml] --dry-run
```

Use `--verbose` to enable DEBUG logging:

```bash
python -m inventory_pipeline.export_to_netbox output/merged_inventory.csv --dry-run --verbose
```

To automatically delete interfaces and MAC addresses, and unassign IP addresses from NetBox that no longer exist in the CSV (network pruning), use the `--prune-network-orphans` flag. This action is opt-in and destructive. Use with `--dry-run` first to see what would be deleted/unassigned:

```bash
python -m inventory_pipeline.export_to_netbox output/merged_inventory.csv --prune-network-orphans --dry-run
```

IP addresses with a **role** assigned in NetBox (e.g. `secondary`, `vip`, `vrrp`, `loopback`) are never unassigned: the exporter creates IPs without a role, so a role marks an address curated manually, and the CSV holds a single IP per interface. An orphan interface hosting such an IP is kept (with a `WARNING`), because deleting an interface in NetBox also deletes its IPs; its other IPs and MACs are still pruned.

Interfaces renamed in the master inventory (same IP or MAC under a new name) are renamed in NetBox before pruning, so pruning never deletes them or their IP/MAC objects. Rename detection runs on every export, with or without this flag.
