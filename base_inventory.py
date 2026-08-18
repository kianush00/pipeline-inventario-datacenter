"""
base_inventory.py
==================
Módulo de utilidades compartido por los tres scripts de inventario:

    prepare_master_inventory.py
    parse_job_output.py
    merge_inventories.py

No se ejecuta directamente.
"""

import sys
from pathlib import Path
from typing import NoReturn

# ============================================================
# SALIDA DE ERROR FATAL
# ============================================================

def error(message: str) -> NoReturn:
    """Imprime un mensaje de error en stderr y termina con exit 1."""
    print(f"[ERROR] {message}", file=sys.stderr)
    sys.exit(1)


# ============================================================
# FORMATO header_list.txt
#
# Cada línea no vacía y no comentario tiene la forma:
#
#     NOMBRE_COLUMNA|FLAG
#
# donde FLAG puede ser:
#
#     0 -> columna no participa del procesamiento.
#     1 -> columna participa normalmente.
#     2 -> columna participa normalmente y corresponde a la
#          clave utilizada para identificar una fila.
#
# El orden de las líneas define el orden lógico de las columnas
# que interesan al pipeline. Las columnas intermedias del
# inventario maestro que no aparezcan en header_list.txt se
# conservan sin tocar y nunca se fusionan.
# ============================================================

def load_header_list(path: Path) -> list[tuple[str, int]]:
    """
    Lee header_list.txt y devuelve una lista de tuplas:

        [(nombre, flag), ...]

    Validaciones:
    - El archivo debe existir y no estar vacío.
    - Cada línea debe tener exactamente dos campos separados por '|'.
    - NOMBRE no puede estar vacío.
    - FLAG debe ser '0', '1' o '2'.
    - No puede haber nombres duplicados.
    - Debe existir exactamente una columna con FLAG 2.

    Parámetros
    ----------
    path : Path
        Ruta al archivo header_list.txt.

    Retorna
    -------
    list[tuple[str, int]]
        Lista de (nombre_columna, flag).
    """
    if not path.is_file():
        error(
            f"No se encontró el archivo header_list:\n"
            f"  {path}"
        )

    entries: list[tuple[str, int]] = []
    seen_names: set[str] = set()

    with path.open("r", encoding="utf-8") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.strip()

            # ------------------------------------------------
            # Ignorar líneas vacías y comentarios.
            # ------------------------------------------------
            if not line or line.startswith("#"):
                continue

            parts = line.split("|")

            if len(parts) != 2:
                error(
                    f"header_list línea {line_number}: formato inválido:\n"
                    f"  {line}\n"
                    f"Formato esperado:\n"
                    f"  NOMBRE_COLUMNA|0\n"
                    f"  NOMBRE_COLUMNA|1\n"
                    f"  NOMBRE_COLUMNA|2"
                )

            name, flag_str = parts

            if not name:
                error(
                    f"header_list línea {line_number}: "
                    f"el nombre de columna está vacío."
                )

            if flag_str not in ("0", "1", "2"):
                error(
                    f"header_list línea {line_number}: "
                    f"el flag debe ser 0, 1 o 2, se encontró '{flag_str}'."
                )

            if name in seen_names:
                error(
                    f"header_list línea {line_number}: "
                    f"nombre de columna duplicado: '{name}'."
                )

            seen_names.add(name)
            entries.append((name, int(flag_str)))

    if not entries:
        error("header_list está vacío (sin entradas válidas).")

    key_entries = [
        name
        for name, flag in entries
        if flag == 2
    ]

    if len(key_entries) != 1:
        error(
            "header_list debe contener exactamente una columna "
            f"con FLAG 2, pero se encontraron {len(key_entries)}.\n"
            + (
                "Columnas con FLAG 2:\n"
                + "\n".join(f"  - {name}" for name in key_entries)
                if key_entries
                else "No existe ninguna columna con FLAG 2."
            )
        )

    return entries


# ============================================================
# PARSEO DE UNA LÍNEA CSV RESPETANDO COMILLAS
#
# Devuelve los campos "en crudo" (incluyendo las comillas que
# los delimitan y las comillas dobles internas sin des-escapar).
# Solo se usa para ubicar los límites de campo (comas fuera de
# comillas), no para interpretar el contenido.
#
# Retorna None si las comillas están desbalanceadas.
# ============================================================

def split_quoted_csv_line(line: str) -> list[str] | None:
    """
    Divide una línea CSV respetando campos entrecomillados.

    Se elimina defensivamente cualquier terminador de línea
    residual (CR, LF o CRLF) antes de procesar la línea.

    Cada campo se devuelve tal como aparece en el texto, comillas
    incluidas. No des-escapa comillas dobles internas.

    Retorna None si las comillas están desbalanceadas.
    """
    line = line.rstrip("\r\n")

    fields: list[str] = []
    field_chars: list[str] = []
    in_quotes = False
    i = 0
    length = len(line)

    while i < length:
        char = line[i]

        if char == '"':
            field_chars.append(char)
            if in_quotes and i + 1 < length and line[i + 1] == '"':
                # Comilla doble escapada dentro de un campo.
                field_chars.append('"')
                i += 1
            else:
                in_quotes = not in_quotes
        elif char == "," and not in_quotes:
            fields.append("".join(field_chars))
            field_chars = []
        else:
            field_chars.append(char)

        i += 1

    fields.append("".join(field_chars))

    if in_quotes:
        return None

    return fields


# ============================================================
# LIMPIEZA DE VALOR CSV
# ============================================================

def strip_quotes(value: str) -> str:
    """Elimina las comillas externas de un campo CSV y des-escapa
    las comillas dobles internas.

    Asume que el valor proviene de split_quoted_csv_line() y que
    las comillas del campo ya fueron validadas allí. Esta función
    no valida por sí misma si existen comillas desbalanceadas.

    Ejemplos:
        '"hello"' -> 'hello'
        campo con comillas duplicadas -> campo con comillas simples
        'unquoted' -> 'unquoted'
    """
    clean = value

    if len(clean) >= 2 and clean[0] == '"' and clean[-1] == '"':
        clean = clean[1:-1]

    clean = clean.replace('""', '"')
    return clean


def is_empty_or_na(value: str) -> bool:
    """Retorna True si el valor, con o sin comillas, es vacío,
    contiene solamente espacios en blanco o es 'N/A'.

    Ejemplos:
        '' -> True
        '   ' -> True
        'N/A' -> True
        '""' -> True
        '"N/A"' -> True
        '"   "' -> True
        'hello' -> False
    """
    return strip_quotes(value).strip() in ("", "N/A")


def clean_value(value: str) -> str:
    """Elimina saltos de línea internos de un valor.

    Los saltos de línea se reemplazan por un espacio para evitar
    concatenar palabras artificialmente.

    El orden de los reemplazos es deliberado: CRLF se procesa
    primero para evitar que '\\r\\n' sea convertido en dos espacios.

    Ejemplos:
        'hello\\nworld' -> 'hello world'
        'hello\\r\\nworld' -> 'hello world'
        'hello world' -> 'hello world'
    """
    return (
        value
        .replace("\r\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
    )