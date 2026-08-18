"""
prepare_master_inventory.py
============================
Convierte un inventario padre en formato ODS a CSV limpio,
validando que las columnas definidas en header_list.txt aparezcan
en el header del ODS con sus nombres exactos.

El orden de las columnas definidas en header_list.txt NO es
relevante. Pueden existir columnas adicionales en el ODS; éstas
se conservan sin tocar.

Uso:
    python3 prepare_master_inventory.py master_inventory.ods [header_list.txt]

Si no se indica la ruta de header_list.txt, se busca un archivo
llamado "header_list.txt" en el mismo directorio que este script.

El CSV de salida se genera automáticamente en el mismo directorio
del ODS, utilizando el mismo nombre y cambiando la extensión a .csv.
"""

import csv
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from base_inventory import (
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

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
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
    - El orden de los nombres NO importa.
    - No se permiten nombres de header_list.txt que falten en el ODS.
    - Las columnas adicionales del ODS se conservan sin modificar.
    - El FLAG asociado a cada columna de header_list.txt no afecta
      este proceso; solamente se utiliza el nombre de la columna
      para validarla.
    """
    print()
    print("Procesando CSV...")

    # --------------------------------------------------------
    # Leer todas las filas del CSV.
    # --------------------------------------------------------
    try:
        file = source_csv.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        )
    except OSError as exc:
        error(
            f"No se pudo abrir el CSV generado por LibreOffice:\n"
            f"  {source_csv}\n"
            f"Motivo: {exc}"
        )

    try:
        with file:
            reader = csv.reader(
                file,
                delimiter=",",
                quotechar='"',
            )
            rows = list(reader)
    except UnicodeDecodeError as exc:
        error(
            f"El CSV generado por LibreOffice contiene "
            f"datos con una codificación inválida:\n"
            f"  {source_csv}\n"
            f"Motivo: {exc}"
        )
    except csv.Error as exc:
        error(
            f"Error al interpretar el formato CSV generado "
            f"por LibreOffice:\n"
            f"  {source_csv}\n"
            f"Motivo: {exc}"
        )

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
    # del ODS.
    #
    # El orden NO importa.
    # --------------------------------------------------------
    source_header_names = set(source_header)

    missing = [
        name
        for name, _flag in header_list
        if name not in source_header_names
    ]

    if missing:
        error(
            "El header del inventario padre no contiene las "
            "siguientes columnas definidas en header_list.txt:\n"
            + "\n".join(f"  - {name}" for name in missing)
        )

    # --------------------------------------------------------
    # Validar que no existan columnas duplicadas en el header
    # del ODS, ya que producirían ambigüedad para el pipeline.
    # --------------------------------------------------------
    seen_headers: set[str] = set()
    duplicated_headers: list[str] = []

    for name in source_header:
        if name in seen_headers and name not in duplicated_headers:
            duplicated_headers.append(name)
        seen_headers.add(name)

    if duplicated_headers:
        error(
            "El header del inventario padre contiene columnas "
            "duplicadas:\n"
            + "\n".join(f"  - {name}" for name in duplicated_headers)
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
    if len(sys.argv) < 2:
        print(
            f"Uso: {sys.argv[0]} "
            "inventario.ods [header_list.txt]"
        )
        sys.exit(1)

    if len(sys.argv) > 3:
        print(
            f"Uso: {sys.argv[0]} "
            "inventario.ods [header_list.txt]"
        )
        sys.exit(1)

    # --------------------------------------------------------
    # Validar ODS.
    # --------------------------------------------------------
    ods_path = Path(sys.argv[1])

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
    header_list_path = (
        Path(sys.argv[2])
        if len(sys.argv) >= 3
        else Path(__file__).resolve().parent / "header_list.txt"
    )

    header_list = load_header_list(header_list_path)

    # --------------------------------------------------------
    # Resolver ruta de salida.
    # --------------------------------------------------------
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

        converted_csv = convert_ods_to_csv(
            libreoffice,
            ods_path,
            tmp_path,
        )

        print(f"CSV temporal generado    : {converted_csv.name}")

        process_csv(
            converted_csv,
            output_path,
            header_list,
        )

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