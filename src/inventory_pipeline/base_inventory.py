"""
base_inventory.py
==================
Utility module shared by the three inventory scripts:

    prepare_master_inventory.py
    parse_job_output.py
    merge_inventories.py

It is not executed directly.
"""

import sys
from pathlib import Path
from typing import NoReturn

# ============================================================
# FATAL ERROR OUTPUT
# ============================================================


def error(message: str) -> NoReturn:
    """Prints an error message to stderr and exits with code 1."""
    print(f"[ERROR] {message}", file=sys.stderr)
    sys.exit(1)


def usage(message: str) -> NoReturn:
    """Prints a usage message to stderr and exits with code 1."""
    print(message, file=sys.stderr)
    sys.exit(1)


# ============================================================
# rundeck_header_list.txt FORMAT
#
# Each non-empty, non-comment line has the form:
#
#     COLUMN_NAME|FLAG
#
# where FLAG can be:
#
#     0 -> the column does not take part in the processing.
#     1 -> the column takes part normally.
#     2 -> the column takes part normally and is the key
#          used to identify a row.
#
# The order of the lines defines the logical order of the columns
# the pipeline cares about. Intermediate columns of the master
# inventory that do not appear in rundeck_header_list.txt are
# kept untouched and are never merged.
# ============================================================


def load_rundeck_header_list(path: Path) -> list[tuple[str, int]]:
    """
    Reads rundeck_header_list.txt and returns a list of tuples:

        [(name, flag), ...]

    Validations:
    - The file must exist and must not be empty.
    - Each line must have exactly two fields separated by '|'.
    - NAME cannot be empty.
    - FLAG must be '0', '1' or '2'.
    - There cannot be duplicate names.
    - There must be exactly one column with FLAG 2.

    Parameters
    ----------
    path : Path
        Path to the rundeck_header_list.txt file.

    Returns
    -------
    list[tuple[str, int]]
        List of (column_name, flag).
    """
    if not path.is_file():
        error(f"rundeck_header_list file not found:\n  {path}")

    entries: list[tuple[str, int]] = []
    seen_names: set[str] = set()

    with path.open("r", encoding="utf-8") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.strip()

            # ------------------------------------------------
            # Skip empty lines and comments.
            # ------------------------------------------------
            if not line or line.startswith("#"):
                continue

            parts = line.split("|")

            if len(parts) != 2:
                error(
                    f"rundeck_header_list line {line_number}: invalid format:\n"
                    f"  {line}\n"
                    f"Expected format:\n"
                    f"  COLUMN_NAME|0\n"
                    f"  COLUMN_NAME|1\n"
                    f"  COLUMN_NAME|2"
                )

            name, flag_str = parts

            if not name:
                error(
                    f"rundeck_header_list line {line_number}: the column name is empty."
                )

            if flag_str not in ("0", "1", "2"):
                error(
                    f"rundeck_header_list line {line_number}: "
                    f"the flag must be 0, 1 or 2, found '{flag_str}'."
                )

            if name in seen_names:
                error(
                    f"rundeck_header_list line {line_number}: "
                    f"duplicate column name: '{name}'."
                )

            seen_names.add(name)
            entries.append((name, int(flag_str)))

    if not entries:
        error("rundeck_header_list is empty (no valid entries).")

    key_entries = [name for name, flag in entries if flag == 2]

    if len(key_entries) != 1:
        error(
            "rundeck_header_list must contain exactly one column "
            f"with FLAG 2, but {len(key_entries)} were found.\n"
            + (
                "Columns with FLAG 2:\n"
                + "\n".join(f"  - {name}" for name in key_entries)
                if key_entries
                else "There is no column with FLAG 2."
            )
        )

    return entries


# ============================================================
# PARSING A CSV LINE WHILE HONORING QUOTES
#
# Returns the "raw" fields (including the quotes that delimit
# them and the inner double quotes, not unescaped).
# It is only used to locate field boundaries (commas outside
# quotes), not to interpret the content.
#
# Returns None if the quotes are unbalanced.
# ============================================================


def split_quoted_csv_line(line: str) -> list[str] | None:
    """
    Splits a CSV line honoring quoted fields.

    Any residual line terminator (CR, LF or CRLF) is defensively
    stripped before processing the line.

    Each field is returned as it appears in the text, quotes
    included. Inner double quotes are not unescaped.

    Returns None if the quotes are unbalanced.
    """
    line = line.rstrip("\r\n")

    fields: list[str] = []
    field_chars: list[str] = []
    in_quotes = False
    i = 0
    length = len(line)

    while i < length:
        char = line[i]

        if char == '"':
            field_chars.append(char)
            if in_quotes and i + 1 < length and line[i + 1] == '"':
                # Escaped double quote inside a field.
                field_chars.append('"')
                i += 1
            else:
                in_quotes = not in_quotes
        elif char == "," and not in_quotes:
            fields.append("".join(field_chars))
            field_chars = []
        else:
            field_chars.append(char)

        i += 1

    fields.append("".join(field_chars))

    if in_quotes:
        return None

    return fields


# ============================================================
# CSV VALUE CLEANUP
# ============================================================


def strip_quotes(value: str) -> str:
    """Removes the outer quotes of a CSV field and unescapes
    the inner double quotes.

    Assumes the value comes from split_quoted_csv_line() and that
    the field quotes were already validated there. This function
    does not check for unbalanced quotes by itself.

    Examples:
        '"hello"' -> 'hello'
        field with doubled quotes -> field with single quotes
        'unquoted' -> 'unquoted'
    """
    clean = value

    if len(clean) >= 2 and clean[0] == '"' and clean[-1] == '"':
        clean = clean[1:-1]

    clean = clean.replace('""', '"')
    return clean


def is_empty_or_na(value: str) -> bool:
    """Returns True if the value, quoted or not, is empty,
    contains only whitespace or is 'N/A'.

    Examples:
        '' -> True
        '   ' -> True
        'N/A' -> True
        '""' -> True
        '"N/A"' -> True
        '"   "' -> True
        'hello' -> False
    """
    return strip_quotes(value).strip().upper() in ("", "N/A")


def clean_value(value: str) -> str:
    """Removes inner line breaks from a value.

    Line breaks are replaced with a space to avoid
    artificially joining words.

    The order of the replacements is deliberate: CRLF is processed
    first so that '\\r\\n' is not turned into two spaces.

    Examples:
        'hello\\nworld' -> 'hello world'
        'hello\\r\\nworld' -> 'hello world'
        'hello world' -> 'hello world'
    """
    return value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
