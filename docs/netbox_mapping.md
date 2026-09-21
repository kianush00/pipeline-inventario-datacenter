# NetBox Mapping Contract (`netbox_mapping.yaml`)

This document describes in depth the declarative contract that governs the synchronization between `merged_inventory.csv` and NetBox. The [`netbox_mapping.yaml`](../netbox_mapping.yaml) file is the **single source of truth** for this mapping.

---

## General File Structure

The YAML is organized into the following sections:

| Section | Purpose |
| --------- | ----------- |
| `csv_columns` | Glossary of CSV columns consumed by the exporter |
| `site` | Definition of the single NetBox Site |
| `cluster_type` | Default ClusterType for VM grouping |
| `device_roles` | Canonical list of device roles |
| `custom_field_definitions` | DDL schema of the Custom Fields in NetBox |
| `node_types` | DML mapping of native and custom fields by node type |
| `empty_values` | Values considered equivalent to empty/null |

---

## CSV Column Definition (`csv_columns`)

Each entry defines a semantic key used by the Python script with the following subfields:

| Subfield | Required | Description |
| ---------- | ----------- | ------------- |
| `source` | Yes | Exact text name of the column in the CSV. Can include a YAML anchor (`&col_id`) to reuse it in lower sections. |
| `required` | No | If `true`, the **presence of the header** in the CSV is mandatory. The script aborts if it is missing. Does not validate that the cell value is filled. |
| `map` | No | Value translation dictionary (e.g., `"Dedicada" → "device"`). |

### Expected Cell Format

- **Simple fields (Strings, Numbers):** Direct value. `.strip()` is applied.
- **Multiple fields (Networking):** List of values separated by commas (`,`). The script performs a `split(",")` and associates elements by index.
- **Empty values:** If the value matches any element in `empty_values`, it is treated as `None` and is not sent to NetBox.

---

## Site (`site`)

Defines the single Site against which the exporter operates. If it does not exist in NetBox, it is created automatically.

```yaml
site:
  name: "${NETBOX_SITE_NAME}"
  slug: "${NETBOX_SITE_NAME}"
```

The slug is automatically generated from the name to avoid errors with spaces and special characters.

---

## Cluster Type (`cluster_type`)

VMs are grouped into Clusters whose name matches the value of the `Cluster` field in the CSV. The `cluster_type` is created automatically if it does not exist.

```yaml
cluster_type:
  default:
    name: "Hipervisor"
    slug: "hipervisor"
```

---

## Device Roles (`device_roles`)

Canonical list of Device Roles. The value of the `Rol` column in the CSV must match one of these names. They are created automatically if they do not exist. The colors are hex values without `#`.

If the CSV role does not match any in the list, the `Others` role is assigned as a fallback.

---

## Custom Fields — DDL Schema (`custom_field_definitions`)

This section **defines the structure** of the Custom Fields in the NetBox database (creation if they do not exist). **It does not extract data from the CSV.**

| Subfield | Required | Description |
| ---------- | ----------- | ------------- |
| `name` | Yes | Internal name (slug) of the custom field in NetBox |
| `label` | Yes | Descriptive name shown in the web interface |
| `type` | Yes | Data type (see supported types table) |
| `required` | Yes | If `true`, the field is mandatory in the NetBox DDL schema |
| `object_types` | Yes | List of NetBox models it applies to (e.g., `dcim.device`) |
| `default` | No | Default value at the database level |
| `choice_set` | No | Valid values. Mandatory if `type` is `select` or `multiselect` |

### Supported Custom Field Types (NetBox 4.6+)

| Type | Description |
| ------ | ------------- |
| `text` | Free text |
| `longtext` | Long text (textarea) |
| `integer` | Integer number |
| `decimal` | Decimal number |
| `boolean` | True / False |
| `date` | Date (`YYYY-MM-DD`) |
| `datetime` | Date and time |
| `url` | Valid URL |
| `json` | Arbitrary JSON |
| `select` | Single selection (requires `choice_set`) |
| `multiselect` | Multiple selection (requires `choice_set`) |
| `object` | Reference to another NetBox object |
| `multiobject` | Multiple reference to NetBox objects |

---

## DML Mapping by Node Type (`node_types`)

Defines how to **extract** data from the CSV and **map** it to fields in the NetBox API. It is organized by node type (`device` and `virtual_machine`), each with its own `native_mappings` and `custom_mappings`.

### Subfields of each Mapping

| Subfield | Required | Description |
| ---------- | ----------- | ------------- |
| `source` | Yes | Exact name of the column in the CSV. Can be a list for transformations. |
| `target` | Yes | Target field in the NetBox API |
| `required` | No | If `true`, the value cannot be empty (fail-fast if missing) |
| `is_unique` | No | If `true`, an empty string is sanitized to `null` to avoid UNIQUE violations |
| `cast` | No | Type conversion function |
| `transform` | No | Structure operation (e.g., `concat_dot`) |
| `map` | No | Translation dictionary declared in the YAML |

### Available Cast Functions

| Cast | Behavior |
| ------ | --------------- |
| `int` | Converts to integer |
| `int_gb_to_mb` | Converts GB (string) to MB (integer) |
| `bool_si_no` | Converts "Sí"/"No" to `true`/`false` |
| `lower` | Converts to lowercase (used for UUID normalization) |

### Transformations

| Transform | Behavior |
|-----------|----------------|
| `concat_dot` | Concatenates a list of values with the separator `. ` (dot + space) |

### Fields NOT included in the DML Mapping

Foreign keys like `site`, `role`, `platform`, `rack`, and `device_type` are resolved to NetBox objects/IDs by specialized functions in the Python script. They are not part of `native_mappings`.

---

## Empty Values (`empty_values`)

List of strings that are considered equivalent to `None`/empty. If a CSV cell value matches exactly one of these, it is omitted in the export:

```yaml
empty_values:
  - "N/A"
  - ""
  - "None"
  - "n/a"
  - "none"
  - "-----"
  - "------"
  - "-------"
```

---

## Credentials

NetBox credentials are read **exclusively** from environment variables. They should **never** be stored in this file:

| Variable | Description |
| ---------- | ------------- |
| `NETBOX_URL` | URL of the NetBox instance |
| `NETBOX_TOKEN` | Token with write permissions on `dcim`, `virtualization`, `ipam`, `extras`, `core` |
| `NETBOX_VERIFY_SSL` | `true` / `false` (default: `true`) |
| `NETBOX_SITE_NAME` | NetBox Site name |

See [Usage and Configuration](usage_and_config.md) for complete configuration instructions.
