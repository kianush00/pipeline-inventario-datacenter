# 🏢 Datacenter Inventory Pipeline

[![Python Version](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![NetBox Integration](https://img.shields.io/badge/NetBox-4.6%2B-0060B8.svg)](https://netbox.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Automated, idempotent, and validated ETL pipeline to collect, consolidate, and synchronize physical and virtual datacenter infrastructure directly into NetBox.**

---

## About The Project

Managing datacenter infrastructure across physical servers, hypervisors, and virtual machines often leads to fragmented data between runtime environments and static spreadsheets.

This repository provides an end-to-end **Data Center Inventory ETL Pipeline** designed to bridge that gap. It automatically gathers hardware and system metadata from target nodes using **Rundeck**, cleanses and normalizes raw outputs, merges them deterministically against a master spreadsheet (source of truth), and idempotently synchronizes the consolidated inventory into **NetBox**.

This project is intentionally designed and optimized for small to medium-sized datacenters, comfortably scaling to manage infrastructure on the order of hundreds or thousands of nodes.

## Key Features

* **Automated Data Collection:** Leverages Rundeck and shell collection agents (`collectors/asset_information.sh`) to query live OS, CPU, RAM, BIOS, network interfaces, and storage data.
* **Master Spreadsheet Integration:** Processes `.ods` or `.xlsx` files seamlessly using LibreOffice, preserving manual metadata and extra columns.
* **Controlled Merging:** Uses unique machine UUIDs as merge keys with configurable column update flags (`0`: preserve master, `1`: allow update, `2`: merge key).
* **NetBox Synchronization:** Idempotent export stage powered by `pynetbox` with dry-run capabilities (`--dry-run`), mapping custom fields, roles, sites, interfaces, and IP allocations via a decoupled YAML contract (`config/netbox_mapping.yaml`).
* **Strict Validation & Data Quality:** Atomic output handling, header contract checks, and rejection of malformed or duplicate keys.

---

## Quick Start

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .            # installs the package and the `inventory-pipeline` command

# Drop the Rundeck job output (*.log) and the master spreadsheet (*.ods/*.xlsx) into input/
inventory-pipeline                       # parse → prepare → merge → dry-run → confirm → export
inventory-pipeline --update-master       # ...and write output/<master>_updated.<ext> at the end
inventory-pipeline --prune-network-orphans --yes   # unattended, with network pruning
```

The pipeline stops at the first stage that exits with a non-zero code. Each stage can still be run on its own with `python -m inventory_pipeline.<module>` (see [Usage & Configuration](docs/usage_and_config.md)).

---

## Documentation

| Document | Description |
| ---------- | ------------- |
| [Architecture](docs/architecture.md) | Data flow, merge model, UUID normalization, and idempotency design |
| [Components](docs/components.md) | Technical reference for the orchestrator and each pipeline stage |
| [Asset Collection](docs/asset_collection.md) | Rundeck collection script (`asset_information.sh`): collected data and compatibility notes |
| [NetBox Mapping](docs/netbox_mapping.md) | Full specification of `netbox_mapping.yaml` (DDL, DML, casts, transforms) |
| [GLPI Roadmap](docs/glpi_roadmap.md) | Phased plan for the GLPI 11 export stage (`export_to_glpi.py`): decisions, data model, requirements |
| [Usage & Configuration](docs/usage_and_config.md) | Environment setup, NetBox credentials, and pipeline execution |
| [Development](docs/development.md) | Testing, linting, code conventions, and security guidelines |

---

## Project Structure

```text
.
├── src/inventory_pipeline/       # Python package (src layout)
│   ├── run_pipeline.py           # Orchestrator: runs every stage, fail-fast (`inventory-pipeline`)
│   ├── parse_job_output.py       # Raw output → normalized CSV
│   ├── prepare_master_inventory.py # Spreadsheet → clean CSV
│   ├── merge_inventories.py      # CSV consolidation by UUID
│   ├── export_to_netbox.py       # Idempotent NetBox synchronization
│   ├── update_master_inventory.py # Write-back to spreadsheet copy
│   ├── base_inventory.py         # Shared utilities module
│   └── paths.py                  # Default project paths (config/, input/, output/, .env)
├── config/
│   ├── netbox_mapping.yaml       # NetBox mapping contract
│   └── rundeck_header_list.txt   # Column schema and merge flags
├── collectors/
│   └── asset_information.sh      # Rundeck collection agent (Bash, pasted into the job)
├── input/                        # Pipeline inputs (git-ignored, except .gitkeep)
├── output/                       # Generated artifacts (git-ignored, except .gitkeep)
├── pyproject.toml                # Project metadata and tool config (Ruff, Pyright, Pytest)
├── requirements.txt              # Pinned dependencies (lockfile)
├── .env.example                  # NetBox credentials template
├── .pyrefly.toml                 # Pyrefly (static typing) configuration
├── .agents/skills/               # AI agent specialized skills
│   ├── code-reviewer/SKILL.md
│   └── qa-tester/SKILL.md
├── tests/                        # Unit test suite (pytest)
│   ├── conftest.py
│   ├── test_parse_job_output.py
│   ├── test_prepare_master_inventory.py
│   ├── test_merge_inventories.py
│   ├── test_update_master_inventory.py
│   ├── test_export_to_netbox.py
│   └── test_run_pipeline.py
├── docs/                         # Project documentation
│   ├── architecture.md
│   ├── components.md
│   ├── asset_collection.md
│   ├── netbox_mapping.md
│   ├── glpi_roadmap.md
│   ├── usage_and_config.md
│   ├── development.md
│   └── assets/
│       ├── inventory_pipeline.drawio
│       └── inventory_pipeline.png
├── GEMINI.md                     # AI agent rules (Antigravity)
├── CLAUDE.md                     # AI agent rules (Claude)
├── .antigravityignore            # Antigravity ignore rules
├── .gitignore
└── LICENSE
```

## License

See [`LICENSE`](LICENSE).
