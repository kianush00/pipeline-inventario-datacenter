"""
prepare_master_inventory.py
============================
Converts a master inventory in ODS/XLSX format into a clean CSV,
validating that the columns defined in rundeck_header_list.txt appear
in the header of the input file with their exact names.

The order of the columns defined in rundeck_header_list.txt is NOT
relevant. The input file may contain additional columns;
these are kept untouched.

The script allows the input file to have an initial row of
grouped categories above the real header. If a row contains
all the names defined in rundeck_header_list.txt exactly once,
that row is considered the real header and the previous rows are
discarded.

If there is no category row, the first row that contains
all the names defined in rundeck_header_list.txt is considered
the real header directly.

Supported input formats:
    .ods
    .xlsx

LibreOffice is used to convert the input file to CSV
before performing the validations and the processing.

Usage:
    python -m inventory_pipeline.prepare_master_inventory \
        master_inventory.(ods|xlsx) \
        [prepared_master_inventory.csv] \
        [rundeck_header_list.txt]

If the output is not given, output/prepared_master_inventory.csv is written.
If the rundeck_header_list.txt path is not given,
config/rundeck_header_list.txt is used (see inventory_pipeline.paths).
"""

import csv
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from inventory_pipeline.base_inventory import (
    clean_value,
    error,
    load_rundeck_header_list,
    usage,
)
from inventory_pipeline.paths import (
    DEFAULT_HEADER_LIST_PATH,
    OUTPUT_DIR,
    PREPARED_CSV_NAME,
)

# ============================================================
# FIND LIBREOFFICE
# ============================================================


def find_libreoffice() -> str:
    for executable in ("libreoffice", "soffice"):
        path = shutil.which(executable)
        if path:
            return path
    error(
        "LibreOffice was not found.\n'libreoffice' or 'soffice' is required in the PATH."
    )
    return ""


# ============================================================
# CONVERT SPREADSHEET TO CSV
# ============================================================


def convert_spreadsheet_to_csv(
    libreoffice: str,
    input_path: Path,
    temporary_directory: Path,
) -> Path:
    print()
    print("Converting input file to CSV...")

    command = [
        libreoffice,
        "--headless",
        "--convert-to",
        "csv",
        "--outdir",
        str(temporary_directory),
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
            "LibreOffice could not convert the input file.\n\n"
            f"stdout:\n{result.stdout}\n\n"
            f"stderr:\n{result.stderr}"
        )

    converted_csv = temporary_directory / f"{input_path.stem}.csv"

    if not converted_csv.is_file():
        error(
            "LibreOffice finished without error, but the generated "
            f"CSV was not found:\n  {converted_csv}"
        )

    return converted_csv


# ============================================================
# FIND REAL HEADER
# ============================================================


def find_real_header(
    rows: list[list[str]],
    rundeck_header_list: list[tuple[str, int]],
) -> tuple[int, list[str]]:
    """
    Finds the first row that contains all the names defined
    in rundeck_header_list.txt exactly once.

    There may be additional columns that are not defined in
    rundeck_header_list.txt.

    Returns:
        (0based_row_index, header)

    If no row contains all the required names, it aborts.
    """
    required_names = [name for name, _flag in rundeck_header_list]
    required_set = set(required_names)

    for row_index, row in enumerate(rows):
        row_names = set(row)

        if not required_set.issubset(row_names):
            continue

        duplicated_required = [name for name in required_names if row.count(name) != 1]

        if duplicated_required:
            error(
                "The candidate header row contains duplicate columns "
                "defined in rundeck_header_list.txt:\n"
                + "\n".join(f"  - {name}" for name in duplicated_required)
                + f"\nRow of the temporary CSV: {row_index + 1}"
            )

        return row_index, row

    error(
        "No row was found that contains all the "
        "column names defined in rundeck_header_list.txt "
        "exactly once."
    )
    return -1, []


# ============================================================
# VALIDATE HEADER AND PROCESS CSV
# ============================================================


def process_csv(
    source_csv: Path,
    output_csv: Path,
    rundeck_header_list: list[tuple[str, int]],
) -> None:
    """
    Reads the CSV generated by LibreOffice, detects the real header
    through rundeck_header_list.txt and writes the clean output CSV.

    Header validation rules:
    - There may be an initial row of grouped categories.
    - The real row is identified by looking for all the names of
      rundeck_header_list.txt exactly.
    - The order of the names does NOT matter.
    - All the names defined in rundeck_header_list.txt must appear
      exactly once in the real header.
    - There may be additional columns not defined in
      rundeck_header_list.txt; these are kept.
    - Unnamed columns are reported as a problem.
    - Duplicate columns are not allowed in the real header.
    - The FLAG associated with each rundeck_header_list.txt column does
      not affect this process.
    """
    print()
    print("Processing CSV...")

    # --------------------------------------------------------
    # Read all the rows of the CSV.
    # --------------------------------------------------------
    try:
        file = source_csv.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        )
    except OSError as exc:
        error(
            f"Could not open the CSV generated by LibreOffice:\n"
            f"  {source_csv}\n"
            f"Reason: {exc}"
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
            f"The CSV generated by LibreOffice contains "
            f"data with an invalid encoding:\n"
            f"  {source_csv}\n"
            f"Reason: {exc}"
        )
    except csv.Error as exc:
        error(
            f"Error while interpreting the CSV format generated "
            f"by LibreOffice:\n"
            f"  {source_csv}\n"
            f"Reason: {exc}"
        )

    # --------------------------------------------------------
    # Validate the minimum number of rows.
    # --------------------------------------------------------
    if not rows:
        error("The inventory does not contain any rows.")

    # --------------------------------------------------------
    # Find the real header.
    #
    # The first row is not assumed to be the header.
    # There may be a previous category row.
    # --------------------------------------------------------
    header_row_index, source_header = find_real_header(
        rows,
        rundeck_header_list,
    )

    discarded_rows = header_row_index

    print(f"Row of the real header in the temporary CSV: {header_row_index + 1}")

    if discarded_rows > 0:
        print(f"Rows discarded before the header: {discarded_rows}")

    print(f"Columns found in the master inventory: {len(source_header)}")

    # --------------------------------------------------------
    # Validate unnamed columns.
    # --------------------------------------------------------
    unnamed_columns = [
        index + 1 for index, name in enumerate(source_header) if name == ""
    ]

    if unnamed_columns:
        error(
            "The master inventory header contains "
            "unnamed columns at the following positions:\n"
            + "\n".join(f"  - position {position}" for position in unnamed_columns)
        )

    # --------------------------------------------------------
    # Validate that there are no duplicate columns in the header.
    # --------------------------------------------------------
    seen_headers: set[str] = set()
    duplicated_headers: list[str] = []

    for name in source_header:
        if name in seen_headers and name not in duplicated_headers:
            duplicated_headers.append(name)
        seen_headers.add(name)

    if duplicated_headers:
        error(
            "The master inventory header contains duplicate "
            "columns:\n" + "\n".join(f"  - {name}" for name in duplicated_headers)
        )

    print(
        f"rundeck_header_list.txt columns found in the file: {len(rundeck_header_list)}"
    )

    # --------------------------------------------------------
    # Write the output CSV.
    #
    # The real header and all the additional columns are kept.
    # The rows before the real header are discarded.
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

            writer.writerow([clean_value(v) for v in source_header])

            # ------------------------------------------------
            # The data starts immediately after the
            # real header.
            #
            # The numbering is derived from header_row_index, so
            # it always corresponds to the original row of the
            # CSV generated by LibreOffice.
            # ------------------------------------------------
            for row_index in range(
                header_row_index + 1,
                len(rows),
            ):
                row = rows[row_index]
                row_number = row_index + 1

                if len(row) != len(source_header):
                    error(
                        f"Row {row_number} contains "
                        f"{len(row)} columns, but "
                        f"{len(source_header)} were expected."
                    )

                writer.writerow([clean_value(v) for v in row])

    except OSError as exc:
        error(f"Could not write the output CSV:\n  {output_csv}\nReason: {exc}")
    except csv.Error as exc:
        error(f"Error while writing the output CSV:\n  {output_csv}\nReason: {exc}")


# ============================================================
# MAIN
# ============================================================


def main() -> None:
    if len(sys.argv) < 2:
        usage(
            f"Usage: {sys.argv[0]} "
            "master_inventory.(ods|xlsx) "
            "[prepared_master_inventory.csv] "
            "[rundeck_header_list.txt]"
        )

    if len(sys.argv) > 4:
        usage(
            f"Invalid number of arguments.\n"
            f"Usage: {sys.argv[0]} "
            "master_inventory.(ods|xlsx) "
            "[prepared_master_inventory.csv] "
            "[rundeck_header_list.txt]"
        )

    # --------------------------------------------------------
    # Validate the input file.
    # --------------------------------------------------------
    input_path = Path(sys.argv[1])

    if not input_path.is_file():
        error(f"The input file does not exist:\n  {input_path}")

    supported_extensions = {".ods", ".xlsx"}

    if input_path.suffix.lower() not in supported_extensions:
        error(
            "The input file must be an ODS (.ods) "
            "or Excel (.xlsx) file:\n"
            f"  {input_path}"
        )

    try:
        input_size = input_path.stat().st_size
    except OSError as exc:
        error(
            f"Could not get the size of the input file:\n  {input_path}\nReason: {exc}"
        )

    if input_size == 0:
        error(f"The input file is empty (0 bytes):\n  {input_path}")

    # --------------------------------------------------------
    # Resolve the output path.
    # --------------------------------------------------------
    output_path = (
        Path(sys.argv[2]) if len(sys.argv) >= 3 else OUTPUT_DIR / PREPARED_CSV_NAME
    )

    # --------------------------------------------------------
    # Validate collision between the input and output files.
    # --------------------------------------------------------
    resolved_input = input_path.resolve()
    resolved_output = output_path.resolve()

    if resolved_input == resolved_output:
        error(
            "The output file cannot be the same file "
            "as the input file:\n"
            f"  {output_path}"
        )

    # --------------------------------------------------------
    # Resolve the rundeck_header_list.txt path.
    # --------------------------------------------------------
    rundeck_header_list_path = (
        Path(sys.argv[3]) if len(sys.argv) >= 4 else DEFAULT_HEADER_LIST_PATH
    )

    rundeck_header_list = load_rundeck_header_list(rundeck_header_list_path)

    # --------------------------------------------------------
    # Information.
    # --------------------------------------------------------
    print()
    print(f"Input file                       : {input_path}")
    print(f"rundeck_header_list.txt          : {rundeck_header_list_path}")
    print(f"Columns in rundeck_header_list   : {len(rundeck_header_list)}")
    print(f"Output CSV                       : {output_path}")

    # --------------------------------------------------------
    # Find LibreOffice.
    # --------------------------------------------------------
    libreoffice = find_libreoffice()

    # --------------------------------------------------------
    # Convert and process.
    # --------------------------------------------------------
    with tempfile.TemporaryDirectory(prefix="inventory_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        converted_csv = convert_spreadsheet_to_csv(
            libreoffice,
            input_path,
            tmp_path,
        )

        print(f"Temporary CSV generated          : {converted_csv.name}")

        # ----------------------------------------------------
        # Create a second temporary file in the same
        # directory as the final output.
        #
        # This allows replacing the final file through
        # os.replace() atomically.
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
                rundeck_header_list,
            )

            # ------------------------------------------------
            # The temporary file only replaces the final
            # output if all the processing finished
            # successfully.
            # ------------------------------------------------
            os.replace(
                temporary_output_path,
                output_path,
            )
            temporary_output_path = None

        finally:
            # ------------------------------------------------
            # If the process failed before the replacement, remove
            # the temporary file so no leftovers remain.
            # ------------------------------------------------
            if temporary_output_path is not None:
                try:
                    temporary_output_path.unlink()
                except FileNotFoundError:
                    pass

    # --------------------------------------------------------
    # Result.
    # --------------------------------------------------------
    print()
    print("[OK] Process completed.")
    print()
    print(f"Original file : {input_path}")
    print(f"Generated CSV : {output_path}")


if __name__ == "__main__":
    main()
