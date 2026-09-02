---
description: Reglas y contexto global del proyecto Datacenter Inventory Pipeline
trigger: always_on
---

# Contexto del Proyecto
Estás trabajando en el **Datacenter Inventory Pipeline**, una canalización ETL (Extract, Transform, Load) automatizada e idempotente. Su propósito es recolectar, consolidar y sincronizar infraestructura de servidores físicos y máquinas virtuales desde Rundeck y hojas de cálculo maestras (CSV/ODS/XLSX) directamente hacia **NetBox v4.6+**.

El flujo consta de:
1. Colección de datos (Rundeck -> `asset_information.sh`).
2. Parseo (`parse_job_output.py`).
3. Preparación del maestro (`prepare_master_inventory.py`).
4. Mezcla/Consolidación usando un UUID único como llave (`merge_inventories.py`).
5. Exportación idempotente hacia NetBox mediante la API REST (`export_to_netbox.py` y `netbox_mapping.yaml`).

# Estándares de Ingeniería de Software

Al asistir en este proyecto, DEBES adherirte estrictamente a los siguientes principios de ingeniería de software a nivel profesional:

## 1. Tipado Fuerte y Estático (Python)
- Todo el código Python debe estar fuertemente tipado utilizando las anotaciones del módulo `typing` nativo.
- Evita a toda costa el uso injustificado de `Any`.
- Utiliza `TypeAlias`, `TypedDict`, y Modelos Pydantic (`BaseModel`) para definir estructuras de datos complejas o payloads de APIs, garantizando una semántica clara.
- Asegúrate de que el código base esté diseñado para pasar verificadores de tipo estáticos.

## 2. Calidad de Código y Semántica (Clean Code)
- Prioriza la legibilidad, la intención y la claridad sobre la brevedad excesiva.
- Nombra variables, funciones y clases de forma descriptiva, revelando su intención en el modelo de dominio (ej. `SyncStatus`, `NetBoxPayload`).
- Sigue el principio de Responsabilidad Única (SRP): las funciones deben hacer una sola cosa y hacerla bien.
- Mantén un manejo de errores robusto. Nunca falles silenciosamente; utiliza logs (`logging`) detallados con contexto y niveles adecuados (INFO, WARNING, ERROR).

## 3. Escalabilidad y Eficiencia
- Diseña algoritmos y estructuras de datos asumiendo que el pipeline procesará **miles de nodos/filas**.
- Evita operaciones de coste cuadrático iterativas. Usa diccionarios y conjuntos (`sets`) para búsquedas en memoria con coste algorítmico constante.
- Implementa estrategias de "Fail-Fast" y "Early Returns" para reducir la anidación del código y mejorar la legibilidad.

## 4. Diseño Idempotente
- Los scripts que modifiquen bases de datos externas (como NetBox) o archivos maestros deben ser estrictamente **idempotentes**. Ejecutar el pipeline múltiples veces con la misma entrada no debe generar duplicados ni alterar el estado esperado.
- Respeta la bandera `--dry-run` para todas las operaciones destructivas o de escritura.

## 5. Documentación y Mantenibilidad
- Toda clase, módulo o función crítica debe contar con Docstrings explicativos.
- No documentes "qué" hace el código línea por línea si es obvio, documenta el "por qué" de las decisiones de negocio.
- Deja los comentarios y mensajes de usuario/logs en español (tal como el resto del proyecto), pero mantén el código fuente (variables, funciones, clases) en inglés.
