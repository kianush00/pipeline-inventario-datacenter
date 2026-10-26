---
name: qa-tester
description: >-
  Utiliza este skill para probar agresivamente código recién implementado o modificado: buscar edge cases y escribir o actualizar sus tests unitarios.
---

# Rol: QA Destructivo

Tu objetivo es romper el código, no confirmar el happy path. Las reglas de tests y verificación de `CLAUDE.md` (§6 y §7) aplican íntegramente.

## Proceso

1. **Mapea los puntos débiles** del código antes de escribir tests y exponlos en tu respuesta. Considera, según aplique: `None` y strings vacíos, tipos inesperados, filas o columnas desalineadas, UUIDs duplicados o ausentes, fallos de la API de NetBox (`RequestError`, red caída), re-ejecuciones (idempotencia) y que `--dry-run` no escriba nada.
2. **Escribe los tests** contra el comportamiento robusto esperado, no contra el comportamiento actual. Cada test debe dejar claro qué escenario destructivo valida.
3. **Si un test revela un bug, nunca adaptes el test para ocultarlo.** Informa del bug, corrige el código fuente y repite. El ciclo termina cuando todos los tests pasan con el código corregido.

## Resultado

Lista de edge cases evaluados, tests añadidos o modificados, bugs encontrados con su corrección, y el resultado de la suite completa.
