"""
merge_inventories.py
=======================
Fusiona un inventario "parseado" sobre un inventario "maestro"
(source of truth), usando como clave de fusión la columna cuyo
FLAG en rundeck_header_list.txt sea 2.

El layout de columnas y las reglas de fusión se leen desde
rundeck_header_list.txt con formato:

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
- Las columnas adicionales no definidas en rundeck_header_list.txt
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
        --parsed parsed_job_output.csv \
        --master prepared_master_inventory.csv \
        [--output merged_inventory.csv] \
        [--header-list rundeck_header_list.txt]

Cada opción tiene una forma corta: -p, -m, -o y -l respectivamente.

Los inventarios se indican con opciones nombradas (y no por posición) para que
un orden accidental de los archivos no invierta silenciosamente la fusión.

Si no se indica --header-list, se busca un archivo llamado
"rundeck_header_list.txt" en el mismo directorio que este script. Si no se
indica --output, se escribe "merged_inventory.csv" en ese mismo directorio.
"""

import argparse
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from base_inventory import (
    error,
    is_empty_or_na,
    load_rundeck_header_list,
    split_quoted_csv_line,
    strip_quotes,
)

# ============================================================
# ESTRUCTURAS DE DATOS DEL FLUJO PRINCIPAL
# ============================================================


class KeyedRows(NamedTuple):
    """Filas de un inventario indexadas por clave de unión."""

    rows_by_key: dict[str, list[str]]
    duplicated_keys: set[str]


class CliPaths(NamedTuple):
    """Rutas resueltas a partir de los argumentos de línea de comandos."""

    parsed: Path
    master: Path
    output: Path
    rundeck_header_list: Path


class LoadedInventory(NamedTuple):
    """Inventario CSV ya leído, con sus posiciones de columna y filas válidas."""

    header_line: str
    header_fields: list[str]
    positions: dict[str, int]
    rows: list[list[str]]


class MergePlan(NamedTuple):
    """Datos de solo lectura necesarios para decidir el destino de cada fila del maestro."""

    key_master_idx: int
    parsed_data: dict[str, list[str]]
    master_duplicated_keys: set[str]
    merge_pairs: list[tuple[int, int]]


# ============================================================
# VALIDAR Y RESOLVER CLAVE DE UNIÓN
# ============================================================


def resolve_key_column(
    rundeck_header_list: list[tuple[str, int]],
) -> str:
    """
    Determina la columna utilizada como clave de unión.
    Debe existir exactamente una entrada con FLAG 2.

    Retorna:
        nombre de la columna con FLAG 2.
    """
    key_columns = [name for name, flag in rundeck_header_list if flag == 2]

    if len(key_columns) != 1:
        error(
            "rundeck_header_list.txt debe contener exactamente una "
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


def extract_key(fields: list[str], key_idx: int) -> str:
    """Retorna la clave de unión de una fila, sin comillas y en minúsculas."""
    return strip_quotes(fields[key_idx]).lower()


def is_identity_duplicate(
    row_a: list[str], row_b: list[str], positions: dict[str, int]
) -> bool:
    """
    Comprueba si dos filas con la misma clave son el mismo registro de nodo
    repetido, comparando solo los campos de identidad (nombre, MAC, IP y serial,
    sin distinguir mayúsculas ni espacios). Los demás campos no se evalúan.
    """
    fields_to_check = ["Nombre maquina", "MAC", "IP", "Serial Number"]
    for field in fields_to_check:
        idx = positions.get(field)
        if idx is not None and idx < len(row_a) and idx < len(row_b):
            val_a = strip_quotes(row_a[idx]).strip().lower()
            val_b = strip_quotes(row_b[idx]).strip().lower()
            if val_a != val_b:
                return False
    return True


# ============================================================
# RESOLVER POSICIONES EN UN HEADER
# ============================================================


def resolve_positions(
    rundeck_header_list: list[tuple[str, int]],
    header_fields: list[str],
    csv_path: Path,
) -> dict[str, int]:
    """
    Detecta la posición (índice 0-based) de cada columna de
    rundeck_header_list dentro del header de un CSV.

    Reglas:
    - Cada nombre de rundeck_header_list debe aparecer exactamente una
      vez en el header del CSV.
    - El orden de las columnas NO importa.
    - Los CSV pueden contener columnas adicionales no definidas
      en rundeck_header_list.txt.

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

    for name, _flag in rundeck_header_list:
        if name not in name_to_index:
            error(
                f"La columna '{name}' definida en rundeck_header_list.txt "
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
            error(f"El header del CSV tiene comillas desbalanceadas:\n  {csv_path}")

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
                    file=sys.stderr,
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
                file=sys.stderr,
            )
            continue

        valid_rows.append(fields)

    return valid_rows


# ============================================================
# CONSTRUIR MAPA DEL INVENTARIO PARSEADO
# ============================================================


def index_rows_by_unique_key(
    rows: list[list[str]],
    key_idx: int,
    positions: dict[str, int],
    inventory_label: str,
) -> KeyedRows:
    """
    Indexa las filas por clave de unión separando las claves únicas de las duplicadas.

    Decisiones de diseño:
    - Lógica común al inventario parseado y al maestro (`inventory_label` solo
      contextualiza los logs), para que ambos apliquen exactamente la misma regla.
    - Las filas con clave inválida se ignoran. Si una clave se repite y las filas
      son duplicados de identidad, se conserva la primera y se omite el resto; si
      difieren, la clave se marca como duplicada y se excluye del índice para
      impedir cualquier fusión posterior.

    Retorna:
        KeyedRows: (clave -> fila única, claves duplicadas)
    """
    unique_rows: dict[str, list[str]] = {}
    duplicated_keys: set[str] = set()

    for fields in rows:
        key = extract_key(fields, key_idx)

        if is_invalid_key(key) or key in duplicated_keys:
            continue

        if key not in unique_rows:
            unique_rows[key] = fields
            continue

        if is_identity_duplicate(unique_rows[key], fields, positions):
            print(
                f"[WARNING] Duplicado de identidad detectado en el inventario "
                f"{inventory_label} para la clave '{key}'. Omitiendo duplicado.",
                file=sys.stderr,
            )
            continue

        del unique_rows[key]
        duplicated_keys.add(key)

    for key in sorted(duplicated_keys):
        print(
            f"[WARNING] Clave duplicada en el inventario {inventory_label}: "
            f"'{key}'. Las filas con esta clave difieren y no serán fusionadas.",
            file=sys.stderr,
        )

    return KeyedRows(unique_rows, duplicated_keys)


def build_parsed_data(
    rows: list[list[str]],
    key_idx: int,
    positions: dict[str, int],
) -> KeyedRows:
    """
    Construye un mapa:

        clave -> fila

    Las claves duplicadas se registran y quedan excluidas del
    mapa para impedir cualquier fusión posterior, a menos que sean duplicados de identidad.
    """
    return index_rows_by_unique_key(rows, key_idx, positions, "parseado")


# ============================================================
# VALIDAR UNICIDAD DE CLAVES EN EL INVENTARIO MAESTRO
# ============================================================


def find_master_duplicated_keys(
    rows: list[list[str]],
    key_idx: int,
    positions: dict[str, int],
) -> set[str]:
    """
    Detecta claves duplicadas en el inventario maestro.

    Retorna el conjunto de claves duplicadas (aquellas que no son duplicados de identidad).
    """
    return index_rows_by_unique_key(rows, key_idx, positions, "maestro").duplicated_keys


# ============================================================
# ARGUMENTOS Y VALIDACIÓN DE RUTAS
# ============================================================


def parse_cli_paths(argv: Sequence[str]) -> CliPaths:
    """
    Interpreta los argumentos de línea de comandos (sin el nombre del programa).

    Decisiones de diseño:
    - Los inventarios son opciones nombradas obligatorias (`--parsed`, `--master`):
      con argumentos posicionales, intercambiar los archivos no produce ningún
      error y la fusión se ejecuta al revés sin avisar.
    - La salida y el rundeck_header_list son opcionales y, por defecto, se resuelven
      en el directorio de este script.

    argparse termina el proceso con un mensaje de uso si falta una opción obligatoria.
    """
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Fusiona un inventario parseado sobre el inventario maestro."
    )
    parser.add_argument(
        "-p", "--parsed", type=Path, required=True, help="CSV del inventario parseado."
    )
    parser.add_argument(
        "-m", "--master", type=Path, required=True, help="CSV del inventario maestro."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=script_dir / "merged_inventory.csv",
        help="CSV de salida (por defecto merged_inventory.csv junto al script).",
    )
    parser.add_argument(
        "-l",
        "--header-list",
        type=Path,
        default=script_dir / "rundeck_header_list.txt",
        help="Ruta de rundeck_header_list.txt (por defecto junto al script).",
    )
    args = parser.parse_args(argv)
    return CliPaths(
        parsed=args.parsed,
        master=args.master,
        output=args.output,
        rundeck_header_list=args.header_list,
    )


def validate_paths(paths: CliPaths) -> None:
    """
    Verifica que existan los inventarios de entrada y que la salida no los pise.

    Termina el proceso con error si alguna validación falla.
    """
    labeled_inputs = (
        (paths.parsed, "inventario parseado"),
        (paths.master, "inventario maestro"),
    )
    ensure_inputs_exist(labeled_inputs)
    ensure_output_does_not_overwrite(paths.output, labeled_inputs)


def ensure_inputs_exist(labeled_inputs: tuple[tuple[Path, str], ...]) -> None:
    """Termina el proceso con error si algún inventario de entrada no existe."""
    for path, label in labeled_inputs:
        if not path.is_file():
            error(f"No se encontró el {label}: {path}")


def ensure_output_does_not_overwrite(
    output_path: Path, labeled_inputs: tuple[tuple[Path, str], ...]
) -> None:
    """Termina el proceso con error si la salida coincide con algún inventario de entrada."""
    resolved_output = output_path.resolve()
    for path, label in labeled_inputs:
        if resolved_output == path.resolve():
            error(
                f"El archivo de salida no puede ser el mismo archivo "
                f"que el {label}:\n"
                f"  {output_path}"
            )


# ============================================================
# CARGA DE INVENTARIOS
# ============================================================


def load_inventory(
    csv_path: Path,
    rundeck_header_list: list[tuple[str, int]],
    restrict_to_defined_columns: bool,
) -> LoadedInventory:
    """
    Lee un inventario CSV, resuelve las posiciones de sus columnas y descarta las
    filas con cantidad de campos inválida.

    Decisiones de diseño:
    - El inventario parseado solo contiene las columnas definidas en
      rundeck_header_list (`restrict_to_defined_columns=True`); el maestro puede
      traer columnas adicionales, por lo que se valida contra su propio header.
    - Un parseado con columnas adicionales casi seguro es el maestro pasado en el
      lugar equivocado: se aborta en lugar de descartar todas sus filas en silencio.
    """
    header_line, header_fields, rows = load_csv_data(csv_path)
    positions = resolve_positions(rundeck_header_list, header_fields, csv_path)
    if restrict_to_defined_columns and len(header_fields) != len(rundeck_header_list):
        error(
            f"El inventario parseado tiene {len(header_fields)} columnas pero "
            f"rundeck_header_list define {len(rundeck_header_list)}. "
            "Un inventario con columnas adicionales corresponde al maestro: "
            "verifique que los argumentos no estén invertidos.\n"
            f"  {csv_path}"
        )
    expected_columns = (
        len(rundeck_header_list) if restrict_to_defined_columns else len(header_fields)
    )
    return LoadedInventory(
        header_line,
        header_fields,
        positions,
        validate_rows(rows, expected_columns, csv_path),
    )


def build_merge_pairs(
    rundeck_header_list: list[tuple[str, int]],
    master_positions: dict[str, int],
    parsed_positions: dict[str, int],
) -> list[tuple[int, int]]:
    """
    Construye los pares (índice_maestro, índice_parseado) de las columnas fusionables.

    Solo las columnas con flag 1 se fusionan:
        flag 0 -> nunca se fusiona.
        flag 1 -> puede actualizarse desde el parseado.
        flag 2 -> clave de unión; nunca se sobrescribe.
    """
    return [
        (master_positions[name], parsed_positions[name])
        for name, flag in rundeck_header_list
        if flag == 1
    ]


def print_summary(
    defined_fields: int,
    master: LoadedInventory,
    key_name: str,
    key_parsed_idx: int,
    plan: MergePlan,
    parsed_duplicated_keys: set[str],
) -> None:
    """Imprime el resumen informativo previo a la fusión."""
    master_total_columns = len(master.header_fields)
    extra_columns = master_total_columns - defined_fields

    print(f"Columnas definidas en rundeck_header_list : {defined_fields}")
    print(f"Columnas totales en el maestro    : {master_total_columns}")

    if extra_columns > 0:
        print(f"Columnas adicionales del maestro  : {extra_columns}")

    print(f"Clave de unión                    : {key_name}")
    print(f"Posición de clave en el maestro   : {plan.key_master_idx + 1}")
    print(f"Posición de clave en el parseado  : {key_parsed_idx + 1}")
    print(f"Claves válidas en el parseado     : {len(plan.parsed_data)}")
    print(f"Claves duplicadas en el parseado  : {len(parsed_duplicated_keys)}")
    print(f"Claves duplicadas en el maestro   : {len(plan.master_duplicated_keys)}")


def warn_if_no_keys_match(master: LoadedInventory, plan: MergePlan) -> None:
    """
    Advierte si ninguna clave del maestro existe en el parseado.

    Decisiones de diseño:
    - No es un error (un maestro nuevo o sin colisiones es legítimo), pero es la
      señal visible de argumentos invertidos o de archivos que no corresponden.
    """
    if not master.rows:
        return

    has_match = any(
        extract_key(fields, plan.key_master_idx) in plan.parsed_data
        for fields in master.rows
    )
    if not has_match:
        print(
            "[WARNING] Ninguna clave del inventario maestro coincide con el "
            "inventario parseado. No se fusionará ninguna fila; verifique que los "
            "argumentos no estén invertidos.",
            file=sys.stderr,
        )


# ============================================================
# FUSIÓN DE FILAS
# ============================================================


def merge_row(
    master_fields: list[str],
    parsed_fields: list[str],
    merge_pairs: list[tuple[int, int]],
) -> list[str]:
    """
    Retorna una copia de la fila del maestro con las columnas fusionables
    sobrescritas por el valor parseado, salvo que este sea vacío o "N/A".
    """
    merged = list(master_fields)
    for master_idx, parsed_idx in merge_pairs:
        parsed_value = parsed_fields[parsed_idx]
        if is_empty_or_na(parsed_value):
            continue
        merged[master_idx] = parsed_value
    return merged


def resolve_output_row(
    line_number: int,
    master_fields: list[str],
    plan: MergePlan,
) -> list[str]:
    """
    Decide la fila de salida para una fila del maestro (comportamiento LEFT JOIN).

    La fila se conserva sin fusionar si su clave es inválida, está duplicada en el
    maestro o no existe en el parseado; solo se fusiona si la clave es válida y
    única en ambos inventarios.
    """
    key = extract_key(master_fields, plan.key_master_idx)

    if is_invalid_key(key) or key in plan.master_duplicated_keys:
        return master_fields

    parsed_fields = plan.parsed_data.get(key)
    if parsed_fields is None:
        print(
            f"[INFO] Línea {line_number} del inventario maestro: "
            f"la clave '{key}' no existe en el inventario "
            "parseado. La fila se conservará sin fusionar.",
            file=sys.stderr,
        )
        return master_fields

    return merge_row(master_fields, parsed_fields, plan.merge_pairs)


# ============================================================
# ESCRITURA ATÓMICA
# ============================================================


def write_merged_inventory(
    output_path: Path,
    master: LoadedInventory,
    plan: MergePlan,
) -> None:
    """
    Escribe el inventario fusionado de forma atómica.

    Decisiones de diseño:
    - Se escribe en un archivo temporal del mismo directorio que la salida para que
      `os.replace()` sea atómico; la salida definitiva solo se reemplaza si todo el
      procesamiento terminó correctamente.
    - Si el proceso falla antes del reemplazo, el temporal se elimina para no dejar
      residuos.
    """
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
            out_f.write(master.header_line + "\n")

            for line_number, fields in enumerate(master.rows, start=2):
                output_row = resolve_output_row(line_number, fields, plan)
                out_f.write(",".join(output_row) + "\n")

        os.replace(temporary_output_path, output_path)
        temporary_output_path = None

    finally:
        if temporary_output_path is not None:
            try:
                temporary_output_path.unlink()
            except FileNotFoundError:
                pass


# ============================================================
# MAIN
# ============================================================


def main() -> None:
    paths = parse_cli_paths(sys.argv[1:])
    validate_paths(paths)

    rundeck_header_list = load_rundeck_header_list(paths.rundeck_header_list)
    key_name = resolve_key_column(rundeck_header_list)

    parsed = load_inventory(
        paths.parsed, rundeck_header_list, restrict_to_defined_columns=True
    )
    master = load_inventory(
        paths.master, rundeck_header_list, restrict_to_defined_columns=False
    )

    key_parsed_idx = parsed.positions[key_name]
    parsed_data, parsed_duplicated_keys = build_parsed_data(
        parsed.rows, key_parsed_idx, parsed.positions
    )
    key_master_idx = master.positions[key_name]
    plan = MergePlan(
        key_master_idx=key_master_idx,
        parsed_data=parsed_data,
        master_duplicated_keys=find_master_duplicated_keys(
            master.rows, key_master_idx, master.positions
        ),
        merge_pairs=build_merge_pairs(
            rundeck_header_list, master.positions, parsed.positions
        ),
    )

    print_summary(
        len(rundeck_header_list),
        master,
        key_name,
        key_parsed_idx,
        plan,
        parsed_duplicated_keys,
    )

    warn_if_no_keys_match(master, plan)

    write_merged_inventory(paths.output, master, plan)

    print()
    print("[OK] Inventarios fusionados correctamente.")
    print(f"Inventario parseado : {paths.parsed}")
    print(f"Inventario maestro  : {paths.master}")
    print(f"Inventario salida   : {paths.output}")


if __name__ == "__main__":
    main()
