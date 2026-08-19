"""
parse_job_output.py
=====================
Normaliza la salida cruda del job de Rundeck a un CSV con el layout de
columnas definido en header_list.txt.

Cada línea válida del log debe contener campos en formato:

    CLAVE="VALOR",CLAVE="VALOR",...

Las claves deben corresponder exactamente con las entradas definidas
en header_list.txt.

El orden de las claves en la salida de Rundeck NO importa. Cada valor
se asigna a la columna correspondiente según el nombre de la clave
definido en header_list.txt.

El segundo valor de cada entrada de header_list.txt determina el
comportamiento de la columna:

    0 -> la columna queda vacía en el CSV de salida.
    1 -> la columna se procesa normalmente.
    2 -> la columna se procesa normalmente y corresponde a la clave
         utilizada para identificar la fila en el proceso de fusión.

Si una clave definida en header_list.txt tiene flag 1 o 2 pero no
aparece en una línea válida, su valor se establece como "N/A".

Las líneas que no correspondan a una fila válida son ignoradas.

Una clave que aparezca en la salida de Rundeck pero que NO exista
exactamente en header_list.txt provoca un error fatal y el script
termina con código de salida 1.

Uso:
    python3 parse_job_output.py \
        job_output.txt \
        [parsed_job_output.csv] \
        [header_list.txt] \

Si no se indica la ruta de header_list.txt, se busca un archivo
llamado "header_list.txt" en el mismo directorio que este script.
"""

import os
import sys
import tempfile
from pathlib import Path

from base_inventory import (
    error,
    load_header_list,
    split_quoted_csv_line,
    strip_quotes,
)


def csv_field(value: str) -> str:
    """Convierte un valor a un campo CSV válido.

    El campo siempre queda entre comillas dobles.
    Las comillas dobles internas se escapan duplicándolas.

    Ejemplos:
        hello -> "hello"
        say "hi" -> CSV con comillas duplicadas
        N/A -> "N/A"
    """
    value = value.replace('"', '""')
    return f'"{value}"'


def parse_key_value_field(field: str) -> tuple[str, str] | None:
    """
    Parsea un campo individual con formato:

        CLAVE="VALOR"

    Retorna:
        (clave, valor)

    o:
        None

    si el formato no es válido.

    La clave debe aparecer sin comillas y el valor debe estar
    delimitado por comillas dobles.
    """

    separator_index = field.find("=")

    if separator_index <= 0:
        return None

    key = field[:separator_index]
    value = field[separator_index + 1:]

    if not key:
        return None

    if len(value) < 2:
        return None

    if not (value.startswith('"') and value.endswith('"')):
        return None

    value = strip_quotes(value)

    return key, value


def main() -> None:
    if len(sys.argv) < 2:
        print(
            f"Uso: {sys.argv[0]} "
            "job_output.txt [parsed_job_output.csv] [header_list.txt]"
        )
        sys.exit(1)

    if len(sys.argv) > 4:
        error(
            f"Cantidad de argumentos inválida.\n"
            f"Uso: {sys.argv[0]} "
            "job_output.txt [parsed_job_output.csv] [header_list.txt]"
        )

    input_path = Path(sys.argv[1])
    output_path = (
        Path(sys.argv[2])
        if len(sys.argv) >= 3
        else Path(__file__).resolve().parent / "parsed_job_output.csv"
    )
    header_list_path = (
        Path(sys.argv[3])
        if len(sys.argv) >= 4
        else Path(__file__).resolve().parent / "header_list.txt"
    )

    if not input_path.is_file():
        error(f"No se encontró el archivo de entrada: {input_path}")

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
    # Cargar header_list: [(nombre, flag), ...]
    #
    # El flag determina si la columna se procesa:
    #
    #   0 -> columna vacía
    #   1 -> procesamiento normal
    #   2 -> procesamiento normal y clave de fusión
    #
    # El orden de header_list determina el orden de las
    # columnas del CSV de salida.
    # --------------------------------------------------------
    header_list = load_header_list(header_list_path)
    defined_fields = len(header_list)

    # --------------------------------------------------------
    # Construir mapas:
    #
    #     nombre_columna -> posición CSV
    #     nombre_columna -> flag
    #
    # Esto permite que el orden de las claves provenientes
    # de Rundeck sea completamente independiente del orden
    # de las columnas del CSV final.
    # --------------------------------------------------------
    header_positions = {
        name: index
        for index, (name, _flag) in enumerate(header_list)
    }

    header_flags = {
        name: flag
        for name, flag in header_list
    }

    # --------------------------------------------------------
    # Construir la línea de header del CSV de salida.
    # Los nombres vienen directamente de header_list, en orden.
    # --------------------------------------------------------
    header_line = ",".join(
        csv_field(name)
        for name, _flag in header_list
    )

    print("Header final:")
    print(header_line)
    print(f"Cantidad de columnas definidas: {defined_fields}")

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

            out_f.write(header_line + "\n")

            # ------------------------------------------------
            # Procesar el archivo de entrada línea a línea.
            #
            # Una línea válida debe:
            #
            #   - Contener campos separados por comas.
            #   - Cada campo debe tener formato CLAVE="VALOR".
            #   - Cada clave debe existir exactamente en header_list.
            #   - No puede repetirse una misma clave.
            #
            # Las claves con flag 0 quedan vacías.
            #
            # Las claves con flag 1 o 2 que no aparezcan en una línea
            # válida se rellenan automáticamente con "N/A".
            #
            # Una clave desconocida o una clave duplicada provoca
            # un error fatal y termina el script con exit 1.
            # ------------------------------------------------
            with input_path.open(
                "r",
                encoding="utf-8",
                errors="replace"
            ) as in_f:

                for line_number, raw_line in enumerate(in_f, start=1):
                    line = raw_line.rstrip("\r\n")

                    if not line:
                        continue

                    # ------------------------------------------------
                    # Ignorar líneas que claramente no corresponden
                    # a una fila de datos de Rundeck.
                    #
                    # Primero se intenta separar la línea como CSV.
                    # Si contiene al menos una clave conocida, se
                    # considera una posible fila de inventario.
                    # ------------------------------------------------
                    fields = split_quoted_csv_line(line)

                    if fields is None:
                        continue

                    is_inventory_line = False

                    for field in fields:
                        parsed = parse_key_value_field(field)

                        if parsed is not None:
                            key, _value = parsed

                            if key in header_positions:
                                is_inventory_line = True
                                break

                    if not is_inventory_line:
                        continue

                    # ------------------------------------------------
                    # Inicializar las columnas según su flag.
                    #
                    #   flag 0 -> vacío
                    #   flag 1 -> N/A
                    #   flag 2 -> N/A
                    #
                    # De esta forma, una clave con flag 0 nunca
                    # recibirá un valor desde la salida de Rundeck.
                    # ------------------------------------------------
                    output_values = [
                        "" if flag == 0 else "N/A"
                        for _name, flag in header_list
                    ]

                    seen_keys: set[str] = set()

                    # ------------------------------------------------
                    # Procesar cada campo CLAVE="VALOR".
                    # ------------------------------------------------
                    for field in fields:

                        parsed = parse_key_value_field(field)

                        if parsed is None:
                            error(
                                f"Línea {line_number}: "
                                f"campo inválido: {field}"
                            )

                        key, value = parsed

                        # ------------------------------------------------
                        # La clave debe coincidir EXACTAMENTE con una
                        # entrada de header_list.txt.
                        #
                        # Si no existe, es un error fatal.
                        # ------------------------------------------------
                        if key not in header_positions:
                            error(
                                f"Línea {line_number}: "
                                f"clave no definida en header_list.txt: "
                                f"'{key}'"
                            )

                        # ------------------------------------------------
                        # No permitir claves duplicadas dentro de la
                        # misma fila.
                        # ------------------------------------------------
                        if key in seen_keys:
                            error(
                                f"Línea {line_number}: "
                                f"clave duplicada: '{key}'"
                            )

                        seen_keys.add(key)

                        # ------------------------------------------------
                        # Si la columna tiene flag 0, se ignora el valor
                        # recibido desde Rundeck y permanece vacía.
                        # ------------------------------------------------
                        if header_flags[key] == 0:
                            continue

                        # ------------------------------------------------
                        # Si la columna tiene flag 1 o 2, colocar el
                        # valor en la posición correspondiente según
                        # header_list.txt.
                        # ------------------------------------------------
                        position = header_positions[key]
                        output_values[position] = value

                    # ------------------------------------------------
                    # Escribir la fila CSV final.
                    #
                    # Todos los valores se escriben como campos CSV
                    # verdaderos, independientemente de cómo hayan
                    # llegado desde Rundeck.
                    # ------------------------------------------------
                    out_f.write(
                        ",".join(
                            csv_field(value)
                            for value in output_values
                        )
                        + "\n"
                    )

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

    print()
    print("[OK] Proceso completado.")
    print(f"Log de entrada : {input_path}")
    print(f"CSV de salida  : {output_path}")


if __name__ == "__main__":
    main()