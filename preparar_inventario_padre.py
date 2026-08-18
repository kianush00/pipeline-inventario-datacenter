"""
preparar_inventario_padre.py
============================
Convierte un inventario padre en formato ODS a CSV limpio,
validando que las columnas definidas en header_list.txt aparezcan
en el header del ODS con sus nombres exactos y en el mismo orden
relativo (pueden existir columnas intermedias no definidas en
header_list.txt; éstas se conservan sin tocar).

Uso:
    python3 preparar_inventario_padre.py inventario.ods \\
        [-H header_list.txt] [-o salida.csv]

Si no se indican -H ni -o, se busca header_list.txt junto al
script y se genera el CSV con el mismo nombre del ODS.
"""

import argparse
import csv
import shutil
import subprocess
import tempfile
from pathlib import Path

from inventario_base import (
    clean_value,
    error,
    load_header_list,
)

# ============================================================
# BUSCAR LIBREOFFICE
# ============================================================

def find_libreoffice() -> str:
    for executable in ("libreoffice", "soffice"):
        path = shutil.which(executable)
        if path:
            return path
    error(
        "No se encontró LibreOffice.\n"
        "Se necesita 'libreoffice' o 'soffice' en el PATH."
    )
    return ""  # nunca se alcanza; satisface al type-checker


# ============================================================
# CONVERTIR ODS A CSV
# ============================================================

def convert_ods_to_csv(
    libreoffice: str,
    ods_path: Path,
    temporary_directory: Path,
) -> Path:
    print()
    print("Convirtiendo ODS a CSV...")

    command = [
        libreoffice,
        "--headless",
        "--convert-to", "csv",
        "--outdir", str(temporary_directory),
        str(ods_path),
    ]

    result = subprocess.run(  # noqa: PLW1510
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        error(
            "LibreOffice no pudo convertir el ODS.\n\n"
            f"stdout:\n{result.stdout}\n\n"
            f"stderr:\n{result.stderr}"
        )

    converted_csv = temporary_directory / f"{ods_path.stem}.csv"

    if not converted_csv.is_file():
        error(
            "LibreOffice terminó sin error, pero no se encontró "
            f"el CSV generado:\n  {converted_csv}"
        )

    return converted_csv


# ============================================================
# VALIDAR HEADER Y PROCESAR CSV
# ============================================================

def process_csv(
    source_csv: Path,
    output_csv: Path,
    header_list: list[tuple[str, int]],
) -> None:
    """
    Lee el CSV generado por LibreOffice, valida el header contra
    header_list y escribe el CSV de salida limpio.

    Reglas de validación del header:
    - La fila 1 del ODS se descarta (contiene valores anidados).
    - La fila 2 es el header real.
    - Cada nombre definido en header_list.txt debe aparecer en el
      header del ODS con nombre exacto (comparación sensible a
      mayúsculas/minúsculas y espacios).
    - Los nombres deben aparecer en el mismo orden relativo que
      en header_list.txt; puede haber columnas intermedias en el
      ODS que no estén en header_list.txt (se conservan tal cual).
    - No se permiten nombres de header_list.txt que falten en el
      ODS ni que estén fuera de orden.
    """
    print()
    print("Procesando CSV...")

    # --------------------------------------------------------
    # Leer todas las filas del CSV.
    # --------------------------------------------------------
    try:
        with source_csv.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.reader(file, delimiter=",", quotechar='"')
            rows = list(reader)
    except Exception as exc:
        error(f"No se pudo leer el CSV generado por LibreOffice: {exc}")

    # --------------------------------------------------------
    # Validar cantidad mínima de filas.
    # --------------------------------------------------------
    if len(rows) < 2:
        error(
            "El inventario no contiene suficientes filas.\n"
            "Se esperaba al menos:\n"
            "  fila 1 → fila que será eliminada (valores anidados)\n"
            "  fila 2 → header real"
        )

    # --------------------------------------------------------
    # Descartar la primera fila (valores anidados del ODS).
    # --------------------------------------------------------
    rows = rows[1:]

    source_header = rows[0]
    print(f"Columnas encontradas en el inventario padre: {len(source_header)}")

    # --------------------------------------------------------
    # Validar que cada nombre de header_list esté en el header
    # del ODS, con nombre exacto y en el mismo orden relativo.
    #
    # Se recorre el header del ODS de izquierda a derecha,
    # avanzando un "cursor" sobre header_list cada vez que se
    # encuentra una coincidencia.
    # --------------------------------------------------------
    cursor = 0                        # posición actual en header_list
    required = header_list            # [(nombre, flag), ...]
    ods_positions: dict[str, int] = {}  # nombre -> índice 0-based en source_header

    for col_index, col_name in enumerate(source_header):
        if cursor >= len(required):
            break  # ya encontramos todos los nombres requeridos
        if col_name == required[cursor][0]:
            ods_positions[required[cursor][0]] = col_index
            cursor += 1

    # ¿Quedaron nombres sin encontrar?
    if cursor < len(required):
        missing = [name for name, _flag in required[cursor:]]
        error(
            "El header del inventario padre no contiene las "
            "siguientes columnas definidas en header_list.txt "
            "(o están fuera de orden):\n"
            + "\n".join(f"  - {m}" for m in missing)
        )

    print(
        f"Columnas de header_list.txt encontradas en el ODS: "
        f"{len(header_list)}"
    )

    # --------------------------------------------------------
    # Escribir CSV de salida.
    #
    # El header se copia tal como viene del ODS (sin modificar).
    # Los valores de cada fila se limpian (se eliminan saltos de
    # línea internos). QUOTE_ALL garantiza que todos los campos
    # queden entrecomillados.
    # --------------------------------------------------------
    try:
        with output_csv.open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(
                file,
                delimiter=",",
                quotechar='"',
                quoting=csv.QUOTE_ALL,
                lineterminator="\n",
            )

            # Header: limpiar y escribir.
            writer.writerow(
                [clean_value(v) for v in source_header]
            )

            # Datos: rows[0] es el header; procesamos rows[1:].
            for row_number, row in enumerate(rows[1:], start=3):
                if len(row) != len(source_header):
                    error(
                        f"La fila {row_number} contiene {len(row)} columnas, "
                        f"pero se esperaban {len(source_header)}."
                    )
                writer.writerow([clean_value(v) for v in row])

    except OSError as exc:
        error(f"No se pudo escribir el CSV de salida: {exc}")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Convierte un inventario padre ODS a CSV, "
            "valida que los headers definidos en header_list.txt "
            "estén presentes en orden, conserva todas las columnas "
            "y elimina saltos de línea internos."
        )
    )
    parser.add_argument(
        "ods",
        type=Path,
        help="Archivo ODS del inventario padre.",
    )
    parser.add_argument(
        "-H", "--header-list",
        type=Path,
        default=None,
        help=(
            "Archivo header_list.txt. "
            "Por defecto: header_list.txt junto al script."
        ),
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help=(
            "CSV de salida. "
            "Por defecto: mismo nombre del ODS con extensión .csv."
        ),
    )

    args = parser.parse_args()

    # --------------------------------------------------------
    # Validar ODS.
    # --------------------------------------------------------
    ods_path = args.ods.resolve()

    if not ods_path.is_file():
        error(f"No existe el archivo ODS:\n  {ods_path}")

    if ods_path.suffix.lower() != ".ods":
        error(
            f"El archivo de entrada no parece ser un ODS:\n"
            f"  {ods_path}"
        )

    # --------------------------------------------------------
    # Resolver ruta de header_list.txt.
    # --------------------------------------------------------
    if args.header_list is not None:
        header_list_path = args.header_list.resolve()
    else:
        header_list_path = Path(__file__).resolve().parent / "header_list.txt"

    header_list = load_header_list(header_list_path)

    # --------------------------------------------------------
    # Resolver ruta de salida.
    # --------------------------------------------------------
    if args.output is not None:
        output_path = args.output.resolve()
    else:
        output_path = ods_path.parent / f"{ods_path.stem}.csv"

    # --------------------------------------------------------
    # Información.
    # --------------------------------------------------------
    print()
    print(f"Archivo ODS              : {ods_path}")
    print(f"header_list.txt          : {header_list_path}")
    print(f"Columnas en header_list  : {len(header_list)}")
    print(f"CSV de salida            : {output_path}")

    # --------------------------------------------------------
    # Buscar LibreOffice.
    # --------------------------------------------------------
    libreoffice = find_libreoffice()

    # --------------------------------------------------------
    # Convertir y procesar.
    # --------------------------------------------------------
    with tempfile.TemporaryDirectory(prefix="inventario_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        converted_csv = convert_ods_to_csv(libreoffice, ods_path, tmp_path)
        print(f"CSV temporal generado    : {converted_csv.name}")

        process_csv(converted_csv, output_path, header_list)

    # --------------------------------------------------------
    # Resultado.
    # --------------------------------------------------------
    print()
    print("[OK] Proceso completado.")
    print()
    print(f"ODS original : {ods_path}")
    print(f"CSV generado : {output_path}")


if __name__ == "__main__":
    main()
