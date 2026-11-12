"""
update_master_inventory.py
=============================
Last step of the ETL pipeline: it takes the merged inventory
(merged_inventory.csv, output of merge_inventories.py) and
lays it over a COPY of the master spreadsheet (.ods/.xlsx),
without ever modifying the original master file.

The script:
1. Creates a physical copy of the master spreadsheet.
2. Locates, within the master, the row that corresponds to the real
   header, looking for the first row whose content (in the same
   order) matches EXACTLY the header of
   merged_inventory.csv. This simultaneously validates that the
   column names and their positions are identical between both
   files, without needing rundeck_header_list.txt.
3. Validates that the number of data rows of the master (starting
   from the detected header) matches exactly the number of
   rows of merged_inventory.csv. If it does not match, it aborts: it
   is not safe to assume positional correspondence between both files.
4. Compares cell by cell (with numeric normalization, so that
   format differences such as "16" vs "16.0" are not reported as if
   they were real changes) and overwrites only the cells whose
   value changed.
5. Reports which columns changed and, for each one, which specific
   cells were modified.

Editing mechanism:
- .xlsx: the file is copied and edited directly with openpyxl,
  preserving the original format.
- .ods: since editing individual cells of an .ods robustly
  requires manually handling the compression of repeated cells/rows
  of the ODF format (number-columns-repeated /
  number-rows-repeated attributes), and the pipeline already depends on
  headless LibreOffice, a round-trip is used: .ods -> .xlsx (LibreOffice) ->
  editing with openpyxl -> .xlsx -> .ods (LibreOffice).

The whole process is atomic: the work is done on a temporary
file in the same output directory, and the final file is only
replaced through os.replace() if the whole process finished without
errors.

Usage:
    python -m inventory_pipeline.update_master_inventory \
        --merged merged_inventory.csv \
        --master master_inventory.(ods|xlsx) \
        [--output master_inventory_updated.(ods|xlsx)]

Short forms: -i (--merged), -m (--master) and -o (--output). The files are
given with named options (and not by position) so that an accidental order
does not write the merged CSV where the master spreadsheet is expected.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

import openpyxl
from openpyxl.cell.cell import Cell, MergedCell
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from inventory_pipeline.base_inventory import error, strip_quotes
from inventory_pipeline.merge_inventories import load_csv_data, validate_rows
from inventory_pipeline.prepare_master_inventory import find_libreoffice

SUPPORTED_EXTENSIONS = {".ods", ".xlsx"}


# ============================================================
# VALUE NORMALIZATION AND CONVERSION
# ============================================================


def normalize_value(value) -> str:
    """
    Normalizes a value (coming from a spreadsheet cell or
    from a CSV field) into a canonical string representation,
    so that equivalence can be compared without false positives due to
    numeric format differences (e.g. 16 vs 16.0).
    """
    if value is None:
        return ""
    text = str(value).strip()
    try:
        as_float = float(text)
    except ValueError:
        return text
    if as_float == int(as_float):
        return str(int(as_float))
    return str(as_float)


def coerce_for_cell(value_str: str) -> int | float | str | None:
    """
    Converts a string value coming from the CSV into the most
    appropriate Python type to write it into a cell, preserving the
    numeric type when applicable (instead of forcing everything to
    text, which would degrade the format of numeric columns).
    """
    if value_str == "":
        return None
    try:
        return int(value_str)
    except ValueError:
        pass
    try:
        return float(value_str)
    except ValueError:
        pass
    return value_str


def column_letter(index0: int) -> str:
    """
    Converts a 0-based column index into spreadsheet-style
    letter notation (0 -> A, 25 -> Z, 26 -> AA, ...).
    """
    index = index0 + 1
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


# ============================================================
# LIBREOFFICE: GENERIC CONVERSION BETWEEN FORMATS
# ============================================================


def convert_with_libreoffice(
    libreoffice: str,
    input_path: Path,
    target_format: str,
    out_dir: Path,
) -> Path:
    command = [
        libreoffice,
        "--headless",
        "--convert-to",
        target_format,
        "--outdir",
        str(out_dir),
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
            f"LibreOffice could not convert the file to "
            f"{target_format}.\n\n"
            f"stdout:\n{result.stdout}\n\n"
            f"stderr:\n{result.stderr}"
        )
    converted = out_dir / f"{input_path.stem}.{target_format}"
    if not converted.is_file():
        error(
            "LibreOffice finished without error, but the converted "
            f"file was not found:\n  {converted}"
        )
    return converted


# ============================================================
# LOCATE THE HEADER AND COUNT DATA ROWS IN THE MASTER
# ============================================================


def find_header_row_number(ws: Worksheet, merged_header: list[str]) -> int | None:
    """
    Finds the first row whose content, in the same order,
    matches EXACTLY merged_header. This validates at once
    column names and positions between the master and the
    merged inventory.
    """
    n_cols = len(merged_header)
    for row_number, row in enumerate(
        ws.iter_rows(
            min_row=1,
            max_row=ws.max_row,
            max_col=n_cols,
            values_only=True,
        ),
        start=1,
    ):
        candidate = ["" if v is None else str(v).strip() for v in row]
        if candidate == merged_header:
            return row_number
    return None


def count_master_data_rows(ws: Worksheet, header_row_number: int, n_cols: int) -> int:
    """
    Counts data rows starting from header_row_number + 1,
    taking as the limit the last row with at least one non-empty
    value within the n_cols relevant columns.
    """
    last_row_with_data = header_row_number
    for row_number, row in enumerate(
        ws.iter_rows(
            min_row=header_row_number + 1,
            max_row=ws.max_row,
            max_col=n_cols,
            values_only=True,
        ),
        start=header_row_number + 1,
    ):
        if any(v is not None and str(v).strip() != "" for v in row):
            last_row_with_data = row_number
    return last_row_with_data - header_row_number


# ============================================================
# UPDATE AN XLSX FILE (DIRECT EDITING)
# ============================================================


def get_active_worksheet(wb: Workbook) -> Worksheet:
    """Gets the active worksheet of a workbook."""
    ws = wb.active

    if not isinstance(ws, Worksheet):
        error("The spreadsheet does not contain a valid active worksheet to update.")

    return ws


def get_cell(ws: Worksheet, row: int, column: int) -> Cell:
    """Gets a regular cell and rejects merged cells."""
    cell = ws.cell(row=row, column=column)

    if isinstance(cell, MergedCell):
        error(f"A merged cell cannot be updated at {column_letter(column - 1)}{row}.")

    return cell


def update_xlsx(
    path: Path,
    merged_header: list[str],
    merged_rows: list[list[str]],
) -> dict[str, list[str]]:
    """
    Opens the .xlsx at `path`, locates the header, validates
    row correspondence, overwrites only the cells
    whose value changed, saves the file and returns a map
    {column_name: [modified_cell_addresses]}.
    """
    wb = openpyxl.load_workbook(path)
    ws = get_active_worksheet(wb)

    n_cols = len(merged_header)
    header_row_number = find_header_row_number(ws, merged_header)
    if header_row_number is None:
        error(
            "No row was found in the master spreadsheet "
            "that matches exactly (same names and same "
            "positions) the header of the merged inventory."
        )

    print(f"Header row detected in the master       : {header_row_number}")

    master_data_rows = count_master_data_rows(ws, header_row_number, n_cols)

    if master_data_rows != len(merged_rows):
        error(
            "The number of data rows of the master spreadsheet "
            f"({master_data_rows}) does not match the number of "
            f"rows of the merged inventory ({len(merged_rows)}).\n"
            "It is not safe to assume positional correspondence between "
            "both files."
        )

    changes: dict[str, list[str]] = {}
    first_data_row = header_row_number + 1

    for row_offset, merged_row in enumerate(merged_rows):
        sheet_row_number = first_data_row + row_offset
        for col_idx in range(n_cols):
            cell = get_cell(ws, sheet_row_number, col_idx + 1)
            current_norm = normalize_value(cell.value)
            new_value_str = merged_row[col_idx]
            new_norm = normalize_value(new_value_str)

            if current_norm == new_norm:
                continue  # No change, do not overwrite.

            cell.value = coerce_for_cell(new_value_str)

            column_name = merged_header[col_idx]
            address = f"{column_letter(col_idx)}{sheet_row_number}"
            changes.setdefault(column_name, []).append(address)

    wb.save(path)
    return changes


# ============================================================
# CHANGE REPORT
# ============================================================


def print_change_report(
    changes: dict[str, list[str]],
    merged_header: list[str],
) -> None:
    print()
    if not changes:
        print(
            "No changes were detected with respect to the previous master spreadsheet."
        )
        return

    print("Modified columns:")
    print()
    for col_idx, column_name in enumerate(merged_header):
        if column_name not in changes:
            continue
        letter = column_letter(col_idx)
        addresses = changes[column_name]
        print(f"Column {letter} ({column_name})")
        print(", ".join(addresses))
        print()


# ============================================================
# MAIN
# ============================================================


class CliArguments(NamedTuple):
    """Command-line arguments; `output` is None if it was not given."""

    merged: Path
    master: Path
    output: Path | None


def parse_cli_arguments(argv: Sequence[str]) -> CliArguments:
    """
    Parses the command-line arguments (without the program name).

    Design decisions:
    - The files are named options: with positional arguments, swapping
      the merged CSV and the master spreadsheet would only be detected later and with
      confusing messages.
    - The output is optional; its default value depends on the extension of the
      master, so it is resolved in `main`.

    argparse ends the process with a usage message if a mandatory option is missing.
    """
    parser = argparse.ArgumentParser(
        description="Lays the merged inventory over a copy of the master."
    )
    parser.add_argument(
        "-i",
        "--merged",
        type=Path,
        required=True,
        help="CSV of the merged inventory (merged_inventory.csv).",
    )
    parser.add_argument(
        "-m",
        "--master",
        type=Path,
        required=True,
        help="Original master spreadsheet (.ods or .xlsx).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Updated copy of the master (default <master>_updated.<ext>).",
    )
    args = parser.parse_args(argv)
    return CliArguments(merged=args.merged, master=args.master, output=args.output)


def main() -> None:
    cli = parse_cli_arguments(sys.argv[1:])
    merged_csv_path = cli.merged
    master_path = cli.master

    for path, label in (
        (merged_csv_path, "merged inventory"),
        (master_path, "master spreadsheet"),
    ):
        if not path.is_file():
            error(f"The {label} was not found:\n  {path}")

    ext = master_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        error(
            "The master spreadsheet must be an ODS (.ods) "
            "or Excel (.xlsx) file:\n"
            f"  {master_path}"
        )

    output_path = (
        cli.output
        if cli.output is not None
        else master_path.parent / f"{master_path.stem}_updated{master_path.suffix}"
    )

    if output_path.suffix.lower() != ext:
        error(
            "The extension of the output file must match "
            "that of the master spreadsheet "
            f"({ext}):\n  {output_path}"
        )

    # --------------------------------------------------------
    # Validate collisions between the input and output files.
    # --------------------------------------------------------
    resolved_output = output_path.resolve()
    resolved_merged = merged_csv_path.resolve()
    resolved_master = master_path.resolve()

    if resolved_output == resolved_merged:
        error(
            "The output file cannot be the same file "
            "as the merged inventory:\n"
            f"  {output_path}"
        )
    if resolved_output == resolved_master:
        error(
            "The output file cannot be the same file "
            "as the master spreadsheet (a copy must be "
            "generated):\n"
            f"  {output_path}"
        )

    # --------------------------------------------------------
    # Load the merged inventory.
    # --------------------------------------------------------
    _header_line, header_fields_raw, rows_raw = load_csv_data(merged_csv_path)
    merged_header = [strip_quotes(f) for f in header_fields_raw]
    n_cols = len(merged_header)

    rows_raw = validate_rows(rows_raw, n_cols, merged_csv_path)
    merged_rows = [[strip_quotes(v) for v in row] for row in rows_raw]

    print()
    print(f"Merged inventory                 : {merged_csv_path}")
    print(f"Master spreadsheet               : {master_path}")
    print(f"Output spreadsheet               : {output_path}")
    print(f"Columns in the merged inventory  : {n_cols}")
    print(f"Data rows in the merged inventory: {len(merged_rows)}")

    # --------------------------------------------------------
    # Process: create copy + edit + atomic replacement.
    # --------------------------------------------------------
    with tempfile.TemporaryDirectory(prefix="update_master_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)

        with tempfile.NamedTemporaryFile(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_out_f:
            temporary_output_path = Path(tmp_out_f.name)

        try:
            if ext == ".xlsx":
                shutil.copy2(master_path, temporary_output_path)
                changes = update_xlsx(
                    temporary_output_path,
                    merged_header,
                    merged_rows,
                )
            else:
                libreoffice = find_libreoffice()

                print()
                print("Converting the master copy to XLSX (intermediate editing)...")
                xlsx_tmp = convert_with_libreoffice(
                    libreoffice, master_path, "xlsx", tmp_dir
                )

                changes = update_xlsx(
                    xlsx_tmp,
                    merged_header,
                    merged_rows,
                )

                print("Converting the edited copy back to ODS...")
                ods_tmp = convert_with_libreoffice(
                    libreoffice, xlsx_tmp, "ods", tmp_dir
                )

                shutil.copy2(ods_tmp, temporary_output_path)

            os.replace(temporary_output_path, output_path)
            temporary_output_path = None
        finally:
            if temporary_output_path is not None:
                try:
                    temporary_output_path.unlink()
                except FileNotFoundError:
                    pass

    # --------------------------------------------------------
    # Change report.
    # --------------------------------------------------------
    print_change_report(changes, merged_header)

    # --------------------------------------------------------
    # Result.
    # --------------------------------------------------------
    print()
    print("[OK] Master spreadsheet updated successfully.")
    print(f"Merged inventory : {merged_csv_path}")
    print(f"Original master  : {master_path}")
    print(f"Updated master   : {output_path}")


if __name__ == "__main__":
    main()
