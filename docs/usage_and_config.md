# Uso y Configuración

Este documento describe cómo configurar el entorno, las variables requeridas y cómo ejecutar cada etapa del pipeline.

---

## Requisitos Previos

- Python 3.10 o superior
- `pip`
- LibreOffice disponible como `libreoffice` o `soffice` en `PATH` (para conversión ODS/XLSX y actualizaciones ODS)
- NetBox 4.6 o superior (para la etapa de exportación)

Las dependencias Python están fijadas en [`requirements.txt`](../requirements.txt). El exportador NetBox usa `pynetbox`, `PyYAML`, `requests` y `urllib3`; el procesamiento de hojas de cálculo usa `openpyxl`.

---

## Configuración del Entorno Virtual

Crear y activar un entorno virtual local desde la raíz del repositorio:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Activar el entorno nuevamente cuando se abra una nueva terminal:

```bash
source .venv/bin/activate
```

El prompt de la terminal normalmente muestra `(.venv)` mientras el entorno está activo. Para desactivarlo:

```bash
deactivate
```

---

## Configuración de NetBox

El exportador requiere las siguientes variables de entorno:

```bash
export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="<write-enabled-token>"
export NETBOX_VERIFY_SSL="true"
```

El `netbox_mapping.yaml` por defecto espera el nombre del Site vía entorno:

```bash
export NETBOX_SITE_NAME="Datacenter Principal"
```

> Si hardcodeas el nombre del site en el YAML, `NETBOX_SITE_NAME` deja de ser requerido.

### Notas de Seguridad

- `NETBOX_VERIFY_SSL` vale `true` por defecto; establécelo a `false` solo cuando el despliegue explícitamente lo requiera.
- El token debe tener permisos de escritura para las áreas de NetBox usadas por el exportador: `dcim`, `virtualization`, `ipam`, `extras` y `core`.
- **Nunca** almacenes `NETBOX_TOKEN` en `netbox_mapping.yaml`, control de versiones, historial de comandos ni archivos de inventario generados.

---

## Ejecución del Pipeline

Ejecutar las etapas desde la raíz del repositorio con el entorno virtual activo:

```bash
# 1. Parsear la salida del job de Rundeck
python parse_job_output.py job_output.log parsed_job_output.csv

# 2. Preparar la hoja de cálculo maestra
python prepare_master_inventory.py master_inventory.ods prepared_master_inventory.csv

# 3. Consolidar el inventario parseado con el maestro
python merge_inventories.py parsed_job_output.csv prepared_master_inventory.csv merged_inventory.csv

# 4. (Opcional) Actualizar una copia de la hoja maestra
python update_master_inventory.py merged_inventory.csv master_inventory.ods master_inventory_updated.ods

# 5. Vista previa de la sincronización con NetBox
python export_to_netbox.py merged_inventory.csv --dry-run

# 6. Aplicar la sincronización a NetBox
python export_to_netbox.py merged_inventory.csv
```

### Opciones Adicionales del Exportador

Cada script soporta rutas opcionales para sus archivos de entrada y salida según se describe en su help text. La ruta del mapping es opcional; por defecto, `export_to_netbox.py` carga `netbox_mapping.yaml` desde el mismo directorio del script. Se puede proveer un mapping personalizado como segundo argumento posicional:

```bash
python export_to_netbox.py merged_inventory.csv custom_netbox_mapping.yaml --dry-run
```

Usar `--verbose` para habilitar logging DEBUG:

```bash
python export_to_netbox.py merged_inventory.csv --dry-run --verbose
```
