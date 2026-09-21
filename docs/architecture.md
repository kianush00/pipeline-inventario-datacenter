# Arquitectura y Flujo de Datos

Este documento describe la arquitectura general del **Datacenter Inventory Pipeline**, el modelo de datos que lo sustenta y las decisiones de diseño detrás de cada etapa.

---

## Visión General

El pipeline sigue una secuencia ETL (Extract, Transform, Load) lineal y determinista:

```text
Rundeck (Nodos) → Shell Script → Log → Parser → CSV Parseado
                                                     ↓
                        Hoja Maestra (ODS/XLSX) → CSV Preparado
                                                     ↓
                                              CSV Consolidado (Merge)
                                                     ↓
                                                  NetBox (API)
```

<img src="./assets/inventory_pipeline.png" alt="Datacenter inventory pipeline" width="100%">

El archivo fuente del diagrama (`inventory_pipeline.drawio`) se encuentra en `docs/assets/`.

---

## Modelo de Cruce por UUID

El **UUID de inventario** (`inventory_uuid`) es la llave maestra que conecta todas las etapas del pipeline. Se extrae directamente del hardware/BIOS del nodo mediante el script de recolección Bash y se mantiene intacto a lo largo de todo el flujo.

### Reglas de Merge (Consolidación)

El archivo [`rundeck_header_list.txt`](../rundeck_header_list.txt) define el contrato de columnas y las reglas de actualización mediante flags numéricos:

| Flag | Comportamiento | Ejemplo |
| ------ | --------------- | --------- |
| `0` | **Preservar** el valor maestro. Nunca se sobrescribe. | Columnas manuales (Rack, Rol) |
| `1` | **Actualizar** si el valor parseado no está vacío. | Datos dinámicos (SO, CPU, RAM) |
| `2` | **Llave de merge**. Debe existir exactamente una. | `UUID` |

El merge key es actualmente el UUID de máquina. Los parsers resuelven columnas por nombre, por lo que el orden de columnas no necesita coincidir.

### Normalización del UUID

Para garantizar la idempotencia de la sincronización contra NetBox, el pipeline normaliza todos los UUID a **lowercase** antes de cualquier comparación o envío a la API. Esto evita duplicados causados por diferencias de capitalización entre fuentes.

---

## Idempotencia

El pipeline está diseñado para ser ejecutado repetidamente con la misma entrada sin generar efectos secundarios:

- **Merge**: Las filas con llaves inválidas o duplicadas se excluyen del merge, preservando la integridad del maestro.
- **NetBox Export**: La sincronización identifica registros existentes por UUID (primario) o por nombre (fallback). Solo se actualizan campos que realmente cambiaron. Los registros sin cambios se omiten silenciosamente.
- **Dry-Run**: Todas las operaciones destructivas soportan `--dry-run` para inspección previa.

---

## Flujo de Sincronización con NetBox

El exportador sigue esta lógica para cada fila del CSV:

1. **Resolución de identidad**: Busca en NetBox por `inventory_uuid`. Si no encuentra, busca por nombre de máquina.
2. **Validación de unicidad**: Si la coincidencia es solo por nombre, verifica que el nombre sea único tanto en el CSV como en NetBox.
3. **Construcción de payload**: Los campos se mapean según el contrato definido en `netbox_mapping.yaml`, aplicando casteos y transformaciones.
4. **Detección de cambios**: Compara el payload contra el estado actual del registro. Solo emite una actualización si hay diferencias reales.
5. **Sincronización de dependencias**: Sites, Manufacturers, Device Types, Platforms, Roles, Clusters, Custom Fields, Interfaces e IPs se crean o resuelven como prerequisitos.
6. **Asignación de IP primaria**: Si un Device o VM posee exactamente una IP válida, se asigna automáticamente como IPv4 primaria.
