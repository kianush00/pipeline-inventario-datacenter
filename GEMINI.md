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

Al asistir en este proyecto, DEBES adherirte estrictamente a los siguientes principios de ingeniería de software a nivel profesional:

## 1. Tipado Fuerte y Estático (Python)

- Todo el código Python debe estar fuertemente tipado utilizando las anotaciones del módulo `typing` nativo.
- Evita a toda costa el uso injustificado de `Any`.
- Utiliza elementos como `TypeAlias`, `TypedDict`, `dataclasses` y Modelos Pydantic (`BaseModel`) para definir estructuras de datos complejas o payloads de APIs, garantizando una semántica clara.
- **Reutilización de tipos existentes:** Antes de importar o definir nuevos tipos de datos, evalúa y prioriza siempre el uso de clases ya declaradas en el archivo de trabajo. No introduzcas tipos redundantes o alternativos provenientes de librerías externas de forma innecesaria.
- Asegúrate de que el código base esté diseñado para pasar verificadores de tipo estáticos sin advertencias (Pylance/MyPy/Pyright).

## 2. Calidad de Código, Semántica y Consistencia Arquitectónica (Clean Code)

- Prioriza la legibilidad, la intención y la robustez profesional sobre la brevedad o soluciones rápidas.
- **Cero atajos o parches forzados:** Toda modificación debe ser limpia, cuidadosa y respetar estrictamente los patrones de diseño y convenciones ya establecidas en el código base.
- **Reutilización y creación de utilidades:** Prohibido duplicar lógica ad-hoc. Aprovecha siempre las funciones auxiliares (utilidades) existentes en el proyecto. Si introduces código nuevo que resuelva transformaciones, parseos o validaciones comunes no cubiertas actualmente, abstáelo en una nueva función auxiliar reutilizable siguiendo el diseño de los módulos de soporte existentes.
- Nombra variables, funciones y clases de forma descriptiva, revelando su intención en el modelo de dominio.
- **Principio de Responsabilidad Única (SRP)**: las funciones deben hacer una sola cosa y hacerla bien.
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

## 6. Comunicación y Formato de Respuestas

- **Tono directo y profesional:** Ve directo al grano sin preámbulos, saludos, cortesías innecesarias ni felicitaciones (ej. "¡Buena pregunta!", "Excelente código", "Buena intuición", "Excelente observación", etc.).
- **Enfoque en la solución técnica:** Proporciona explicaciones técnicas concisas, fundamentadas, precisas y profesionales.
- **Diffs limpios y contextualizados:** Entrega los bloques de código modificados listos para integrarse sin placeholders ambiguos, asegurando total coherencia con el resto del script.

## 7. Verificación de Calidad, Tipos y Linting Continuo

Antes de dar cualquier modificación por concluida, **como agente DEBES verificar y corregir autónomamente** el código modificado ejecutando las siguientes herramientas desde el entorno virtual:

1. **Tipado Estático (Pyright / Pyrefly):**
   - Linux/macOS: `.venv/bin/pyright <archivo_modificado>`
   - Windows: `.\.venv\Scripts\pyright <archivo_modificado>`
   - Corrige cualquier discrepancia de tipos (`reportGeneralTypeIssues`, argumentos faltantes o `Any` injustificado).

2. **Linting y Estándares de Código (Ruff):**
   - Ejecuta el linter para detectar errores semánticos, imports en desuso o violaciones de reglas alineadas con SonarQube:
     - Linux/macOS: `.venv/bin/ruff check <archivo_modificado> --fix`
     - Windows: `.\.venv\Scripts\ruff check <archivo_modificado> --fix`

3. **Formateo y Consistencia (Ruff Format):**
   - Garantiza que la indentación, saltos de línea y longitud cumplan con el estándar PEP 8:
     - Linux/macOS: `.venv/bin/ruff format <archivo_modificado>`
     - Windows: `.\.venv\Scripts\ruff format <archivo_modificado>`

4. **Validación en Seco (Dry-Run / Syntax Check):**
   - Para cambios en `export_to_netbox.py` o scripts del pipeline, ejecuta un chequeo de sintaxis rápida:
     - Linux/macOS: `.venv/bin/python3 -m py_compile <archivo_modificado>`
     - Windows: `.\.venv\Scripts\python -m py_compile <archivo_modificado>`
   - Si se cuenta con datos de prueba o fixtures, verifica la consistencia con el flag `--dry-run` antes de confirmar la solución.
