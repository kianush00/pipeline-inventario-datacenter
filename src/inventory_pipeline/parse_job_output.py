"""
parse_job_output.py
=====================
Normalizes the raw output of the Rundeck job into a CSV with the column
layout defined in rundeck_header_list.txt.

Each valid log line must contain fields in the format:

    KEY="VALUE",KEY="VALUE",...

The keys must match exactly the entries defined
in rundeck_header_list.txt.

The order of the keys in the Rundeck output does NOT matter. Each value
is assigned to the corresponding column according to the key name
defined in rundeck_header_list.txt.

The second value of each rundeck_header_list.txt entry determines the
behavior of the column:

    0 -> the column is left empty in the output CSV.
    1 -> the column is processed normally.
    2 -> the column is processed normally and is the key
         used to identify the row in the merge process.

If a key defined in rundeck_header_list.txt has flag 1 or 2 but does not
appear in a valid line, its value is set to "N/A".

Lines that do not correspond to a valid row are ignored.

A key that appears in the Rundeck output but does NOT exist
exactly in rundeck_header_list.txt causes a fatal error and the script
exits with exit code 1.

Usage:
    python -m inventory_pipeline.parse_job_output \
        job_output.log \
        [parsed_job_output.csv] \
        [rundeck_header_list.txt]

If the output is not given, output/parsed_job_output.csv is written. If the
rundeck_header_list.txt path is not given, config/rundeck_header_list.txt is
used (see inventory_pipeline.paths).
"""

import os
import sys
import tempfile
from pathlib import Path

from inventory_pipeline.base_inventory import (
    error,
    load_rundeck_header_list,
    split_quoted_csv_line,
    strip_quotes,
    usage,
)
from inventory_pipeline.paths import (
    DEFAULT_HEADER_LIST_PATH,
    OUTPUT_DIR,
    PARSED_CSV_NAME,
)


def csv_field(value: str) -> str:
    """Converts a value into a valid CSV field.

    The field is always wrapped in double quotes.
    Inner double quotes are escaped by doubling them.

    Examples:
        hello -> "hello"
        say "hi" -> CSV with doubled quotes
        N/A -> "N/A"
    """
    value = value.replace('"', '""')
    return f'"{value}"'


def parse_key_value_field(field: str) -> tuple[str, str] | None:
    """
    Parses a single field with the format:

        KEY="VALUE"

    Returns:
        (key, value)

    or:
        None

    if the format is not valid.

    The key must appear unquoted and the value must be
    delimited by double quotes.
    """

    separator_index = field.find("=")

    if separator_index <= 0:
        return None

    key = field[:separator_index]
    value = field[separator_index + 1 :]

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
        usage(
            f"Usage: {sys.argv[0]} "
            "job_output.log [parsed_job_output.csv] [rundeck_header_list.txt]"
        )

    if len(sys.argv) > 4:
        usage(
            f"Invalid number of arguments.\n"
            f"Usage: {sys.argv[0]} "
            "job_output.log [parsed_job_output.csv] [rundeck_header_list.txt]"
        )

    input_path = Path(sys.argv[1])
    output_path = (
        Path(sys.argv[2]) if len(sys.argv) >= 3 else OUTPUT_DIR / PARSED_CSV_NAME
    )
    rundeck_header_list_path = (
        Path(sys.argv[3]) if len(sys.argv) >= 4 else DEFAULT_HEADER_LIST_PATH
    )

    if not input_path.is_file():
        error(f"Input file not found: {input_path}")

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
    # Load rundeck_header_list: [(name, flag), ...]
    #
    # The flag determines whether the column is processed:
    #
    #   0 -> empty column
    #   1 -> normal processing
    #   2 -> normal processing and merge key
    #
    # The order of rundeck_header_list determines the order of the
    # columns of the output CSV.
    # --------------------------------------------------------
    rundeck_header_list = load_rundeck_header_list(rundeck_header_list_path)
    defined_fields = len(rundeck_header_list)

    # --------------------------------------------------------
    # Build maps:
    #
    #     column_name -> CSV position
    #     column_name -> flag
    #
    # This makes the order of the keys coming from Rundeck
    # completely independent of the order of the columns
    # of the final CSV.
    # --------------------------------------------------------
    header_positions = {
        name: index for index, (name, _flag) in enumerate(rundeck_header_list)
    }

    header_flags = dict(rundeck_header_list)

    # --------------------------------------------------------
    # Build the header line of the output CSV.
    # The names come directly from rundeck_header_list, in order.
    # --------------------------------------------------------
    header_line = ",".join(csv_field(name) for name, _flag in rundeck_header_list)

    print("Final header:")
    print(header_line)
    print(f"Number of defined columns: {defined_fields}")

    # --------------------------------------------------------
    # Create a temporary file in the same directory as the
    # output to allow an atomic replacement through
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
            # Process the input file line by line.
            #
            # A valid line must:
            #
            #   - Contain comma-separated fields.
            #   - Each field must have the format KEY="VALUE".
            #   - Each key must exist exactly in rundeck_header_list.
            #   - The same key cannot be repeated.
            #
            # Keys with flag 0 are left empty.
            #
            # Keys with flag 1 or 2 that do not appear in a valid
            # line are automatically filled with "N/A".
            #
            # An unknown key or a duplicate key causes
            # a fatal error and ends the script with exit 1.
            # ------------------------------------------------
            with input_path.open("r", encoding="utf-8", errors="replace") as in_f:
                for line_number, raw_line in enumerate(in_f, start=1):
                    line = raw_line.rstrip("\r\n")

                    if not line:
                        continue

                    # ------------------------------------------------
                    # Skip lines that clearly do not correspond
                    # to a Rundeck data row.
                    #
                    # First, the line is split as CSV.
                    # If it contains at least one known key, it is
                    # considered a possible inventory row.
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
                    # Initialize the columns according to their flag.
                    #
                    #   flag 0 -> empty
                    #   flag 1 -> N/A
                    #   flag 2 -> N/A
                    #
                    # This way, a key with flag 0 will never
                    # receive a value from the Rundeck output.
                    # ------------------------------------------------
                    output_values = [
                        "" if flag == 0 else "N/A"
                        for _name, flag in rundeck_header_list
                    ]

                    seen_keys: set[str] = set()

                    # ------------------------------------------------
                    # Process each KEY="VALUE" field.
                    # ------------------------------------------------
                    for field in fields:
                        parsed = parse_key_value_field(field)

                        if parsed is None:
                            error(f"Line {line_number}: invalid field: {field}")

                        key, value = parsed

                        # ------------------------------------------------
                        # The key must match EXACTLY an
                        # entry of rundeck_header_list.txt.
                        #
                        # If it does not exist, it is a fatal error.
                        # ------------------------------------------------
                        if key not in header_positions:
                            error(
                                f"Line {line_number}: "
                                f"key not defined in rundeck_header_list.txt: "
                                f"'{key}'"
                            )

                        # ------------------------------------------------
                        # Do not allow duplicate keys within the
                        # same row.
                        # ------------------------------------------------
                        if key in seen_keys:
                            error(f"Line {line_number}: duplicate key: '{key}'")

                        seen_keys.add(key)

                        # ------------------------------------------------
                        # If the column has flag 0, the value received
                        # from Rundeck is ignored and it stays empty.
                        # ------------------------------------------------
                        if header_flags[key] == 0:
                            continue

                        # ------------------------------------------------
                        # If the column has flag 1 or 2, place the
                        # value at the corresponding position according to
                        # rundeck_header_list.txt.
                        # ------------------------------------------------
                        position = header_positions[key]
                        output_values[position] = value

                    # ------------------------------------------------
                    # Write the final CSV row.
                    #
                    # All values are written as true CSV
                    # fields, regardless of how they
                    # arrived from Rundeck.
                    # ------------------------------------------------
                    out_f.write(
                        ",".join(csv_field(value) for value in output_values) + "\n"
                    )

        # --------------------------------------------------------
        # The temporary file only replaces the final output
        # if all the previous processing finished successfully.
        # --------------------------------------------------------
        os.replace(temporary_output_path, output_path)
        temporary_output_path = None

    finally:
        # --------------------------------------------------------
        # If the process failed before the replacement, remove the
        # temporary file so no leftovers remain.
        # --------------------------------------------------------
        if temporary_output_path is not None:
            try:
                temporary_output_path.unlink()
            except FileNotFoundError:
                pass

    print()
    print("[OK] Process completed.")
    print(f"Input log  : {input_path}")
    print(f"Output CSV : {output_path}")


if __name__ == "__main__":
    main()
