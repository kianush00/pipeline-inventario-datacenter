---
description: Reglas y contexto global del proyecto Datacenter Inventory Pipeline
trigger: always_on
---

# Contexto del Proyecto

**Datacenter Inventory Pipeline**: ETL idempotente que recolecta servidores físicos y VMs desde Rundeck y hojas de cálculo maestras (CSV/ODS/XLSX) y los sincroniza con **NetBox v4.6+**.

Flujo:

1. Colección (Rundeck -> `collectors/asset_information.sh`).
2. Parseo (`parse_job_output.py`).
3. Preparación del maestro (`prepare_master_inventory.py`).
4. Consolidación usando un UUID único como llave (`merge_inventories.py`).
5. Exportación idempotente a NetBox vía API REST (`export_to_netbox.py` + `config/netbox_mapping.yaml`).

Los módulos Python viven en `src/inventory_pipeline/` (src layout, `pip install -e .`). El orquestador `run_pipeline.py` (comando `inventory-pipeline`) ejecuta las etapas 2 a 5 con fail-fast, lee de `input/` y escribe en `output/`. Las rutas por defecto se centralizan en `paths.py`. La arquitectura y los contratos de datos están documentados en `docs/`.

## 1. Tipado

- El código (incluidos los tests) debe pasar Pyright sin advertencias. `Any` solo cuando no exista alternativa razonable.
- Modela estructuras y payloads con las construcciones que ya usa el proyecto (`NamedTuple`, `TypedDict`, `dataclass`, modelos Pydantic). Reutiliza los tipos ya declarados antes de crear nuevos.

## 2. Diseño

- **Sin side-effects ocultos:** no mutes argumentos recibidos (cachés, dicts, listas). Si una función produce estado nuevo, lo retorna explícitamente.
- **Consistencia:** replica los patrones existentes del módulo (manejo de excepciones, estructura de control, utilidades). Si una transformación, parseo o validación se repite, extráela a una utilidad reutilizable en vez de duplicarla.
- **Errores:** nunca falles en silencio; registra con `logging`, con contexto y el nivel adecuado.
- **Escala:** asume miles de nodos/filas. Evita costes cuadráticos; usa dicts/sets para búsquedas. Prefiere fail-fast y early returns.

## 3. Idempotencia

- Todo lo que escriba en NetBox o en archivos maestros debe ser idempotente: re-ejecutar con la misma entrada no genera duplicados ni cambios de estado.
- Toda operación de escritura respeta `--dry-run`.

## 4. Idioma y Documentación

- Identificadores y documentación técnica (`docs/*.md`) en inglés; comentarios, logs y mensajes al usuario en español.
- Docstrings en módulos, clases y funciones críticas, explicando el "por qué" de las decisiones de negocio.
- Si un cambio afecta comportamiento, arquitectura o contratos (p. ej. `netbox_mapping.yaml`), actualiza `docs/` en el mismo cambio.

## 5. Comunicación

Directo y técnico, sin preámbulos ni elogios.

## 6. Verificación

Antes de dar un cambio por terminado, ejecuta y deja limpio lo siguiente sobre los archivos modificados (en Windows, los binarios están en `.venv\Scripts\`):

- **Python:** `.venv/bin/pyright <archivo>`, `.venv/bin/ruff check <archivo> --fix`, `.venv/bin/ruff format <archivo>`.
- **Bash (`collectors/*.sh`):** `bash -n <script>` y `shellcheck <script>`. Suprime falsos positivos solo con `# shellcheck disable=SCxxxx` y un comentario que lo justifique. Los `.sh` deben quedar con finales de línea LF.
- **Pipeline:** si hay datos de prueba disponibles, valida con `--dry-run`.
- **Tests:** `.venv/bin/pytest tests/` al final, siempre.

## 7. Tests

- Cubre el código nuevo y revisa los tests existentes que toca el cambio: actualízalos si su contrato cambió o si quedaron ciegos a nuevos edge cases.
- No añadas tests redundantes con escenarios ya cubiertos.
- Todos los `import` van al inicio del archivo de test, nunca dentro de funciones.

## 8. Revisión Obligatoria (Skills)

Si el cambio toca lógica de parseo, `netbox_mapping.yaml`, la interacción con la API en `export_to_netbox.py`, o supera ~30 líneas modificadas, no lo des por terminado sin pasar por:

1. `qa-tester` (`.agents/skills/qa-tester/SKILL.md`).
2. `code-reviewer` (`.agents/skills/code-reviewer/SKILL.md`), sobre el diff final.

El reporte final debe incluir un resumen de los hallazgos de ambas revisiones.
