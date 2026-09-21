# Componentes del Pipeline

Este documento describe cada script del pipeline, su propósito, entradas, salidas y dependencias.

---

## Recolección Rundeck

### `asset_information.sh`

Script Bash ejecutado en cada nodo objetivo por Rundeck. Recolecta:

- Identidad de la máquina (UUID, hostname, serial)
- Sistema operativo y versión
- Tipo de virtualización
- Interfaces de red (nombre, estado, IP, máscara, MAC)
- CPU (modelo, cores, threads, sockets)
- Memoria RAM
- BIOS (versión, fecha)
- DMI y metadatos de hardware
- Almacenamiento (discos, RAID, capacidad)

Su salida está destinada a ser guardada como el resultado de un job de Rundeck (archivo `.log`).

---

## Contrato de Esquema

### `rundeck_header_list.txt`

Define el contrato de columnas para la recolección, el parseo, la preparación del maestro y el merge. Cada entrada utiliza el formato:

```text
COLUMN_NAME|FLAG
```

Donde `FLAG` controla el comportamiento de merge:

- `0`: Preservar el valor maestro sin sobrescribir.
- `1`: Permitir actualización con valores no vacíos del parseo.
- `2`: Llave de merge (debe existir exactamente una).

Ver [Arquitectura](architecture.md) para más detalle sobre las reglas de merge.

---

## Parser de Salida

### `parse_job_output.py`

Convierte la salida cruda de Rundeck en un CSV normalizado.

| Aspecto | Detalle |
| --------- | --------- |
| **Entrada** | Archivo `.log` de salida de Rundeck |
| **Salida** | `parsed_job_output.csv` |
| **Validaciones** | Valida claves contra `rundeck_header_list.txt`, rechaza campos malformados o duplicados |
| **Valores faltantes** | Se completan con `N/A` |
| **Líneas inválidas** | Se ignoran silenciosamente |

---

## Preparación del Maestro

### `prepare_master_inventory.py`

Acepta una hoja de cálculo maestra (ODS o XLSX) y la convierte a un CSV limpio para la etapa de merge.

| Aspecto | Detalle |
| --------- | --------- |
| **Entrada** | Hoja de cálculo maestra (`.ods` / `.xlsx`) |
| **Salida** | `prepared_master_inventory.csv` |
| **Dependencia externa** | LibreOffice (para conversión ODS/XLSX → CSV) |
| **Comportamiento** | Localiza y valida el encabezado real, preserva columnas adicionales |

---

## Consolidación (Merge)

### `merge_inventories.py`

Superpone el inventario parseado sobre el maestro preparado usando la columna marcada con flag `2` (UUID).

| Aspecto | Detalle |
| --------- | --------- |
| **Entradas** | `parsed_job_output.csv` + `prepared_master_inventory.csv` |
| **Salida** | `merged_inventory.csv` |
| **Preservación** | Mantiene todas las filas y columnas extra del maestro |
| **Actualización** | Solo actualiza campos con flag `1` |
| **Seguridad** | Nunca sobrescribe la llave de merge. Excluye filas con llaves inválidas o duplicadas |

---

## Actualización del Maestro (Opcional)

### `update_master_inventory.py`

Escribe los valores consolidados de vuelta a una copia de la hoja de cálculo maestra original.

| Aspecto | Detalle |
| --------- | --------- |
| **Entrada** | `merged_inventory.csv` + hoja maestra original |
| **Salida** | Copia actualizada de la hoja maestra (misma extensión) |
| **Seguridad** | La hoja maestra original **nunca** se modifica |
| **Dependencia** | LibreOffice (para archivos ODS) |

---

## Exportación a NetBox

### `export_to_netbox.py`

Etapa ETL downstream. Lee el CSV consolidado y crea o actualiza Devices y Virtual Machines en NetBox, junto con todas las entidades dependientes.

| Aspecto | Detalle |
| --------- | --------- |
| **Entrada** | `merged_inventory.csv` + `netbox_mapping.yaml` |
| **Salida** | Sincronización directa con NetBox vía API REST |
| **Idempotencia** | Identifica registros por UUID (primario) o nombre (fallback) |
| **Dry-Run** | Soporta `--dry-run` para inspección sin escritura |
| **Scope** | Opera contra un **único Site** definido por `NETBOX_SITE_NAME` |

**Entidades sincronizadas:**

- Sites, Cluster Types, Manufacturers, Device Types
- Platforms, Racks, Clusters, Device Roles
- Custom Fields, Interfaces, Direcciones IP
- Asignación automática de IPv4 primaria (cuando existe exactamente una IP)

**Códigos de salida:**

- `0`: Sin errores a nivel de fila.
- `1`: Al menos una fila produjo un error.

---

## Módulo Base

### `base_inventory.py`

Módulo compartido que provee utilidades comunes reutilizadas por los scripts del pipeline (parseo de argumentos, lectura de archivos de configuración, validaciones transversales).
