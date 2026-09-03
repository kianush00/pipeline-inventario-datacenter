# 🏢 Datacenter Inventory Pipeline

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![NetBox Integration](https://img.shields.io/badge/NetBox-4.6%2B-0060B8.svg)](https://netbox.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Automated, idempotent, and validated ETL pipeline to collect, consolidate, and synchronize physical and virtual datacenter infrastructure directly into NetBox.**

---

## About The Project

Managing datacenter infrastructure across physical servers, hypervisors, and virtual machines often leads to fragmented data between runtime environments and static spreadsheets.

This repository provides an end-to-end **Data Center Inventory ETL Pipeline** designed to bridge that gap. It automatically gathers hardware and system metadata from target nodes using **Rundeck**, cleanses and normalizes raw outputs, merges them deterministically against a master spreadsheet (source of truth), and idempotently synchronizes the consolidated inventory into **NetBox**.

## Key Features

* **Automated Data Collection:** Leverages Rundeck and shell collection agents (`asset_information.sh`) to query live OS, CPU, RAM, BIOS, network interfaces, and storage data.
* **Master Spreadsheet Integration:** Processes `.ods` or `.xlsx` files seamlessly using LibreOffice, preserving manual metadata and extra columns.
* **Controlled Merging:** Uses unique machine UUIDs as merge keys with configurable column update flags (`0`: preserve master, `1`: allow update, `2`: merge key).
* **NetBox Synchronization:** Idempotent export stage powered by `pynetbox` with dry-run capabilities (`--dry-run`), mapping custom fields, roles, sites, interfaces, and IP allocations via a decoupled YAML contract (`netbox_mapping.yaml`).
* **Strict Validation & Data Quality:** Atomic output handling, header contract checks, and rejection of malformed or duplicate keys.

---

## Architecture and data flow

The project follows the sequence represented in the diagram under the assets folder.

<img src="./assets/inventory_pipeline.png" alt="Datacenter inventory pipeline" width="100%">

Generated `.log`, `.csv`, `.ods`, and `.xlsx` files are runtime artifacts and are ignored by Git. They are shown here only to describe the files passed between scripts.

## Requirements

* Python 3.10 or newer
* `pip`
* LibreOffice available as `libreoffice` or `soffice` in `PATH` for ODS/XLSX conversion and ODS updates
* NetBox 4.6 or newer for the NetBox export stage

Python dependencies are pinned in [`requirements.txt`](requirements.txt). The NetBox exporter uses `pynetbox`, `PyYAML`, `requests`, and `urllib3`; spreadsheet processing uses `openpyxl`.

## Virtual environment setup

Create and activate a project-local virtual environment from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Activate the environment again whenever a new shell is opened:

```bash
source .venv/bin/activate
```

The shell prompt normally shows `(.venv)` while the environment is active. To leave it, run:

```bash
deactivate
```

## Main components

### Rundeck collection

[`asset_information.sh`](asset_information.sh) is executed on each target node by Rundeck. It collects machine identity, operating system, virtualization, network interfaces, CPU, memory, BIOS, DMI, storage, and related metadata. Its output is intended to be saved as a job result log.

### Schema and merge rules

[`rundeck_header_list.txt`](rundeck_header_list.txt) is the schema contract for collection, parsing, master preparation, and merging. Each entry uses:

```text
COLUMN_NAME|FLAG
```

* `0`: preserve the master value and do not update it during the merge
* `1`: allow a non-empty parsed value to update the master value
* `2`: use the column as the unique merge key; exactly one such column must exist

The merge key is currently the machine UUID. The parsers resolve columns by name, so column order does not have to match.

### Raw-output parser

[`parse_job_output.py`](parse_job_output.py) converts Rundeck output into a normalized CSV. It accepts fields in any order, validates keys against `rundeck_header_list.txt`, rejects malformed or duplicate fields, fills missing processed values with `N/A`, and ignores lines that are not valid inventory rows.

### Master inventory preparation

[`prepare_master_inventory.py`](prepare_master_inventory.py) accepts an ODS or XLSX master spreadsheet, uses LibreOffice to convert it to CSV, locates and validates the real header, preserves additional columns, and writes a clean CSV for the merge stage.

### Merge and consolidation

[`merge_inventories.py`](merge_inventories.py) overlays the parsed inventory onto the prepared master inventory using the column flagged with `2`. It preserves every master row and extra column, updates only fields flagged with `1`, never overwrites the key, and excludes rows with invalid or duplicate keys from merging.

### Optional master spreadsheet update

[`update_master_inventory.py`](update_master_inventory.py) writes the merged values back to a copy of the original ODS or XLSX master spreadsheet. The original master is never modified. The output must use the same extension as the input. For ODS files, LibreOffice is required for the conversion round trip.

### NetBox export

[`export_to_netbox.py`](export_to_netbox.py) is the downstream ETL stage. It reads the merged CSV and creates or updates NetBox Devices and Virtual Machines, along with the configured sites, cluster types, manufacturers, device types, platforms, racks, clusters, roles, custom fields, interfaces, and IP addresses. The script operates against a **single NetBox Site** defined by the `NETBOX_SITE_NAME` environment variable; multi-site deployments are not supported.

The exporter is idempotent: it primarily identifies existing Devices and Virtual Machines by the `inventory_uuid` custom field, using the object name as a fallback. When the match is by name only, the update is allowed if the name is unique in both the CSV and NetBox; otherwise the row is skipped. Rows whose machine type is not defined in the mapping are skipped. A row with inconsistent network column lengths still synchronizes its Device or VM, but its interfaces are skipped.

### NetBox mapping contract

[`netbox_mapping.yaml`](netbox_mapping.yaml) is the single source of truth for the mapping between `merged_inventory.csv` and NetBox. It defines:

* the target site and cluster type
* canonical device roles
* machine type and environment choice sets and mappings
* inventory status mappings and defaults
* NetBox custom fields and their object types
* native Device and Virtual Machine field mappings
* network column mappings and interface status values
* the exact inventory column names consumed by the exporter
* values treated as empty and therefore omitted

Keep this file aligned with the merged CSV header. It is independent of `rundeck_header_list.txt`, which controls collection and merge behavior.

### CSV Input Format

The `export_to_netbox.py` script expects specific formats in the CSV cells:

* **Simple fields**: Processed as raw strings (leading/trailing whitespace is stripped).
* **Empty values**: Any cell that exactly matches one of the strings defined in the `empty_values` array in `netbox_mapping.yaml` (e.g., "N/A", "None", "-") is treated as empty/null. These fields are skipped and not exported.
* **Multiple values (Networking)**: The 5 network interface columns (Interfaces, Status, IP, Prefix, MAC) support multiple values per cell. They must be formatted as **comma-separated lists** (e.g., `eth0, eth1`). The order of the items in these 5 columns must perfectly align index-by-index. If any of the network columns have a mismatch in the number of elements, the interfaces for that machine will be skipped entirely.

## Usage

Run the stages from the repository root with the virtual environment active.

```bash
# 1. Parse the Rundeck job output
python parse_job_output.py job_output.log parsed_job_output.csv

# 2. Prepare the master spreadsheet
python prepare_master_inventory.py master_inventory.ods prepared_master_inventory.csv

# 3. Merge parsed data into the master CSV
python merge_inventories.py parsed_job_output.csv prepared_master_inventory.csv merged_inventory.csv

# 4. Optionally update a copy of the master spreadsheet
python update_master_inventory.py merged_inventory.csv master_inventory.ods master_inventory_updated.ods

# 5. Preview the NetBox synchronization
python export_to_netbox.py merged_inventory.csv --dry-run

# 6. Apply the synchronization to NetBox
python export_to_netbox.py merged_inventory.csv
```

Each script also supports optional paths for its input and output files as described in its module help text. The NetBox mapping path is optional; by default, `export_to_netbox.py` loads `netbox_mapping.yaml` from the same directory as the script. A custom mapping can be provided as the second positional argument:

```bash
python export_to_netbox.py merged_inventory.csv custom_netbox_mapping.yaml --dry-run
```

Use `--verbose` with the exporter to enable DEBUG logging:

```bash
python export_to_netbox.py merged_inventory.csv --dry-run --verbose
```

## NetBox configuration

The exporter requires these environment variables:

```bash
export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="<write-enabled-token>"
export NETBOX_VERIFY_SSL="true"
export NETBOX_SITE_NAME="Datacenter Principal"
```

`NETBOX_VERIFY_SSL` defaults to `true`; set it to `false` only when the deployment explicitly requires disabling certificate verification. The token must have write permissions for the NetBox areas used by the exporter, including `dcim`, `virtualization`, `ipam`, `extras`, and `core`.

Never store `NETBOX_TOKEN` in `netbox_mapping.yaml`, source control, command history, or generated inventory files. Run `--dry-run` first to inspect the planned synchronization. The exporter exits with code `0` when no row-level errors occur and code `1` when at least one row produces an error.

## Project structure

```text
.
├── asset_information.sh
├── base_inventory.py
├── export_to_netbox.py
├── merge_inventories.py
├── netbox_mapping.yaml
├── parse_job_output.py
├── prepare_master_inventory.py
├── rundeck_header_list.txt
├── update_master_inventory.py
├── requirements.txt
├── LICENSE
├── CLAUDE.md
├── GEMINI.md
├── README.md
└── assets/
  └── flujo_inventario_datacenter.drawio
```

## Data quality and security

The pipeline validates required headers, rejects duplicate columns and malformed key-value fields, detects invalid or duplicate merge keys, and validates the mapping's required inventory columns before contacting NetBox. Temporary output files are replaced atomically where supported by the individual script.

Because the inventory contains infrastructure, network, hardware, and operating-system information, restrict access to Rundeck jobs, NetBox credentials, source spreadsheets, generated files, and logs. Avoid committing runtime artifacts or sensitive inventory data.

## License

See [`LICENSE`](LICENSE).
