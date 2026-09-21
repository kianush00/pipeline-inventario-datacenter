# Desarrollo y Testing

Este documento describe cómo contribuir al proyecto, ejecutar pruebas, y las herramientas de calidad de código utilizadas.

---

## Ejecutar Pruebas

El repositorio incluye una suite de pruebas unitarias exhaustiva que valida la lógica, los edge cases y las interacciones con la API (mediante mocks). Para ejecutar las pruebas:

```bash
pytest tests/ -v
```

Antes de hacer commit de código nuevo o desplegar cambios, asegúrate de que todas las pruebas pasen exitosamente.

---

## Herramientas de Calidad de Código

### Tipado Estático (Pyright)

```bash
.venv/bin/pyright <archivo>
```

Verifica anotaciones de tipo, argumentos faltantes y uso injustificado de `Any`.

### Linting (Ruff)

```bash
# Detectar y corregir errores automáticamente
.venv/bin/ruff check <archivo> --fix

# Formatear según PEP 8
.venv/bin/ruff format <archivo>
```

### Validación de Sintaxis

```bash
.venv/bin/python3 -m py_compile <archivo>
```

---

## Convenciones del Código

- **Tipado fuerte:** Todo el código Python usa anotaciones del módulo `typing`. Se evita `Any` injustificado.
- **Código fuente en inglés:** Variables, funciones y clases en inglés. Comentarios, logs y mensajes de usuario en español.
- **Principio de Responsabilidad Única (SRP):** Las funciones deben hacer una sola cosa y hacerla bien.
- **Early Returns / Fail-Fast:** Se reduce la anidación devolviendo tempranamente en los casos de error.
- **Idempotencia:** Los scripts que modifican sistemas externos (NetBox) o archivos maestros deben ser estrictamente idempotentes.

Para las directrices completas de ingeniería, consultar `GEMINI.md` y `CLAUDE.md` en la raíz del proyecto.

---

## Calidad y Seguridad de Datos

El pipeline valida encabezados requeridos, rechaza columnas duplicadas y campos malformados, detecta llaves de merge inválidas o duplicadas, y valida las columnas requeridas del mapping antes de contactar NetBox. Los archivos de salida temporales se reemplazan atómicamente donde lo soporte el script individual.

Dado que el inventario contiene información de infraestructura, red, hardware y sistemas operativos, se debe restringir el acceso a:

- Jobs de Rundeck
- Credenciales de NetBox
- Hojas de cálculo fuente
- Archivos generados y logs

Evitar hacer commit de artefactos de ejecución o datos sensibles del inventario.
