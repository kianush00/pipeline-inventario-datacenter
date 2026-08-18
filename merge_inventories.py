"""
merge_inventories.py
=======================
Fusiona un inventario "parseado" sobre un inventario "maestro"
(source of truth), usando como clave de fusión la columna cuyo
FLAG en header_list.txt sea 2.

El layout de columnas y las reglas de fusión se leen desde
header_list.txt con formato:

    NOMBRE_COLUMNA|FLAG

donde FLAG indica:
    0 → conservar el valor del inventario maestro
    1 → permitir que el valor del inventario parseado
        sobrescriba al del maestro (si no está vacío ni es "N/A")
    2 → columna utilizada como clave de unión

Debe existir exactamente una columna con FLAG 2.

La columna con FLAG 2 puede tener cualquier nombre. Ese nombre
se utiliza como clave de unión tanto en el inventario maestro
como en el inventario parseado.

Las posiciones de las columnas se detectan automáticamente
leyendo los headers de ambos CSV.

El inventario maestro es la base:
- Todas sus filas se conservan en el CSV de salida.
- Las columnas adicionales no definidas en header_list.txt
  se conservan sin fusionar.
- Las columnas con FLAG 0 se conservan sin fusionar.
- Las columnas con FLAG 1 pueden ser actualizadas desde el
  inventario parseado.
- La columna con FLAG 2 se utiliza exclusivamente como clave
  de unión y nunca se sobrescribe.

La unión se comporta como un LEFT JOIN:
- Si la clave del maestro no existe en el parseado, la fila del
  maestro se conserva sin modificar.
- Si la clave del maestro es vacía, N/A o "Not Settable", la fila
  del maestro se conserva sin fusionar.
- Si una clave aparece más de una vez en cualquiera de los dos
  inventarios, las filas afectadas se conservan sin fusionar.
- Solo se fusionan filas cuya clave sea válida y única en ambos
  inventarios.

Uso:
    python3 merge_inventories.py \
        parsed_inventory.csv \
        master_inventory.csv \
        merged_inventory.csv \
        [header_list.txt]

Si no se indica la ruta de header_list.txt, se busca un archivo
llamado "header_list.txt" en el mismo directorio que este script.
"""

import sys
from pathlib import Path

from base_inventory import (
    error,
    is_empty_or_na,
    load_header_list,
    split_quoted_csv_line,
    strip_quotes,
)

# ============================================================
# VALIDAR Y RESOLVER CLAVE DE UNIÓN
# ============================================================

def resolve_key_column(
    header_list: list[tuple[str, int]],
) -> str:
    """
    Determina la columna utilizada como clave de unión.

    Debe existir exactamente una entrada con FLAG 2.

    Retorna:
        nombre de la columna con FLAG 2.
    """
    key_columns = [
        name
        for name, flag in header_list
        if flag == 2
    ]

    if len(key_columns) != 1:
        error(
            "header_list.txt debe contener exactamente una "
            "columna con flag 2.\n"
            f"Se encontraron {len(key_columns)}."
        )

    return key_columns[0]


# ============================================================
# RESOLVER POSICIONES EN UN HEADER
# ============================================================

def resolve_positions(
    header_list: list[tuple[str, int]],
    header_fields: list[str],
    csv_path: Path,
) -> dict[str, int]:
    """
    Detecta la posición (índice 0-based) de cada columna de
    header_list dentro del header de un CSV.

    Reglas:
    - Cada nombre de header_list debe aparecer exactamente una
      vez en el header del CSV.
    - El orden de las columnas NO importa.
    - Los CSV pueden contener columnas adicionales no definidas
      en header_list.txt.

    Retorna:
        dict {nombre_columna: índice_0based}
    """
    name_to_index: dict[str, int] = {}

    for idx, raw_field in enumerate(header_fields):
        field_name = strip_quotes(raw_field)

        if field_name in name_to_index:
            error(
                f"El archivo tiene el nombre de columna "
                f"'{field_name}' duplicado (posiciones "
                f"{name_to_index[field_name] + 1} y {idx + 1}).\n"
                f"  {csv_path}"
            )

        name_to_index[field_name] = idx

    positions: dict[str, int] = {}

    for name, _flag in header_list:
        if name not in name_to_index:
            error(
                f"La columna '{name}' definida en header_list.txt "
                f"no existe en el header del archivo.\n"
                f"  {csv_path}"
            )

        positions[name] = name_to_index[name]

    return positions


# ============================================================
# LEER HEADER
# ============================================================

def read_header(
    csv_path: Path,
) -> tuple[str, list[str]]:
    """
    Lee y valida el header de un CSV.

    Retorna:
        (línea_original, campos_del_header)
    """
    with csv_path.open(
        "r",
        encoding="utf-8",
        errors="replace",
    ) as file:
        header_line = file.readline().rstrip("\r\n")

    if not header_line:
        error(f"El archivo CSV está vacío:\n  {csv_path}")

    header_fields = split_quoted_csv_line(header_line)

    if header_fields is None:
        error(
            f"El header del CSV tiene comillas desbalanceadas:\n"
            f"  {csv_path}"
        )

    return header_line, header_fields


# ============================================================
# LEER DATOS DEL CSV PARSEADO
# ============================================================

def load_parsed_data(
    input_parsed_path: Path,
    defined_fields: int,
    key_idx: int,
) -> tuple[dict[str, list[str]], set[str]]:
    """
    Lee el inventario parseado y construye un mapa:

        clave -> fila

    Solo se consideran claves válidas.

    Las claves duplicadas se registran en duplicated_keys y
    quedan excluidas del mapa para impedir cualquier fusión
    posterior.
    """
    parsed_data: dict[str, list[str]] = {}
    duplicated_keys: set[str] = set()

    with input_parsed_path.open(
        "r",
        encoding="utf-8",
        errors="replace",
    ) as file:
        next(file, None)

        for line_number, raw_line in enumerate(file, start=2):
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
                    file=sys.stderr
                )
                continue

            key = strip_quotes(fields[key_idx])

            if key in ("", "N/A", "Not Settable"):
                continue

            if key in duplicated_keys:
                continue

            if key in parsed_data:
                print(
                    f"[WARNING] Clave duplicada en el inventario "
                    f"parseado: '{key}'. Las filas con esta clave "
                    "no serán fusionadas.",
                    file=sys.stderr
                )
                del parsed_data[key]
                duplicated_keys.add(key)
                continue

            parsed_data[key] = fields

    return parsed_data, duplicated_keys


# ============================================================
# VALIDAR UNICIDAD DE CLAVES EN EL INVENTARIO MAESTRO
# ============================================================

def validate_parent_keys(
    input_parent_path: Path,
    parent_total_columns: int,
    key_idx: int,
) -> set[str]:
    """
    Recorre el inventario maestro y detecta claves duplicadas.

    Retorna el conjunto de claves duplicadas.

    La validación se realiza antes de comenzar la fusión.
    """
    seen_keys: set[str] = set()
    duplicated_keys: set[str] = set()

    with input_parent_path.open(
        "r",
        encoding="utf-8",
        errors="replace",
    ) as file:
        next(file, None)

        for line_number, raw_line in enumerate(file, start=2):
            line = raw_line.rstrip("\r\n")

            if not line:
                continue

            fields = split_quoted_csv_line(line)

            if fields is None:
                print(
                    f"[ERROR] Línea {line_number} del inventario maestro: "
                    "comillas desbalanceadas.",
                    file=sys.stderr
                )
                continue

            if len(fields) != parent_total_columns:
                print(
                    f"[ERROR] Línea {line_number} del inventario maestro: "
                    f"se esperaban {parent_total_columns} campos, "
                    f"pero se encontraron {len(fields)}.",
                    file=sys.stderr
                )
                continue

            key = strip_quotes(fields[key_idx])

            if key in ("", "N/A", "Not Settable"):
                continue

            if key in seen_keys:
                duplicated_keys.add(key)
            else:
                seen_keys.add(key)

    for key in sorted(duplicated_keys):
        print(
            f"[WARNING] Clave duplicada en el inventario maestro: "
            f"'{key}'. Las filas con esta clave no serán fusionadas.",
            file=sys.stderr
        )

    return duplicated_keys


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    if len(sys.argv) < 4:
        print(
            f"Uso: {sys.argv[0]} "
            "inventario_parseado.csv "
            "inventario_maestro.csv "
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
        (input_parent_path, "inventario maestro"),
    ):
        if not path.is_file():
            error(f"No se encontró el {label}: {path}")

    # --------------------------------------------------------
    # Cargar header_list.
    # --------------------------------------------------------
    header_list = load_header_list(header_list_path)
    defined_fields = len(header_list)

    # --------------------------------------------------------
    # Determinar la columna de unión a partir del flag 2.
    # --------------------------------------------------------
    key_name = resolve_key_column(header_list)

    # --------------------------------------------------------
    # Leer y validar header del inventario parseado.
    # --------------------------------------------------------
    _parsed_header_line, parsed_header_fields = read_header(
        input_parsed_path
    )

    parsed_positions = resolve_positions(
        header_list,
        parsed_header_fields,
        input_parsed_path,
    )

    key_parsed_idx = parsed_positions[key_name]

    # --------------------------------------------------------
    # Leer y validar header del inventario maestro.
    # --------------------------------------------------------
    parent_header_line, parent_header_fields = read_header(
        input_parent_path
    )

    parent_positions = resolve_positions(
        header_list,
        parent_header_fields,
        input_parent_path,
    )

    key_parent_idx = parent_positions[key_name]
    parent_total_columns = len(parent_header_fields)
    extra_columns = parent_total_columns - defined_fields

    # --------------------------------------------------------
    # Construir mapa de fusión.
    #
    # Solo flag 1.
    #
    # flag 0 -> nunca se fusiona.
    # flag 1 -> puede actualizarse desde el parseado.
    # flag 2 -> clave de unión; nunca se sobrescribe.
    # --------------------------------------------------------
    merge_pairs: list[tuple[int, int]] = []

    for name, flag in header_list:
        if flag != 1:
            continue

        parent_idx = parent_positions[name]
        parsed_idx = parsed_positions[name]

        merge_pairs.append((parent_idx, parsed_idx))

    # --------------------------------------------------------
    # Validar unicidad de claves ANTES de comenzar la fusión.
    # --------------------------------------------------------
    parsed_data, parsed_duplicated_keys = load_parsed_data(
        input_parsed_path,
        defined_fields,
        key_parsed_idx,
    )

    parent_duplicated_keys = validate_parent_keys(
        input_parent_path,
        parent_total_columns,
        key_parent_idx,
    )

    # --------------------------------------------------------
    # Resumen informativo.
    # --------------------------------------------------------
    print(f"Columnas definidas en header_list : {defined_fields}")
    print(f"Columnas totales en el maestro    : {parent_total_columns}")

    if extra_columns > 0:
        print(f"Columnas adicionales del maestro  : {extra_columns}")

    print(f"Clave de unión                    : {key_name}")
    print(f"Posición de clave en el maestro   : {key_parent_idx + 1}")
    print(f"Posición de clave en el parseado  : {key_parsed_idx + 1}")
    print(f"Claves válidas en el parseado     : {len(parsed_data)}")
    print(
        f"Claves duplicadas en el parseado  : "
        f"{len(parsed_duplicated_keys)}"
    )
    print(
        f"Claves duplicadas en el maestro   : "
        f"{len(parent_duplicated_keys)}"
    )

    # --------------------------------------------------------
    # Leer inventario maestro, realizar LEFT JOIN y escribir salida.
    #
    # TODAS las filas del maestro se conservan.
    #
    # Una fila solamente se modifica si:
    #   1. Tiene una clave válida.
    #   2. La clave es única en el maestro.
    #   3. La clave existe en el parseado.
    #   4. La clave es única en el parseado.
    #
    # Si alguna condición falla, la fila del maestro se escribe
    # sin fusionar.
    # --------------------------------------------------------
    with (
        output_path.open(
            "w",
            encoding="utf-8",
            newline="",
        ) as out_f,
        input_parent_path.open(
            "r",
            encoding="utf-8",
            errors="replace",
        ) as in_f,
    ):
        # Header del maestro sin modificar.
        out_f.write(parent_header_line + "\n")
        next(in_f, None)

        for line_number, raw_line in enumerate(in_f, start=2):
            line = raw_line.rstrip("\r\n")

            if not line:
                continue

            fields = split_quoted_csv_line(line)

            if fields is None:
                print(
                    f"[ERROR] Línea {line_number} del inventario maestro: "
                    "comillas desbalanceadas.",
                    file=sys.stderr
                )
                continue

            if len(fields) != parent_total_columns:
                print(
                    f"[ERROR] Línea {line_number} del inventario maestro: "
                    f"se esperaban {parent_total_columns} campos, "
                    f"pero se encontraron {len(fields)}.",
                    file=sys.stderr
                )
                continue

            key = strip_quotes(fields[key_parent_idx])

            # ------------------------------------------------
            # Clave inválida:
            # conservar la fila del maestro sin fusionar.
            # ------------------------------------------------
            if key in ("", "N/A", "Not Settable"):
                out_f.write(",".join(fields) + "\n")
                continue

            # ------------------------------------------------
            # Clave duplicada en el maestro:
            # conservar la fila sin fusionar.
            # ------------------------------------------------
            if key in parent_duplicated_keys:
                out_f.write(",".join(fields) + "\n")
                continue

            # ------------------------------------------------
            # La clave no existe en el parseado:
            # LEFT JOIN -> conservar la fila del maestro.
            # ------------------------------------------------
            if key not in parsed_data:
                print(
                    f"[WARNING] Línea {line_number} del inventario maestro: "
                    f"la clave '{key}' no existe en el inventario "
                    "parseado. La fila se conservará sin fusionar.",
                    file=sys.stderr
                )
                out_f.write(",".join(fields) + "\n")
                continue

            # ------------------------------------------------
            # La clave existe y es única en ambos inventarios.
            # Fusionar solamente las columnas con flag 1.
            # ------------------------------------------------
            parsed_fields = parsed_data[key]

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
    print(f"Inventario maestro  : {input_parent_path}")
    print(f"Inventario salida   : {output_path}")


if __name__ == "__main__":
    main()