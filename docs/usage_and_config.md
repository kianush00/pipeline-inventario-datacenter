# Usage and Configuration

This document describes how to configure the environment, the required variables, and how to execute each stage of the pipeline.

---

## Prerequisites

- Python 3.10 or higher
- `pip`
- LibreOffice available as `libreoffice` or `soffice` in `PATH` (for ODS/XLSX conversion and ODS updates)
- NetBox 4.6 or higher (for the export stage)

Python dependencies are pinned in [`requirements.txt`](../requirements.txt). The NetBox exporter uses `pynetbox`, `PyYAML`, `requests`, and `urllib3`; spreadsheet processing uses `openpyxl`.

---

## Virtual Environment Setup

Create and activate a local virtual environment from the root of the repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

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

The exporter requires the following environment variables:

```bash
export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="<write-enabled-token>"
export NETBOX_VERIFY_SSL="true"
```

The default `netbox_mapping.yaml` expects the Site name via the environment:

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

Run the stages from the root of the repository with the virtual environment active:

```bash
# 1. Parse the Rundeck job output
python3 parse_job_output.py job_output.log [parsed_job_output.csv]

# 2. Prepare the master spreadsheet
python3 prepare_master_inventory.py master_inventory.ods [prepared_master_inventory.csv]

# 3. Consolidate the parsed inventory with the master
python3 merge_inventories.py parsed_job_output.csv prepared_master_inventory.csv [merged_inventory.csv]

# 4. (Optional) Update a copy of the master sheet
python3 update_master_inventory.py merged_inventory.csv master_inventory.ods [master_inventory_updated.ods]

# 5. Preview the synchronization with NetBox
python3 export_to_netbox.py merged_inventory.csv --dry-run

# 6. Apply the synchronization to NetBox
python3 export_to_netbox.py merged_inventory.csv
```

### Additional Exporter Options

Each script supports optional paths for its input and output files as described in its help text. The mapping path is optional; by default, `export_to_netbox.py` loads `netbox_mapping.yaml` from the script's directory. A custom mapping can be provided as the second positional argument:

```bash
python3 export_to_netbox.py merged_inventory.csv [custom_netbox_mapping.yaml] --dry-run
```

Use `--verbose` to enable DEBUG logging:

```bash
python3 export_to_netbox.py merged_inventory.csv --dry-run --verbose
```
