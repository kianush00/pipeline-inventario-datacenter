"""
prepare_master_inventory.py
============================
Convierte un inventario padre en formato ODS a CSV limpio,
validando que las columnas definidas en header_list.txt aparezcan
en el header del ODS con sus nombres exactos.

El orden de las columnas definidas en header_list.txt NO es
relevante. Pueden existir columnas adicionales en el ODS; éstas
se conservan sin tocar.

El script permite que el ODS tenga una fila inicial de categorías
agrupadas sobre el header real. Si una fila contiene todos los
nombres definidos en header_list.txt exactamente una vez, esa fila
se considera el header real y las filas anteriores se descartan.

Si no existe una fila de categorías, la primera fila que contenga
todos los nombres definidos en header_list.txt se considera
directamente el header real.

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
    return ""


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
# BUSCAR HEADER REAL
# ============================================================

def find_real_header(
    rows: list[list[str]],
    header_list: list[tuple[str, int]],
) -> tuple[int, list[str]]:
    """
    Busca la primera fila que contenga todos los nombres definidos
    en header_list.txt exactamente una vez.

    Puede haber columnas adicionales que no estén definidas en
    header_list.txt.

    Retorna:
        (índice_0based_de_la_fila, header)

    Si ninguna fila contiene todos los nombres requeridos, aborta.
    """
    required_names = [name for name, _flag in header_list]
    required_set = set(required_names)

    for row_index, row in enumerate(rows):
        row_names = set(row)

        if not required_set.issubset(row_names):
            continue

        duplicated_required = [
            name
            for name in required_names
            if row.count(name) != 1
        ]

        if duplicated_required:
            error(
                "La fila candidata a header contiene columnas "
                "definidas en header_list.txt duplicadas:\n"
                + "\n".join(
                    f"  - {name}"
                    for name in duplicated_required
                )
                + f"\nFila del CSV temporal: {row_index + 1}"
            )

        return row_index, row

    error(
        "No se encontró ninguna fila que contenga todos los "
        "nombres de columnas definidos en header_list.txt "
        "exactamente una vez."
    )
    return -1, []


# ============================================================
# VALIDAR HEADER Y PROCESAR CSV
# ============================================================

def process_csv(
    source_csv: Path,
    output_csv: Path,
    header_list: list[tuple[str, int]],
) -> None:
    """
    Lee el CSV generado por LibreOffice, detecta el header real
    mediante header_list.txt y escribe el CSV de salida limpio.

    Reglas de validación del header:
    - Puede existir una fila inicial de categorías agrupadas.
    - La fila real se identifica buscando todos los nombres de
      header_list.txt exactamente.
    - El orden de los nombres NO importa.
    - Todos los nombres definidos en header_list.txt deben aparecer
      exactamente una vez en el header real.
    - Pueden existir columnas adicionales no definidas en
      header_list.txt; éstas se conservan.
    - Las columnas sin nombre se reportan como un problema.
    - No se permiten columnas duplicadas en el header real.
    - El FLAG asociado a cada columna de header_list.txt no afecta
      este proceso.
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
    if not rows:
        error(
            "El inventario no contiene ninguna fila."
        )

    # --------------------------------------------------------
    # Buscar el header real.
    #
    # No se asume que la primera fila sea el header.
    # Puede existir una fila previa de categorías.
    # --------------------------------------------------------
    header_row_index, source_header = find_real_header(
        rows,
        header_list,
    )

    discarded_rows = header_row_index

    print(
        f"Fila del header real en el CSV temporal: "
        f"{header_row_index + 1}"
    )

    if discarded_rows > 0:
        print(
            f"Filas descartadas antes del header: "
            f"{discarded_rows}"
        )

    print(
        f"Columnas encontradas en el inventario padre: "
        f"{len(source_header)}"
    )

    # --------------------------------------------------------
    # Validar columnas sin nombre.
    # --------------------------------------------------------
    unnamed_columns = [
        index + 1
        for index, name in enumerate(source_header)
        if name == ""
    ]

    if unnamed_columns:
        error(
            "El header del inventario padre contiene "
            "columnas sin nombre en las siguientes posiciones:\n"
            + "\n".join(
                f"  - posición {position}"
                for position in unnamed_columns
            )
        )

    # --------------------------------------------------------
    # Validar que no existan columnas duplicadas en el header.
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
    # Se conserva el header real y todas las columnas adicionales.
    # Las filas anteriores al header real se descartan.
    # --------------------------------------------------------
    try:
        with output_csv.open(
            "w",
            encoding="utf-8",
            newline="",
        ) as file:
            writer = csv.writer(
                file,
                delimiter=",",
                quotechar='"',
                quoting=csv.QUOTE_ALL,
                lineterminator="\n",
            )

            writer.writerow(
                [clean_value(v) for v in source_header]
            )

            # ------------------------------------------------
            # Los datos comienzan inmediatamente después del
            # header real.
            #
            # La numeración se deriva de header_row_index, por
            # lo que corresponde siempre a la fila original del
            # CSV generado por LibreOffice.
            # ------------------------------------------------
            for row_index in range(
                header_row_index + 1,
                len(rows),
            ):
                row = rows[row_index]
                row_number = row_index + 1

                if len(row) != len(source_header):
                    error(
                        f"La fila {row_number} contiene "
                        f"{len(row)} columnas, pero se esperaban "
                        f"{len(source_header)}."
                    )

                writer.writerow([clean_value(v) for v in row])

    except OSError as exc:
        error(
            f"No se pudo escribir el CSV de salida:\n"
            f"  {output_csv}\n"
            f"Motivo: {exc}"
        )
    except csv.Error as exc:
        error(
            f"Error al escribir el CSV de salida:\n"
            f"  {output_csv}\n"
            f"Motivo: {exc}"
        )


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

    try:
        ods_size = ods_path.stat().st_size
    except OSError as exc:
        error(
            f"No se pudo obtener el tamaño del archivo ODS:\n"
            f"  {ods_path}\n"
            f"Motivo: {exc}"
        )

    if ods_size == 0:
        error(
            f"El archivo ODS está vacío (0 bytes):\n"
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