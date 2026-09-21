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

## Quick Start

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the pipeline
python parse_job_output.py job_output.log parsed_job_output.csv
python prepare_master_inventory.py master_inventory.ods prepared_master_inventory.csv
python merge_inventories.py parsed_job_output.csv prepared_master_inventory.csv merged_inventory.csv
python export_to_netbox.py merged_inventory.csv --dry-run
```

---

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/architecture.md) | Data flow, merge model, UUID normalization, and idempotency design |
| [Components](docs/components.md) | Technical reference for each pipeline script |
| [NetBox Mapping](docs/netbox_mapping.md) | Full specification of `netbox_mapping.yaml` (DDL, DML, casts, transforms) |
| [Usage & Configuration](docs/usage_and_config.md) | Environment setup, NetBox credentials, and pipeline execution |
| [Development](docs/development.md) | Testing, linting, code conventions, and security guidelines |

---

## Project Structure

```text
.
├── asset_information.sh          # Rundeck collection agent (Bash)
├── parse_job_output.py           # Raw output → normalized CSV
├── prepare_master_inventory.py   # Spreadsheet → clean CSV
├── merge_inventories.py          # CSV consolidation by UUID
├── update_master_inventory.py    # Write-back to spreadsheet copy
├── export_to_netbox.py           # Idempotent NetBox synchronization
├── base_inventory.py             # Shared utilities module
├── netbox_mapping.yaml           # NetBox mapping contract
├── rundeck_header_list.txt       # Column schema and merge flags
├── requirements.txt
├── conftest.py
├── tests/                        # Unit test suite (pytest)
├── docs/                         # Project documentation
│   ├── architecture.md
│   ├── components.md
│   ├── netbox_mapping.md
│   ├── usage_and_config.md
│   ├── development.md
│   └── assets/
│       ├── inventory_pipeline.drawio
│       └── inventory_pipeline.png
├── GEMINI.md                     # AI agent rules (Antigravity)
├── CLAUDE.md                     # AI agent rules (Claude)
└── LICENSE
```

## License

See [`LICENSE`](LICENSE).
