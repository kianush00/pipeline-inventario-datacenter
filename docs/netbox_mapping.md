# Contrato de Mapeo NetBox (`netbox_mapping.yaml`)

Este documento describe en profundidad el contrato declarativo que gobierna la sincronización entre `merged_inventory.csv` y NetBox. El archivo [`netbox_mapping.yaml`](../netbox_mapping.yaml) es la **fuente única de verdad** para este mapeo.

---

## Estructura General del Archivo

El YAML está organizado en las siguientes secciones:

| Sección | Propósito |
| --------- | ----------- |
| `csv_columns` | Glosario de columnas del CSV consumidas por el exportador |
| `site` | Definición del Site único de NetBox |
| `cluster_type` | ClusterType por defecto para agrupación de VMs |
| `device_roles` | Lista canónica de roles de dispositivo |
| `custom_field_definitions` | Esquema DDL de los Custom Fields en NetBox |
| `node_types` | Mapeo DML de campos nativos y custom por tipo de nodo |
| `empty_values` | Valores que se consideran equivalentes a vacío/null |

---

## Definición de Columnas del CSV (`csv_columns`)

Cada entrada define una clave semántica usada por el script Python con los siguientes subcampos:

| Subcampo | Requerido | Descripción |
| ---------- | ----------- | ------------- |
| `source` | Sí | Nombre textual exacto de la columna en el CSV. Puede incluir un ancla YAML (`&col_id`) para reutilizarlo en secciones inferiores. |
| `required` | No | Si es `true`, la **presencia del encabezado** en el CSV es obligatoria. El script aborta si falta. No valida que el valor de cada celda esté lleno. |
| `map` | No | Diccionario de traducción de valores (ej. `"Dedicada" → "device"`). |

### Formato de Celdas Esperado

- **Campos simples (Strings, Números):** Valor directo. Se aplica `.strip()`.
- **Campos múltiples (Networking):** Lista de valores separados por coma (`,`). El script hace `split(",")` y asocia elementos por índice.
- **Valores vacíos:** Si el valor coincide con algún elemento de `empty_values`, se trata como `None` y no se envía a NetBox.

---

## Site (`site`)

Define el Site único contra el cual opera el exportador. Si no existe en NetBox, se crea automáticamente.

```yaml
site:
  name: "${NETBOX_SITE_NAME}"
  slug: "${NETBOX_SITE_NAME}"
```

El slug se genera automáticamente a partir del nombre para evitar errores con espacios y caracteres especiales.

---

## Cluster Type (`cluster_type`)

Las VMs se agrupan en Clusters cuyo nombre coincide con el valor del campo `Cluster` del CSV. El `cluster_type` se crea automáticamente si no existe.

```yaml
cluster_type:
  default:
    name: "Hipervisor"
    slug: "hipervisor"
```

---

## Roles de Dispositivo (`device_roles`)

Lista canónica de Device Roles. El valor de la columna `Rol` del CSV debe coincidir con alguno de estos nombres. Se crean automáticamente si no existen. Los colores son valores hexadecimales sin `#`.

Si el rol del CSV no coincide con ninguno de la lista, se asigna el rol `Others` como fallback.

---

## Custom Fields — Esquema DDL (`custom_field_definitions`)

Esta sección **define la estructura** de los Custom Fields en la base de datos de NetBox (creación si no existen). **No extrae datos del CSV.**

| Subcampo | Requerido | Descripción |
| ---------- | ----------- | ------------- |
| `name` | Sí | Nombre interno (slug) del custom field en NetBox |
| `label` | Sí | Nombre descriptivo mostrado en la interfaz web |
| `type` | Sí | Tipo de dato (ver tabla de tipos soportados) |
| `required` | Sí | Si es `true`, el campo es obligatorio en el esquema DDL de NetBox |
| `object_types` | Sí | Lista de modelos NetBox a los que aplica (ej. `dcim.device`) |
| `default` | No | Valor por defecto a nivel de base de datos |
| `choice_set` | No | Valores válidos. Obligatorio si `type` es `select` o `multiselect` |

### Tipos de Custom Field Soportados (NetBox 4.6+)

| Tipo | Descripción |
| ------ | ------------- |
| `text` | Texto libre |
| `longtext` | Texto largo (textarea) |
| `integer` | Número entero |
| `decimal` | Número decimal |
| `boolean` | Verdadero / Falso |
| `date` | Fecha (`YYYY-MM-DD`) |
| `datetime` | Fecha y hora |
| `url` | URL válida |
| `json` | JSON arbitrario |
| `select` | Selección única (requiere `choice_set`) |
| `multiselect` | Selección múltiple (requiere `choice_set`) |
| `object` | Referencia a otro objeto NetBox |
| `multiobject` | Referencia múltiple a objetos NetBox |

---

## Mapeo DML por Tipo de Nodo (`node_types`)

Define cómo **extraer** datos del CSV y **mapearlos** a campos de la API de NetBox. Se organiza por tipo de nodo (`device` y `virtual_machine`), cada uno con sus propios `native_mappings` y `custom_mappings`.

### Subcampos de cada Mapping

| Subcampo | Requerido | Descripción |
| ---------- | ----------- | ------------- |
| `source` | Sí | Nombre exacto de la columna en el CSV. Puede ser una lista para transformaciones. |
| `target` | Sí | Campo de destino en la API de NetBox |
| `required` | No | Si es `true`, el valor no puede estar vacío (fail-fast si falta) |
| `is_unique` | No | Si es `true`, un string vacío se sanitiza a `null` para evitar violaciones UNIQUE |
| `cast` | No | Función de conversión de tipo |
| `transform` | No | Operación de estructura (ej. `concat_dot`) |
| `map` | No | Diccionario de traducción declarado en el YAML |

### Funciones de Cast Disponibles

| Cast | Comportamiento |
| ------ | --------------- |
| `int` | Convierte a entero |
| `int_gb_to_mb` | Convierte GB (string) a MB (entero) |
| `bool_si_no` | Convierte "Sí"/"No" a `true`/`false` |
| `lower` | Convierte a minúsculas (usado para normalización de UUID) |

### Transformaciones

| Transform | Comportamiento |
|-----------|----------------|
| `concat_dot` | Concatena una lista de valores con separador `. ` (punto + espacio) |

### Campos NO incluidos en el Mapeo DML

Las claves foráneas como `site`, `role`, `platform`, `rack` y `device_type` son resueltas a objetos/IDs de NetBox por funciones especializadas en el script Python. No forman parte de `native_mappings`.

---

## Valores Vacíos (`empty_values`)

Lista de strings que se consideran equivalentes a `None`/vacío. Si el valor de una celda del CSV coincide exactamente con alguno de estos, se omite en la exportación:

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

## Credenciales

Las credenciales de NetBox se leen **exclusivamente** desde variables de entorno. **Nunca** deben almacenarse en este archivo:

| Variable | Descripción |
| ---------- | ------------- |
| `NETBOX_URL` | URL de la instancia de NetBox |
| `NETBOX_TOKEN` | Token con permisos write sobre `dcim`, `virtualization`, `ipam`, `extras`, `core` |
| `NETBOX_VERIFY_SSL` | `true` / `false` (por defecto: `true`) |
| `NETBOX_SITE_NAME` | Nombre del Site de NetBox |

Ver [Uso y Configuración](usage_and_config.md) para instrucciones completas de configuración.
