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
- Si la clave del maestro es vacía o N/A, la fila del maestro
  se conserva sin fusionar.
- Si una clave aparece más de una vez en cualquiera de los dos
  inventarios, las filas afectadas se conservan sin fusionar.
- Solo se fusionan filas cuya clave sea válida y única en ambos
  inventarios.

Uso:
    python3 merge_inventories.py \
        parsed_inventory.csv \
        master_inventory.csv \
        [merged_inventory.csv] \
        [header_list.txt]

Si no se indica la ruta de header_list.txt, se busca un archivo
llamado "header_list.txt" en el mismo directorio que este script.
"""

import os
import sys
import tempfile
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
# VALIDAR CLAVE DE UNIÓN
# ============================================================

def is_invalid_key(value: str) -> bool:
    """
    Retorna True si el valor no puede utilizarse como clave
    de unión.

    Una clave es inválida solamente si está vacía o es "N/A".
    """
    return is_empty_or_na(value)


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
# LEER CSV COMPLETO
# ============================================================

def load_csv_data(
    csv_path: Path,
) -> tuple[str, list[str], list[list[str]]]:
    """
    Lee un CSV completo en una sola pasada.

    Retorna:
        (línea_original_del_header, campos_del_header, filas_de_datos)

    Las filas se mantienen en memoria para evitar una segunda
    lectura del archivo durante el procesamiento posterior.
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

        rows: list[list[str]] = []

        for line_number, raw_line in enumerate(file, start=2):
            line = raw_line.rstrip("\r\n")

            if not line:
                continue

            fields = split_quoted_csv_line(line)

            if fields is None:
                print(
                    f"[ERROR] Línea {line_number} de "
                    f"{csv_path.name}: comillas desbalanceadas.",
                    file=sys.stderr
                )
                continue

            rows.append(fields)

    return header_line, header_fields, rows


# ============================================================
# VALIDAR ESTRUCTURA DE FILAS
# ============================================================

def validate_rows(
    rows: list[list[str]],
    expected_columns: int,
    csv_path: Path,
) -> list[list[str]]:
    """
    Valida que todas las filas tengan exactamente la cantidad
    de columnas esperada.

    Las filas inválidas se descartan.
    """
    valid_rows: list[list[str]] = []

    for row_number, fields in enumerate(rows, start=2):
        if len(fields) != expected_columns:
            print(
                f"[ERROR] Línea {row_number} de {csv_path.name}: "
                f"se esperaban {expected_columns} campos, "
                f"pero se encontraron {len(fields)}.",
                file=sys.stderr
            )
            continue

        valid_rows.append(fields)

    return valid_rows


# ============================================================
# CONSTRUIR MAPA DEL INVENTARIO PARSEADO
# ============================================================

def build_parsed_data(
    rows: list[list[str]],
    key_idx: int,
) -> tuple[dict[str, list[str]], set[str]]:
    """
    Construye un mapa:

        clave -> fila

    Las claves duplicadas se registran y quedan excluidas del
    mapa para impedir cualquier fusión posterior.
    """
    parsed_data: dict[str, list[str]] = {}
    duplicated_keys: set[str] = set()

    for fields in rows:
        key = strip_quotes(fields[key_idx])

        if is_invalid_key(key):
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

def find_parent_duplicated_keys(
    rows: list[list[str]],
    key_idx: int,
) -> set[str]:
    """
    Detecta claves duplicadas en el inventario maestro.

    Retorna el conjunto de claves duplicadas.
    """
    seen_keys: set[str] = set()
    duplicated_keys: set[str] = set()

    for fields in rows:
        key = strip_quotes(fields[key_idx])

        if is_invalid_key(key):
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
    if len(sys.argv) < 3:
        print(
            f"Uso: {sys.argv[0]} "
            "parsed_inventory.csv "
            "master_inventory.csv "
            "[merged_inventory.csv] "
            "[header_list.txt]"
        )
        sys.exit(1)

    if len(sys.argv) > 5:
        error(
            f"Cantidad de argumentos inválida.\n"
            f"Uso: {sys.argv[0]} "
            "parsed_inventory.csv "
            "master_inventory.csv "
            "[merged_inventory.csv] "
            "[header_list.txt]"
        )

    input_parsed_path = Path(sys.argv[1])
    input_parent_path = Path(sys.argv[2])
    output_path = (
        Path(sys.argv[3])
        if len(sys.argv) >= 4
        else Path(__file__).resolve().parent / "merged_inventory.csv"
    )
    header_list_path = (
        Path(sys.argv[4])
        if len(sys.argv) >= 5
        else Path(__file__).resolve().parent / "header_list.txt"
    )

    for path, label in (
        (input_parsed_path, "inventario parseado"),
        (input_parent_path, "inventario maestro"),
    ):
        if not path.is_file():
            error(f"No se encontró el {label}: {path}")

    # --------------------------------------------------------
    # Validar colisiones entre archivos de entrada y salida.
    # --------------------------------------------------------
    resolved_output = output_path.resolve()
    resolved_parsed = input_parsed_path.resolve()
    resolved_parent = input_parent_path.resolve()

    if resolved_output == resolved_parsed:
        error(
            "El archivo de salida no puede ser el mismo archivo "
            "que el inventario parseado:\n"
            f"  {output_path}"
        )

    if resolved_output == resolved_parent:
        error(
            "El archivo de salida no puede ser el mismo archivo "
            "que el inventario maestro:\n"
            f"  {output_path}"
        )

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
    # Leer completamente el inventario parseado.
    # --------------------------------------------------------
    (
        _parsed_header_line,
        parsed_header_fields,
        parsed_rows,
    ) = load_csv_data(input_parsed_path)

    parsed_positions = resolve_positions(
        header_list,
        parsed_header_fields,
        input_parsed_path,
    )

    key_parsed_idx = parsed_positions[key_name]

    parsed_rows = validate_rows(
        parsed_rows,
        defined_fields,
        input_parsed_path,
    )

    # --------------------------------------------------------
    # Leer completamente el inventario maestro.
    # --------------------------------------------------------
    (
        parent_header_line,
        parent_header_fields,
        parent_rows,
    ) = load_csv_data(input_parent_path)

    parent_positions = resolve_positions(
        header_list,
        parent_header_fields,
        input_parent_path,
    )

    key_parent_idx = parent_positions[key_name]
    parent_total_columns = len(parent_header_fields)
    extra_columns = parent_total_columns - defined_fields

    parent_rows = validate_rows(
        parent_rows,
        parent_total_columns,
        input_parent_path,
    )

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
    # Validar unicidad de claves usando las filas ya cargadas
    # en memoria.
    # --------------------------------------------------------
    parsed_data, parsed_duplicated_keys = build_parsed_data(
        parsed_rows,
        key_parsed_idx,
    )

    parent_duplicated_keys = find_parent_duplicated_keys(
        parent_rows,
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
    # Crear archivo temporal en el mismo directorio que la
    # salida para permitir un reemplazo atómico mediante
    # os.replace().
    # --------------------------------------------------------
    temporary_output_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as out_f:
            temporary_output_path = Path(out_f.name)

            # ------------------------------------------------
            # Escribir header del maestro sin modificar.
            # ------------------------------------------------
            out_f.write(parent_header_line + "\n")

            # ------------------------------------------------
            # Procesar todas las filas del maestro ya cargadas
            # en memoria.
            # ------------------------------------------------
            for line_number, fields in enumerate(parent_rows, start=2):
                key = strip_quotes(fields[key_parent_idx])

                # ------------------------------------------------
                # Clave inválida:
                # conservar la fila del maestro sin fusionar.
                # ------------------------------------------------
                if is_invalid_key(key):
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
        # El archivo temporal solo reemplaza la salida definitiva
        # si todo el procesamiento anterior terminó correctamente.
        # --------------------------------------------------------
        os.replace(temporary_output_path, output_path)
        temporary_output_path = None

    finally:
        # --------------------------------------------------------
        # Si el proceso falló antes del reemplazo, eliminar el
        # archivo temporal para no dejar residuos.
        # --------------------------------------------------------
        if temporary_output_path is not None:
            try:
                temporary_output_path.unlink()
            except FileNotFoundError:
                pass

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