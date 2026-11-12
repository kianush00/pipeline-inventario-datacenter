"""
merge_inventories.py
=======================
Merges a "parsed" inventory onto a "master" inventory
(source of truth), using as the merge key the column whose
FLAG in rundeck_header_list.txt is 2.

The column layout and the merge rules are read from
rundeck_header_list.txt with the format:

    COLUMN_NAME|FLAG

where FLAG means:
    0 → keep the value of the master inventory
    1 → allow the value of the parsed inventory to
        overwrite the master one (if it is neither empty nor "N/A")
    2 → column used as the join key

There must be exactly one column with FLAG 2.

The column with FLAG 2 can have any name. That name
is used as the join key both in the master inventory
and in the parsed inventory.

The column positions are detected automatically
by reading the headers of both CSVs.

The master inventory is the base:
- All its rows are kept in the output CSV.
- Additional columns not defined in rundeck_header_list.txt
  are kept without merging.
- Columns with FLAG 0 are kept without merging.
- Columns with FLAG 1 can be updated from the
  parsed inventory.
- The column with FLAG 2 is used exclusively as the join
  key and is never overwritten.

The join behaves like a LEFT JOIN:
- If the master key does not exist in the parsed one, the master
  row is kept unmodified.
- If the master key is empty or N/A, the master row
  is kept without merging.
- If a key appears more than once in either of the two
  inventories, the affected rows are kept without merging.
- Only rows whose key is valid and unique in both
  inventories are merged.

Usage:
    python -m inventory_pipeline.merge_inventories \
        --parsed parsed_job_output.csv \
        --master prepared_master_inventory.csv \
        [--output merged_inventory.csv] \
        [--header-list rundeck_header_list.txt]

Each option has a short form: -p, -m, -o and -l respectively.

The inventories are given with named options (and not by position) so that
an accidental order of the files does not silently invert the merge.

If --header-list is not given, config/rundeck_header_list.txt is used. If
--output is not given, output/merged_inventory.csv is written (see
inventory_pipeline.paths).
"""

import argparse
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from inventory_pipeline.base_inventory import (
    error,
    is_empty_or_na,
    load_rundeck_header_list,
    split_quoted_csv_line,
    strip_quotes,
)
from inventory_pipeline.paths import (
    DEFAULT_HEADER_LIST_PATH,
    MERGED_CSV_NAME,
    OUTPUT_DIR,
)

# ============================================================
# DATA STRUCTURES OF THE MAIN FLOW
# ============================================================


class KeyedRows(NamedTuple):
    """Rows of an inventory indexed by join key."""

    rows_by_key: dict[str, list[str]]
    duplicated_keys: set[str]


class CliPaths(NamedTuple):
    """Paths resolved from the command-line arguments."""

    parsed: Path
    master: Path
    output: Path
    rundeck_header_list: Path


class LoadedInventory(NamedTuple):
    """CSV inventory already read, with its column positions and valid rows."""

    header_line: str
    header_fields: list[str]
    positions: dict[str, int]
    rows: list[list[str]]


class MergePlan(NamedTuple):
    """Read-only data needed to decide the fate of each master row."""

    key_master_idx: int
    parsed_data: dict[str, list[str]]
    master_duplicated_keys: set[str]
    merge_pairs: list[tuple[int, int]]


# ============================================================
# VALIDATE AND RESOLVE THE JOIN KEY
# ============================================================


def resolve_key_column(
    rundeck_header_list: list[tuple[str, int]],
) -> str:
    """
    Determines the column used as the join key.
    There must be exactly one entry with FLAG 2.

    Returns:
        name of the column with FLAG 2.
    """
    key_columns = [name for name, flag in rundeck_header_list if flag == 2]

    if len(key_columns) != 1:
        error(
            "rundeck_header_list.txt must contain exactly one "
            "column with flag 2.\n"
            f"{len(key_columns)} were found."
        )

    return key_columns[0]


# ============================================================
# VALIDATE THE JOIN KEY
# ============================================================


def is_invalid_key(value: str) -> bool:
    """
    Returns True if the value cannot be used as a join
    key.
    A key is invalid only if it is empty or is "N/A".
    """
    return is_empty_or_na(value)


def extract_key(fields: list[str], key_idx: int) -> str:
    """Returns the join key of a row, unquoted and in lowercase."""
    return strip_quotes(fields[key_idx]).lower()


def is_identity_duplicate(
    row_a: list[str], row_b: list[str], positions: dict[str, int]
) -> bool:
    """
    Checks whether two rows with the same key are the same node record
    repeated, comparing only the identity fields (name, MAC, IP and serial,
    ignoring case and whitespace). The other fields are not evaluated.
    """
    fields_to_check = ["Machine name", "MAC", "IP", "Serial Number"]
    for field in fields_to_check:
        idx = positions.get(field)
        if idx is not None and idx < len(row_a) and idx < len(row_b):
            val_a = strip_quotes(row_a[idx]).strip().lower()
            val_b = strip_quotes(row_b[idx]).strip().lower()
            if val_a != val_b:
                return False
    return True


# ============================================================
# RESOLVE POSITIONS IN A HEADER
# ============================================================


def resolve_positions(
    rundeck_header_list: list[tuple[str, int]],
    header_fields: list[str],
    csv_path: Path,
) -> dict[str, int]:
    """
    Detects the position (0-based index) of each column of
    rundeck_header_list within the header of a CSV.

    Rules:
    - Each rundeck_header_list name must appear exactly
      once in the CSV header.
    - The order of the columns does NOT matter.
    - The CSVs may contain additional columns not defined
      in rundeck_header_list.txt.

    Returns:
        dict {column_name: 0based_index}
    """
    name_to_index: dict[str, int] = {}

    for idx, raw_field in enumerate(header_fields):
        field_name = strip_quotes(raw_field)

        if field_name in name_to_index:
            error(
                f"The file has the column name "
                f"'{field_name}' duplicated (positions "
                f"{name_to_index[field_name] + 1} and {idx + 1}).\n"
                f"  {csv_path}"
            )

        name_to_index[field_name] = idx

    positions: dict[str, int] = {}

    for name, _flag in rundeck_header_list:
        if name not in name_to_index:
            error(
                f"The column '{name}' defined in rundeck_header_list.txt "
                f"does not exist in the header of the file.\n"
                f"  {csv_path}"
            )

        positions[name] = name_to_index[name]

    return positions


# ============================================================
# READ THE WHOLE CSV
# ============================================================


def load_csv_data(
    csv_path: Path,
) -> tuple[str, list[str], list[list[str]]]:
    """
    Reads a whole CSV in a single pass.

    Returns:
        (original_header_line, header_fields, data_rows)

    The rows are kept in memory to avoid a second
    read of the file during the later processing.
    """
    with csv_path.open(
        "r",
        encoding="utf-8",
        errors="replace",
    ) as file:
        header_line = file.readline().rstrip("\r\n")

        if not header_line:
            error(f"The CSV file is empty:\n  {csv_path}")

        header_fields = split_quoted_csv_line(header_line)

        if header_fields is None:
            error(f"The CSV header has unbalanced quotes:\n  {csv_path}")

        rows: list[list[str]] = []

        for line_number, raw_line in enumerate(file, start=2):
            line = raw_line.rstrip("\r\n")

            if not line:
                continue

            fields = split_quoted_csv_line(line)

            if fields is None:
                print(
                    f"[ERROR] Line {line_number} of "
                    f"{csv_path.name}: unbalanced quotes.",
                    file=sys.stderr,
                )
                continue

            rows.append(fields)

    return header_line, header_fields, rows


# ============================================================
# VALIDATE ROW STRUCTURE
# ============================================================


def validate_rows(
    rows: list[list[str]],
    expected_columns: int,
    csv_path: Path,
) -> list[list[str]]:
    """
    Validates that all rows have exactly the expected
    number of columns.

    Invalid rows are discarded.
    """
    valid_rows: list[list[str]] = []

    for row_number, fields in enumerate(rows, start=2):
        if len(fields) != expected_columns:
            print(
                f"[ERROR] Line {row_number} of {csv_path.name}: "
                f"{expected_columns} fields were expected, "
                f"but {len(fields)} were found.",
                file=sys.stderr,
            )
            continue

        valid_rows.append(fields)

    return valid_rows


# ============================================================
# BUILD THE MAP OF THE PARSED INVENTORY
# ============================================================


def index_rows_by_unique_key(
    rows: list[list[str]],
    key_idx: int,
    positions: dict[str, int],
    inventory_label: str,
) -> KeyedRows:
    """
    Indexes the rows by join key, separating the unique keys from the duplicate ones.

    Design decisions:
    - Logic shared by the parsed inventory and the master (`inventory_label` only
      gives context to the logs), so that both apply exactly the same rule.
    - Rows with an invalid key are ignored. If a key is repeated and the rows
      are identity duplicates, the first one is kept and the rest are skipped; if
      they differ, the key is marked as duplicate and excluded from the index to
      prevent any later merge.

    Returns:
        KeyedRows: (key -> unique row, duplicate keys)
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
                f"[WARNING] Identity duplicate detected in the {inventory_label} "
                f"inventory for the key '{key}'. Skipping duplicate.",
                file=sys.stderr,
            )
            continue

        del unique_rows[key]
        duplicated_keys.add(key)

    for key in sorted(duplicated_keys):
        print(
            f"[WARNING] Duplicate key in the {inventory_label} inventory: "
            f"'{key}'. The rows with this key differ and will not be merged.",
            file=sys.stderr,
        )

    return KeyedRows(unique_rows, duplicated_keys)


def build_parsed_data(
    rows: list[list[str]],
    key_idx: int,
    positions: dict[str, int],
) -> KeyedRows:
    """
    Builds a map:

        key -> row

    Duplicate keys are recorded and excluded from the
    map to prevent any later merge, unless they are identity duplicates.
    """
    return index_rows_by_unique_key(rows, key_idx, positions, "parsed")


# ============================================================
# VALIDATE KEY UNIQUENESS IN THE MASTER INVENTORY
# ============================================================


def find_master_duplicated_keys(
    rows: list[list[str]],
    key_idx: int,
    positions: dict[str, int],
) -> set[str]:
    """
    Detects duplicate keys in the master inventory.

    Returns the set of duplicate keys (those that are not identity duplicates).
    """
    return index_rows_by_unique_key(rows, key_idx, positions, "master").duplicated_keys


# ============================================================
# ARGUMENTS AND PATH VALIDATION
# ============================================================


def parse_cli_paths(argv: Sequence[str]) -> CliPaths:
    """
    Parses the command-line arguments (without the program name).

    Design decisions:
    - The inventories are mandatory named options (`--parsed`, `--master`):
      with positional arguments, swapping the files produces no
      error and the merge runs backwards without warning.
    - The output and the rundeck_header_list are optional and, by default, resolve
      to output/ and config/ respectively (see inventory_pipeline.paths).

    argparse ends the process with a usage message if a mandatory option is missing.
    """
    parser = argparse.ArgumentParser(
        description="Merges a parsed inventory onto the master inventory."
    )
    parser.add_argument(
        "-p", "--parsed", type=Path, required=True, help="CSV of the parsed inventory."
    )
    parser.add_argument(
        "-m", "--master", type=Path, required=True, help="CSV of the master inventory."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=OUTPUT_DIR / MERGED_CSV_NAME,
        help=f"Output CSV (default output/{MERGED_CSV_NAME}).",
    )
    parser.add_argument(
        "-l",
        "--header-list",
        type=Path,
        default=DEFAULT_HEADER_LIST_PATH,
        help="Path of rundeck_header_list.txt (default in config/).",
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
    Verifies that the input inventories exist and that the output does not clobber them.

    Ends the process with an error if any validation fails.
    """
    labeled_inputs = (
        (paths.parsed, "parsed inventory"),
        (paths.master, "master inventory"),
    )
    ensure_inputs_exist(labeled_inputs)
    ensure_output_does_not_overwrite(paths.output, labeled_inputs)


def ensure_inputs_exist(labeled_inputs: tuple[tuple[Path, str], ...]) -> None:
    """Ends the process with an error if any input inventory does not exist."""
    for path, label in labeled_inputs:
        if not path.is_file():
            error(f"The {label} was not found: {path}")


def ensure_output_does_not_overwrite(
    output_path: Path, labeled_inputs: tuple[tuple[Path, str], ...]
) -> None:
    """Ends the process with an error if the output matches any input inventory."""
    resolved_output = output_path.resolve()
    for path, label in labeled_inputs:
        if resolved_output == path.resolve():
            error(
                f"The output file cannot be the same file "
                f"as the {label}:\n"
                f"  {output_path}"
            )


# ============================================================
# INVENTORY LOADING
# ============================================================


def load_inventory(
    csv_path: Path,
    rundeck_header_list: list[tuple[str, int]],
    restrict_to_defined_columns: bool,
) -> LoadedInventory:
    """
    Reads a CSV inventory, resolves the positions of its columns and discards the
    rows with an invalid number of fields.

    Design decisions:
    - The parsed inventory only contains the columns defined in
      rundeck_header_list (`restrict_to_defined_columns=True`); the master may
      bring additional columns, so it is validated against its own header.
    - A parsed inventory with additional columns is almost certainly the master passed
      in the wrong place: it aborts instead of silently discarding all its rows.
    """
    header_line, header_fields, rows = load_csv_data(csv_path)
    positions = resolve_positions(rundeck_header_list, header_fields, csv_path)
    if restrict_to_defined_columns and len(header_fields) != len(rundeck_header_list):
        error(
            f"The parsed inventory has {len(header_fields)} columns but "
            f"rundeck_header_list defines {len(rundeck_header_list)}. "
            "An inventory with additional columns corresponds to the master: "
            "check that the arguments are not swapped.\n"
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
    Builds the (master_index, parsed_index) pairs of the mergeable columns.

    Only columns with flag 1 are merged:
        flag 0 -> never merged.
        flag 1 -> can be updated from the parsed one.
        flag 2 -> join key; never overwritten.
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
    """Prints the informational summary before the merge."""
    master_total_columns = len(master.header_fields)
    extra_columns = master_total_columns - defined_fields

    print(f"Columns defined in rundeck_header_list : {defined_fields}")
    print(f"Total columns in the master       : {master_total_columns}")

    if extra_columns > 0:
        print(f"Additional master columns         : {extra_columns}")

    print(f"Join key                          : {key_name}")
    print(f"Key position in the master        : {plan.key_master_idx + 1}")
    print(f"Key position in the parsed        : {key_parsed_idx + 1}")
    print(f"Valid keys in the parsed          : {len(plan.parsed_data)}")
    print(f"Duplicate keys in the parsed      : {len(parsed_duplicated_keys)}")
    print(f"Duplicate keys in the master      : {len(plan.master_duplicated_keys)}")


def warn_if_no_keys_match(master: LoadedInventory, plan: MergePlan) -> None:
    """
    Warns if no master key exists in the parsed inventory.

    Design decisions:
    - It is not an error (a new master or one without collisions is legitimate), but it is
      the visible sign of swapped arguments or of files that do not belong together.
    """
    if not master.rows:
        return

    has_match = any(
        extract_key(fields, plan.key_master_idx) in plan.parsed_data
        for fields in master.rows
    )
    if not has_match:
        print(
            "[WARNING] No key of the master inventory matches the "
            "parsed inventory. No row will be merged; check that the "
            "arguments are not swapped.",
            file=sys.stderr,
        )


# ============================================================
# ROW MERGE
# ============================================================


def merge_row(
    master_fields: list[str],
    parsed_fields: list[str],
    merge_pairs: list[tuple[int, int]],
) -> list[str]:
    """
    Returns a copy of the master row with the mergeable columns
    overwritten by the parsed value, unless it is empty or "N/A".
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
    Decides the output row for a master row (LEFT JOIN behavior).

    The row is kept without merging if its key is invalid, is duplicated in the
    master or does not exist in the parsed inventory; it is only merged if the key
    is valid and unique in both inventories.
    """
    key = extract_key(master_fields, plan.key_master_idx)

    if is_invalid_key(key) or key in plan.master_duplicated_keys:
        return master_fields

    parsed_fields = plan.parsed_data.get(key)
    if parsed_fields is None:
        print(
            f"[INFO] Line {line_number} of the master inventory: "
            f"the key '{key}' does not exist in the parsed "
            "inventory. The row will be kept without merging.",
            file=sys.stderr,
        )
        return master_fields

    return merge_row(master_fields, parsed_fields, plan.merge_pairs)


# ============================================================
# ATOMIC WRITE
# ============================================================


def write_merged_inventory(
    output_path: Path,
    master: LoadedInventory,
    plan: MergePlan,
) -> None:
    """
    Writes the merged inventory atomically.

    Design decisions:
    - It is written to a temporary file in the same directory as the output so that
      `os.replace()` is atomic; the final output is only replaced if all the
      processing finished successfully.
    - If the process fails before the replacement, the temporary file is removed so no
      leftovers remain.
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
    print("[OK] Inventories merged successfully.")
    print(f"Parsed inventory : {paths.parsed}")
    print(f"Master inventory : {paths.master}")
    print(f"Output inventory : {paths.output}")


if __name__ == "__main__":
    main()
