"""
prepare_master_inventory.py
============================
Convierte un inventario maestro en formato ODS/XLSX a CSV limpio,
validando que las columnas definidas en header_list.txt aparezcan
en el header del archivo de entrada con sus nombres exactos.

El orden de las columnas definidas en header_list.txt NO es
relevante. Pueden existir columnas adicionales en el archivo de
entrada; éstas se conservan sin tocar.

El script permite que el archivo de entrada tenga una fila inicial
de categorías agrupadas sobre el header real. Si una fila contiene
todos los nombres definidos en header_list.txt exactamente una vez,
esa fila se considera el header real y las filas anteriores se
descartan.

Si no existe una fila de categorías, la primera fila que contenga
todos los nombres definidos en header_list.txt se considera
directamente el header real.

Formatos de entrada soportados:
    .ods
    .xlsx

LibreOffice se utiliza para convertir el archivo de entrada a CSV
antes de realizar las validaciones y el procesamiento.

Uso:
    python3 prepare_master_inventory.py \
        master_inventory.(ods|xlsx) \
        [prepared_master_inventory.csv] \
        [header_list.txt]

Si no se indica la ruta de header_list.txt, se busca un archivo
llamado "header_list.txt" en el mismo directorio que este script.
"""

import csv
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from base_inventory import (
    clean_value,
    error,
    load_header_list,
    usage,
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
# CONVERTIR PLANILLA A CSV
# ============================================================

def convert_spreadsheet_to_csv(
    libreoffice: str,
    input_path: Path,
    temporary_directory: Path,
) -> Path:
    print()
    print("Convirtiendo archivo de entrada a CSV...")

    command = [
        libreoffice,
        "--headless",
        "--convert-to", "csv",
        "--outdir", str(temporary_directory),
        str(input_path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        error(
            "LibreOffice no pudo convertir el archivo de entrada.\n\n"
            f"stdout:\n{result.stdout}\n\n"
            f"stderr:\n{result.stderr}"
        )

    converted_csv = temporary_directory / f"{input_path.stem}.csv"

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
        f"Columnas encontradas en el inventario maestro: "
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
            "El header del inventario maestro contiene "
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
            "El header del inventario maestro contiene columnas "
            "duplicadas:\n"
            + "\n".join(f"  - {name}" for name in duplicated_headers)
        )

    print(
        f"Columnas de header_list.txt encontradas en el archivo: "
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
        usage(
            f"Uso: {sys.argv[0]} "
            "master_inventory.(ods|xlsx) "
            "[prepared_master_inventory.csv] "
            "[header_list.txt]"
        )

    if len(sys.argv) > 4:
        usage(
            f"Cantidad de argumentos inválida.\n"
            f"Uso: {sys.argv[0]} "
            "master_inventory.(ods|xlsx) "
            "[prepared_master_inventory.csv] "
            "[header_list.txt]"
        )

    # --------------------------------------------------------
    # Validar archivo de entrada.
    # --------------------------------------------------------
    input_path = Path(sys.argv[1])

    if not input_path.is_file():
        error(f"No existe el archivo de entrada:\n  {input_path}")

    supported_extensions = {".ods", ".xlsx"}

    if input_path.suffix.lower() not in supported_extensions:
        error(
            "El archivo de entrada debe ser un archivo ODS "
            "(.ods) o Excel (.xlsx):\n"
            f"  {input_path}"
        )

    try:
        input_size = input_path.stat().st_size
    except OSError as exc:
        error(
            "No se pudo obtener el tamaño del archivo de entrada:\n"
            f"  {input_path}\n"
            f"Motivo: {exc}"
        )

    if input_size == 0:
        error(
            f"El archivo de entrada está vacío (0 bytes):\n"
            f"  {input_path}"
        )

    # --------------------------------------------------------
    # Resolver ruta de salida.
    # --------------------------------------------------------
    output_path = (
        Path(sys.argv[2])
        if len(sys.argv) >= 3
        else Path(__file__).resolve().parent / "prepared_master_inventory.csv"
    )

    # --------------------------------------------------------
    # Validar colisión entre archivo de entrada y salida.
    # --------------------------------------------------------
    resolved_input = input_path.resolve()
    resolved_output = output_path.resolve()

    if resolved_input == resolved_output:
        error(
            "El archivo de salida no puede ser el mismo archivo "
            "que el archivo de entrada:\n"
            f"  {output_path}"
        )

    # --------------------------------------------------------
    # Resolver ruta de header_list.txt.
    # --------------------------------------------------------
    header_list_path = (
        Path(sys.argv[3])
        if len(sys.argv) >= 4
        else Path(__file__).resolve().parent / "header_list.txt"
    )

    header_list = load_header_list(header_list_path)

    # --------------------------------------------------------
    # Información.
    # --------------------------------------------------------
    print()
    print(f"Archivo de entrada       : {input_path}")
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

        converted_csv = convert_spreadsheet_to_csv(
            libreoffice,
            input_path,
            tmp_path,
        )

        print(f"CSV temporal generado    : {converted_csv.name}")

        # ----------------------------------------------------
        # Crear un segundo archivo temporal en el mismo
        # directorio que la salida definitiva.
        #
        # Esto permite reemplazar el archivo final mediante
        # os.replace() de forma atómica.
        # ----------------------------------------------------
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
            ) as temporary_output:
                temporary_output_path = Path(temporary_output.name)

            process_csv(
                converted_csv,
                temporary_output_path,
                header_list,
            )

            # ------------------------------------------------
            # El archivo temporal solo reemplaza la salida
            # definitiva si todo el procesamiento terminó
            # correctamente.
            # ------------------------------------------------
            os.replace(
                temporary_output_path,
                output_path,
            )
            temporary_output_path = None

        finally:
            # ------------------------------------------------
            # Si el proceso falló antes del reemplazo, eliminar
            # el archivo temporal para no dejar residuos.
            # ------------------------------------------------
            if temporary_output_path is not None:
                try:
                    temporary_output_path.unlink()
                except FileNotFoundError:
                    pass

    # --------------------------------------------------------
    # Resultado.
    # --------------------------------------------------------
    print()
    print("[OK] Proceso completado.")
    print()
    print(f"Archivo original : {input_path}")
    print(f"CSV generado     : {output_path}")


if __name__ == "__main__":
    main()