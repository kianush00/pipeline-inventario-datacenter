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
python3 merge_inventories.py --parsed parsed_job_output.csv --master prepared_master_inventory.csv [--output merged_inventory.csv]

# 4. (Optional) Update a copy of the master sheet
python3 update_master_inventory.py --merged merged_inventory.csv --master master_inventory.ods [--output master_inventory_updated.ods]

# 5. Preview the synchronization with NetBox
python3 export_to_netbox.py merged_inventory.csv --dry-run

# 6. Apply the synchronization to NetBox
python3 export_to_netbox.py merged_inventory.csv
```

### Merge Options

`merge_inventories.py` takes its inventories as **named options** instead of positional arguments, so an accidental swap of the parsed and master files cannot silently invert the merge:

| Option | Required | Description |
| ------ | -------- | ----------- |
| `-p`, `--parsed` | Yes | Parsed inventory CSV (`parsed_job_output.csv`) |
| `-m`, `--master` | Yes | Prepared master inventory CSV (`prepared_master_inventory.csv`) |
| `-o`, `--output` | No | Output CSV. Defaults to `merged_inventory.csv` next to the script |
| `-l`, `--header-list` | No | Path to `rundeck_header_list.txt`. Defaults to the file next to the script |

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

Each script supports optional paths for its input and output files as described in its help text. The mapping path is optional; by default, `export_to_netbox.py` loads `netbox_mapping.yaml` from the script's directory. A custom mapping can be provided as the second positional argument:

```bash
python3 export_to_netbox.py merged_inventory.csv [custom_netbox_mapping.yaml] --dry-run
```

Use `--verbose` to enable DEBUG logging:

```bash
python3 export_to_netbox.py merged_inventory.csv --dry-run --verbose
```

To automatically delete interfaces and MAC addresses, and unassign IP addresses from NetBox that no longer exist in the CSV (network pruning), use the `--prune-network-orphans` flag. This action is opt-in and destructive. Use with `--dry-run` first to see what would be deleted/unassigned:

```bash
python3 export_to_netbox.py merged_inventory.csv --prune-network-orphans --dry-run
```
