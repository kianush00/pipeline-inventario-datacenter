# Pipeline Components

This document describes each script in the pipeline, its purpose, inputs, outputs, and dependencies.

---

## Rundeck Collection

### `asset_information.sh`

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

Its output is intended to be saved as the result of a Rundeck job (`.log` file).

---

## Schema Contract

### `rundeck_header_list.txt`

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
| **Output** | `parsed_job_output.csv` |
| **Validations** | Validates keys against `rundeck_header_list.txt`, rejects malformed or duplicate fields |
| **Missing values** | Filled with `N/A` |
| **Invalid lines** | Silently ignored |

---

## Master Preparation

### `prepare_master_inventory.py`

Accepts a master spreadsheet (ODS or XLSX) and converts it to a clean CSV for the merge stage.

| Aspect | Detail |
| --------- | --------- |
| **Input** | Master spreadsheet (`.ods` / `.xlsx`) |
| **Output** | `prepared_master_inventory.csv` |
| **External dependency** | LibreOffice (for ODS/XLSX → CSV conversion) |
| **Behavior** | Locates and validates the actual header, preserves additional columns |

---

## Consolidation (Merge)

### `merge_inventories.py`

Overlays the parsed inventory onto the prepared master using the column marked with flag `2` (UUID).

| Aspect | Detail |
| --------- | --------- |
| **Inputs** | `parsed_job_output.csv` + `prepared_master_inventory.csv` |
| **Output** | `merged_inventory.csv` |
| **Preservation** | Keeps all extra rows and columns from the master |
| **Update** | Only updates fields with flag `1` |
| **Security** | Never overwrites the merge key. Excludes rows with invalid or duplicate keys |

---

## Master Update (Optional)

### `update_master_inventory.py`

Writes the consolidated values back to a copy of the original master spreadsheet.

| Aspect | Detail |
| --------- | --------- |
| **Input** | `merged_inventory.csv` + original master spreadsheet |
| **Output** | Updated copy of the master spreadsheet (same extension) |
| **Security** | The original master spreadsheet is **never** modified |
| **Dependency** | LibreOffice (for ODS files) |

---

## NetBox Export

### `export_to_netbox.py`

Downstream ETL stage. Reads the consolidated CSV and creates or updates Devices and Virtual Machines in NetBox, along with all dependent entities.

| Aspect | Detail |
| --------- | --------- |
| **Input** | `merged_inventory.csv` + `netbox_mapping.yaml` |
| **Output** | Direct synchronization with NetBox via REST API |
| **Idempotency** | Identifies records by UUID (primary) or name (fallback) |
| **Dry-Run** | Supports `--dry-run` for inspection without writing |
| **Scope** | Operates against a **single Site** defined by `NETBOX_SITE_NAME` |

**Synchronized entities:**

- Sites, Cluster Types, Manufacturers, Device Types
- Platforms, Racks, Clusters, Device Roles
- Custom Fields, Interfaces, IP Addresses
- Automatic primary IPv4 assignment (when exactly one IP exists)

**Exit codes:**

- `0`: No errors at the row level.
- `1`: At least one row produced an error.

---

## Base Module

### `base_inventory.py`

Shared module that provides common utilities reused by the pipeline scripts (argument parsing, configuration file reading, transversal validations).
