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
| `map` | No | Diccionario de traducción de valores puramente enfocado en normalizar el CSV hacia valores aceptados por NetBox (ej. `"Dedicada" → "dedicated"`). Se recomienda incluir mapeos de identidad (e.g., `"active" → "active"`) para soportar CSVs que ya contengan los valores nativos de NetBox en inglés. |
| `cluster_host_types` | No | Lista de strings (valores normalizados por el `map`, si existe) que indican que el dispositivo actúa como un hipervisor (host de cluster). |
| `virtual_machine_types` | No | Lista de strings (valores normalizados por el `map`, si existe) que indican que la fila corresponde a una Máquina Virtual en lugar de a un Servidor Físico. |

### Expected Cell Format

- **Simple fields (Strings, Numbers):** Direct value. `.strip()` is applied.
- **Multiple fields (Networking & `multiselect` Custom Fields):** List of values separated by commas (`,`).
- **Empty values:** If the value matches any element in `empty_values`, it is treated as `None` and is not sent to NetBox.

### Defined Aliases Glossary (`csv_columns`)

This table acts as a data dictionary of the fields the script expects to extract. Example values apply to both physical and virtual cases.

| Internal Alias (`key`) | Typical Source (`source`) | Description (Business Semantics) | Example Values |
| ---------------------- | ------------------------- | -------------------------------- | -------------- |
| `machine_name` | `Nombre maquina` | Hostname or main label of the device/VM. Required. | `srv-web-01`, `db-prod-sql` |
| `machine_type` | `Tipo de maquina` | Defines if the node is a physical server, hypervisor, or virtual machine. | `Dedicada`, `VM`, `Hipervisor` |
| `role` | `Rol` | Network function or role of the device in the infrastructure. | `Web Server`, `Database Server` |
| `desc` | `Descripcion` | Brief free text to describe the node. | `Primary DB server` |
| `cluster_name` | `Cluster` | Name of the cluster the node belongs to. | `Cluster-VMware-01`, `KVM-Pool-B` |
| `host_device` | `Dispositivo Host` | (For VMs) Name of the physical server running the VM. | `hyper-node-05` |
| `rack` | `Rack` | Name of the physical rack where it is located. | `Rack-A1`, `Fila-2-R4` |
| `pos_u` | `Posicion (U)` | Physical position or lowest bay number within the rack. | `12`, `25` |
| `alt_u` | `Altura (U)` | Total U height the device occupies in the rack (usually 1, 2, or fractional). | `1.0`, `1.5`, `2.0` |
| `os` | `SO Host` | Base operating system of the node. | `Ubuntu`, `Windows Server 2022` |
| `os_ver` | `Version SO Host` | Specific OS version. | `22.04 LTS`, `2019 Standard` |
| `hypervisor_os` | `SO Hipervisor` | Base hypervisor operating system (e.g. ESXi, Proxmox). | `VMware ESXi`, `Proxmox VE` |
| `hypervisor_ver` | `Version SO Hipervisor` | Hypervisor OS build or version. | `7.0.3`, `8.1.1` |
| `iface_names` | `Interfaces` | Comma-separated list of network interface names. | `eth0, eth1`, `vmnic0` |
| `iface_status` | `Interfaces estado` | Comma-separated list of link statuses for each interface. | `up, down` |
| `iface_ip` | `IP` | List of associated IP addresses (primary and secondaries). | `10.0.0.10, 192.168.1.5` |
| `iface_pfx` | `Red IP` | List of CIDR prefixes or subnets associated with each IP. | `192.168.1.0/26` |
| `iface_mac` | `MAC` | List of MAC addresses corresponding to the interfaces. | `aa:bb:cc:dd:ee:ff` |
| `cpu_model` | `CPU Modelo` | Commercial name of the processor. | `Intel Xeon Gold 6230` |
| `cores` | `Cores` | Number of physical (Device) or virtual (VM) cores. | `16`, `32` |
| `threads` | `Threads` | Total number of threads of the physical CPU. | `32`, `64` |
| `sockets` | `Sockets` | Number of sockets or physical CPUs mounted on the motherboard. | `2`, `4` |
| `ram_gb` | `RAM (GB)` | Total RAM memory expressed in Gigabytes. | `128`, `256` |
| `disks` | `Discos` | Raw breakdown of the disk configuration. | `2x SSD 512GB, 4x HDD 4TB` |
| `disk_cap` | `Capacidad visible (GB)` | Total visible storage capacity. | `1024`, `500` |
| `raid` | `RAID` | Configured RAID level. | `RAID 1`, `RAID 5` |
| `manufacturer` | `Marca` | Hardware brand or manufacturer. | `Dell`, `HP`, `Cisco` |
| `model` | `Modelo` | Specific hardware model. | `PowerEdge R740`, `ProLiant DL380` |
| `serial` | `Serial Number` | Physical serial number for general hardware (non-Dell). | `ABC12345` |
| `service_tag` | `Service Tag` | Conventionally reserved for the Service Tag of Dell equipment. | `ST-442-XY` |
| `asset_tag` | `Nro Inventario` | Internal inventory plate or number. | `INV-9876` |
| `inventory_uuid` | `UUID` | Unique and deterministic identifier of the node (ideally extracted from DMI or virtual system). | `564d...e2f1` |
| `bios_ver` | `Version BIOS` | Current BIOS/UEFI firmware version. | `2.14.0` |
| `bios_date` | `Fecha BIOS` | Release or update date of the BIOS. | `2023-01-15` |
| `status` | `Estado` | Lifecycle status (active, offline, decommissioning). | `Activo`, `offline`, `En baja` |
| `environment` | `Entorno` | Deployment environment (Production, QA, Development). | `Producción`, `Desarrollo` |
| `rundeck_node` | `Nodo Rundeck` | Indicates if the node was discovered via Rundeck (boolean). | `Sí`, `No` |
| `notes` | `Notas` | Extensive notes or additional audit metadata (flexible placement). | `Replace disks in Q3` |

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
| `float` | Converts to float (decimal) |
| `int_gb_to_mb` | Converts GB (string) to MB (integer) |
| `bool_si_no` | Converts "Sí"/"No" to `true`/`false` |
| `lower` | Converts to lowercase (used for UUID normalization) |

### Transformations

| Transform | Behavior |
| ----------- | ---------------- |
| `concat_dot` | Concatenates a list of values with the separator `.` (dot + space) |

### Fields NOT included in the DML Mapping

Foreign keys like `site`, `role`, `platform`, `rack`, and `device_type` are resolved to NetBox objects/IDs by specialized functions in the Python script. They are not part of `native_mappings`.

---

## Empty Values (`empty_values`)

List of strings that are considered equivalent to `None`/empty. If a CSV cell value matches one of these (case-insensitive), it is omitted in the export:

```yaml
empty_values:
  - "N/A"
  - ""
  - "None"
  - "Not Settable"
  - "Not Applicable"
  - "Not Available"
  - "Unknown"
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
