"""
fusionar_inventarios.py
=======================
Fusiona un inventario "parseado" sobre un inventario "padre"
(source of truth), usando UUID como clave de fusión.

El layout de columnas y las reglas de fusión se leen desde
header_list.txt con formato:

    NOMBRE_COLUMNA|FLAG

donde FLAG indica:
    0 → conservar el valor del inventario padre
    1 → permitir que el valor del inventario parseado
        sobrescriba al del padre (si no está vacío ni es "N/A")

Las posiciones de las columnas se detectan automáticamente
leyendo el header del inventario padre.

El inventario padre es la base:
- Puede tener columnas adicionales no definidas en
  header_list.txt; éstas se conservan sin fusionar.
- Las columnas intermedias (presentes en el padre pero no en
  header_list.txt) también se conservan tal cual.

Uso:
    python3 fusionar_inventarios.py \\
        inventario_parseado.csv \\
        inventario_padre.csv \\
        inventario_fusionado.csv \\
        [header_list.txt]

Si no se indica la ruta de header_list.txt, se busca un archivo
llamado "header_list.txt" en el mismo directorio que este script.
"""

import sys
from pathlib import Path

from inventario_base import (
    error,
    is_empty_or_na,
    load_header_list,
    split_quoted_csv_line,
    strip_quotes,
)

# ============================================================
# RESOLVER POSICIONES EN EL HEADER DEL PADRE
# ============================================================

def resolve_positions(
    header_list: list[tuple[str, int]],
    parent_header_fields: list[str],
    parent_header_path: Path,
) -> dict[str, int]:
    """
    Detecta la posición (índice 0-based) de cada columna de
    header_list dentro del header del inventario padre.

    Reglas:
    - Cada nombre de header_list debe aparecer exactamente una
      vez en el header del padre.
    - Los nombres deben aparecer en el mismo orden relativo que
      en header_list (pueden existir columnas intermedias).
    - Si algún nombre falta o está fuera de orden, se aborta.

    Retorna un dict {nombre: índice_0based}.
    """
    # Construir mapa nombre->índice del padre (comprobando unicidad).
    name_to_index: dict[str, int] = {}
    for idx, raw_field in enumerate(parent_header_fields):
        field_name = strip_quotes(raw_field)
        if field_name in name_to_index:
            error(
                f"El inventario padre tiene el nombre de columna "
                f"'{field_name}' duplicado (posiciones "
                f"{name_to_index[field_name] + 1} y {idx + 1}).\n"
                f"  {parent_header_path}"
            )
        name_to_index[field_name] = idx

    # Verificar que todos los nombres de header_list existen y
    # que respetan el orden relativo.
    positions: dict[str, int] = {}
    previous_index = -1

    for name, _flag in header_list:
        if name not in name_to_index:
            error(
                f"La columna '{name}' definida en header_list.txt "
                f"no existe en el header del inventario padre.\n"
                f"  {parent_header_path}"
            )

        col_index = name_to_index[name]

        if col_index <= previous_index:
            error(
                f"La columna '{name}' definida en header_list.txt "
                f"aparece fuera de orden en el inventario padre "
                f"(posición {col_index + 1} ≤ posición anterior "
                f"{previous_index + 1}).\n"
                f"  {parent_header_path}"
            )

        positions[name] = col_index
        previous_index = col_index

    return positions


# ============================================================
# RESOLVER POSICIONES EN EL HEADER DEL PARSEADO
# ============================================================

def resolve_parsed_positions(
    header_list: list[tuple[str, int]],
    parsed_header_fields: list[str],
    parsed_header_path: Path,
) -> dict[str, int]:
    """
    Detecta la posición (índice 0-based) de cada columna de
    header_list dentro del header del inventario parseado.

    El inventario parseado debe contener exactamente las columnas
    de header_list, en el mismo orden y sin columnas extra.
    """
    defined_fields = len(header_list)

    if len(parsed_header_fields) != defined_fields:
        error(
            f"El inventario parseado tiene {len(parsed_header_fields)} "
            f"columnas; se esperaban {defined_fields} "
            f"(las definidas en header_list.txt).\n"
            f"  {parsed_header_path}"
        )

    positions: dict[str, int] = {}

    for idx, (name, _flag) in enumerate(header_list):
        found_name = strip_quotes(parsed_header_fields[idx])
        if found_name != name:
            error(
                f"El header del inventario parseado no coincide "
                f"con header_list.txt en la posición {idx + 1}:\n"
                f"  esperado : '{name}'\n"
                f"  encontrado: '{found_name}'\n"
                f"  {parsed_header_path}"
            )
        positions[name] = idx

    return positions


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    if len(sys.argv) < 4:
        print(
            f"Uso: {sys.argv[0]} "
            "inventario_parseado.csv "
            "inventario_padre.csv "
            "inventario_fusionado.csv "
            "[header_list.txt]"
        )
        sys.exit(1)

    input_parsed_path = Path(sys.argv[1])
    input_parent_path = Path(sys.argv[2])
    output_path       = Path(sys.argv[3])
    header_list_path  = (
        Path(sys.argv[4]) if len(sys.argv) >= 5
        else Path(__file__).resolve().parent / "header_list.txt"
    )

    for path, label in (
        (input_parsed_path, "inventario parseado"),
        (input_parent_path, "inventario padre"),
    ):
        if not path.is_file():
            error(f"No se encontró el {label}: {path}")

    # --------------------------------------------------------
    # Cargar header_list: [(nombre, flag), ...]
    # --------------------------------------------------------
    header_list = load_header_list(header_list_path)
    defined_fields = len(header_list)

    # Localizar la columna UUID.
    uuid_name = "UUID"
    uuid_in_list = any(name == uuid_name for name, _ in header_list)
    if not uuid_in_list:
        error(
            "No se encontró la columna 'UUID' en header_list.txt.\n"
            "UUID es necesario para realizar la fusión."
        )

    # --------------------------------------------------------
    # Leer y validar header del inventario parseado.
    # --------------------------------------------------------
    with input_parsed_path.open(
        "r", encoding="utf-8", errors="replace"
    ) as f:
        parsed_header_line = f.readline().rstrip("\r\n")

    parsed_header_fields = split_quoted_csv_line(parsed_header_line)
    if parsed_header_fields is None:
        error(
            "El header del inventario parseado tiene "
            "comillas desbalanceadas."
        )

    parsed_positions = resolve_parsed_positions(
        header_list, parsed_header_fields, input_parsed_path
    )

    uuid_parsed_idx = parsed_positions[uuid_name]

    # --------------------------------------------------------
    # Leer y validar header del inventario padre.
    # --------------------------------------------------------
    with input_parent_path.open(
        "r", encoding="utf-8", errors="replace"
    ) as f:
        parent_header_line = f.readline().rstrip("\r\n")

    parent_header_fields = split_quoted_csv_line(parent_header_line)
    if parent_header_fields is None:
        error(
            "El header del inventario padre tiene "
            "comillas desbalanceadas."
        )

    parent_positions = resolve_positions(
        header_list, parent_header_fields, input_parent_path
    )

    uuid_parent_idx      = parent_positions[uuid_name]
    parent_total_columns = len(parent_header_fields)
    extra_columns        = parent_total_columns - defined_fields

    # --------------------------------------------------------
    # Resumen informativo.
    # --------------------------------------------------------
    print(f"Columnas definidas en header_list : {defined_fields}")
    print(f"Columnas totales en el padre      : {parent_total_columns}")
    if extra_columns > 0:
        print(f"Columnas adicionales del padre    : {extra_columns}")
    print(f"Posición de UUID en el padre      : {uuid_parent_idx + 1}")
    print(f"Posición de UUID en el parseado   : {uuid_parsed_idx + 1}")

    # --------------------------------------------------------
    # Construir mapa de fusión:
    #   índice_en_padre -> índice_en_parseado
    # Solo para columnas con flag=1.
    # --------------------------------------------------------
    # [(idx_padre, idx_parseado), ...] para columnas fusionables
    merge_pairs: list[tuple[int, int]] = []

    for name, flag in header_list:
        if flag != 1:
            continue
        if name == uuid_name:
            continue  # el UUID nunca se sobreescribe
        parent_idx = parent_positions[name]
        parsed_idx = parsed_positions[name]
        merge_pairs.append((parent_idx, parsed_idx))

    # --------------------------------------------------------
    # Leer inventario parseado y construir mapa uuid -> fila.
    # --------------------------------------------------------
    parsed_data: dict[str, list[str]] = {}

    with input_parsed_path.open(
        "r", encoding="utf-8", errors="replace"
    ) as f:
        next(f, None)  # saltar header ya validado

        for line_number, raw_line in enumerate(f, start=2):
            line = raw_line.rstrip("\r\n")

            if not line:
                continue

            fields = split_quoted_csv_line(line)

            if fields is None:
                print(
                    f"[ERROR] Línea {line_number} del inventario parseado: "
                    "comillas desbalanceadas.",
                    file=sys.stderr,
                )
                continue

            if len(fields) != defined_fields:
                print(
                    f"[ERROR] Línea {line_number} del inventario parseado: "
                    f"se esperaban {defined_fields} campos, "
                    f"pero se encontraron {len(fields)}.",
                    file=sys.stderr,
                )
                continue

            uuid = strip_quotes(fields[uuid_parsed_idx])

            if uuid in ("", "N/A"):
                print(
                    f"[WARNING] Línea {line_number} del inventario parseado "
                    "ignorada: UUID vacío o N/A.",
                    file=sys.stderr,
                )
                continue

            if uuid in parsed_data:
                print(
                    f"[WARNING] UUID duplicado en el inventario parseado: "
                    f"'{uuid}'. Se usará la primera ocurrencia.",
                    file=sys.stderr,
                )
                continue

            parsed_data[uuid] = fields

    # --------------------------------------------------------
    # Leer inventario padre, fusionar y escribir salida.
    # --------------------------------------------------------
    with (
        output_path.open("w", encoding="utf-8", newline="") as out_f,
        input_parent_path.open(
            "r", encoding="utf-8", errors="replace"
        ) as in_f,
    ):
        # Escribir header del padre sin modificar.
        out_f.write(parent_header_line + "\n")
        next(in_f, None)  # saltar header ya leído

        for line_number, raw_line in enumerate(in_f, start=2):
            line = raw_line.rstrip("\r\n")

            if not line:
                continue

            fields = split_quoted_csv_line(line)

            if fields is None:
                print(
                    f"[ERROR] Línea {line_number} del inventario padre: "
                    "comillas desbalanceadas.",
                    file=sys.stderr,
                )
                continue

            if len(fields) != parent_total_columns:
                print(
                    f"[ERROR] Línea {line_number} del inventario padre: "
                    f"se esperaban {parent_total_columns} campos, "
                    f"pero se encontraron {len(fields)}.",
                    file=sys.stderr,
                )
                continue

            uuid = strip_quotes(fields[uuid_parent_idx])

            if uuid in parsed_data:
                parsed_fields = parsed_data[uuid]

                for parent_idx, parsed_idx in merge_pairs:
                    parsed_value = parsed_fields[parsed_idx]
                    if is_empty_or_na(parsed_value):
                        continue
                    fields[parent_idx] = parsed_value

            out_f.write(",".join(fields) + "\n")

    # --------------------------------------------------------
    # Resultado.
    # --------------------------------------------------------
    print()
    print("[OK] Inventarios fusionados correctamente.")
    print(f"Inventario parseado : {input_parsed_path}")
    print(f"Inventario padre    : {input_parent_path}")
    print(f"Inventario salida   : {output_path}")


if __name__ == "__main__":
    main()
