import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from inventory_pipeline.base_inventory import load_rundeck_header_list
from inventory_pipeline.parse_job_output import csv_field, main, parse_key_value_field
from inventory_pipeline.paths import DEFAULT_HEADER_LIST_PATH, PROJECT_ROOT

COLLECTOR_SCRIPT = PROJECT_ROOT / "collectors" / "asset_information.sh"


class TestCollectorContract:
    """Validates that the Rundeck collector and the parser agree on the keys."""

    def test_collector_output_keys_match_header_list(self) -> None:
        """A key emitted by the collector but missing from rundeck_header_list.txt is
        fatal for the parser, and a column the collector no longer emits is silently
        filled with N/A: both lists must name exactly the same columns."""
        script = COLLECTOR_SCRIPT.read_text(encoding="utf-8")
        keys_block = re.search(
            r"^OUTPUT_KEYS=\(\r?\n(.*?)^\)", script, re.MULTILINE | re.DOTALL
        )
        assert keys_block is not None, "OUTPUT_KEYS array not found in the collector"

        collector_keys = re.findall(r'"([^"]*)"', keys_block.group(1))
        header_names = [
            name for name, _flag in load_rundeck_header_list(DEFAULT_HEADER_LIST_PATH)
        ]

        assert sorted(collector_keys) == sorted(header_names)


class TestCsvField:
    """Validates the conversion to CSV format and quote escaping."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("hello", '"hello"'),
            ("", '""'),
            (" ", '" "'),
            ("N/A", '"N/A"'),
            # Escaping of inner double quotes
            ('say "hi"', '"say ""hi"""'),
            ('""', '""""""'),
            ('"already_quoted"', '"""already_quoted"""'),
        ],
    )
    def test_csv_field(self, value: str, expected: str) -> None:
        assert csv_field(value) == expected


class TestParseKeyValueField:
    """Validates the parsing of fields in KEY="VALUE" format."""

    def test_parse_valid_field(self) -> None:
        assert parse_key_value_field('IP="10.0.0.1"') == ("IP", "10.0.0.1")

    def test_parse_valid_empty_value(self) -> None:
        assert parse_key_value_field('MAC=""') == ("MAC", "")

    def test_parse_missing_equals(self) -> None:
        assert parse_key_value_field("JUST_KEY") is None

    def test_parse_empty_key(self) -> None:
        assert parse_key_value_field('="VALUE"') is None

    def test_parse_unquoted_value(self) -> None:
        assert parse_key_value_field("IP=10.0.0.1") is None

    def test_parse_partially_quoted_value(self) -> None:
        assert parse_key_value_field('IP="10.0.0.1') is None
        assert parse_key_value_field('IP=10.0.0.1"') is None

    def test_parse_empty_unquoted_value(self) -> None:
        assert parse_key_value_field("IP=") is None

    def test_parse_key_with_spaces(self) -> None:
        # The current parser does not strip the key
        # This means that spaces are taken literally.
        # It can be considered an edge case of the current behavior.
        assert parse_key_value_field(' MY_KEY ="123"') == (" MY_KEY ", "123")

    def test_parse_value_with_internal_quotes(self) -> None:
        # If the string starts and ends with quotes, strip_quotes removes them.
        # The rest is left untouched.
        assert parse_key_value_field('DESC="Server "01""') == ("DESC", 'Server "01"')


class TestParseJobOutputMain:
    """Validates the main integration flow (main)."""

    @pytest.fixture
    def setup_files(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        input_log = tmp_path / "job_output.log"
        output_csv = tmp_path / "parsed_job_output.csv"
        header_list = tmp_path / "rundeck_header_list.txt"

        header_list.write_text("UUID|2\nIP|1\nMAC|1\nNOTES|0\n")
        return input_log, output_csv, header_list

    def test_main_happy_path(self, setup_files: tuple[Path, Path, Path]) -> None:
        input_log, output_csv, header_list = setup_files
        # We add a garbage line that must be ignored.
        input_log.write_text(
            "Starting job...\n"
            'UUID="123",IP="10.0.0.1",MAC="aa:bb"\n'
            'UUID="456",IP="10.0.0.2",NOTES="hello"\n'  # NOTES flag 0, discarded
            "Done.\n"
        )

        with patch.object(
            sys,
            "argv",
            ["script.py", str(input_log), str(output_csv), str(header_list)],
        ):
            main()

        assert output_csv.exists()
        lines = output_csv.read_text().splitlines()
        assert len(lines) == 3
        assert lines[0] == '"UUID","IP","MAC","NOTES"'
        assert lines[1] == '"123","10.0.0.1","aa:bb",""'
        assert lines[2] == '"456","10.0.0.2","N/A",""'

    def test_main_unknown_key_causes_fatal_error(
        self, setup_files: tuple[Path, Path, Path]
    ) -> None:
        input_log, output_csv, header_list = setup_files
        # BAD_KEY is not in rundeck_header_list
        input_log.write_text('UUID="123",IP="10.0.0.1",BAD_KEY="uh oh"\n')

        with (
            patch.object(
                sys,
                "argv",
                ["script.py", str(input_log), str(output_csv), str(header_list)],
            ),
            pytest.raises(SystemExit),
        ):
            main()

    def test_main_duplicate_key_causes_fatal_error(
        self, setup_files: tuple[Path, Path, Path]
    ) -> None:
        input_log, output_csv, header_list = setup_files
        # IP declared twice on the same line
        input_log.write_text('UUID="123",IP="10.0.0.1",IP="10.0.0.2"\n')

        with (
            patch.object(
                sys,
                "argv",
                ["script.py", str(input_log), str(output_csv), str(header_list)],
            ),
            pytest.raises(SystemExit),
        ):
            main()

    def test_main_malformed_field_in_inventory_line_causes_fatal_error(
        self, setup_files: tuple[Path, Path, Path]
    ) -> None:
        input_log, output_csv, header_list = setup_files
        # Line with at least one valid key (UUID), but it has a corrupt field
        input_log.write_text('UUID="123",CORRUPT,IP="10.0.0.1"\n')

        with (
            patch.object(
                sys,
                "argv",
                ["script.py", str(input_log), str(output_csv), str(header_list)],
            ),
            pytest.raises(SystemExit),
        ):
            main()

    def test_main_invalid_arguments_too_few(self) -> None:
        with patch.object(sys, "argv", ["script.py"]), pytest.raises(SystemExit):
            main()

    def test_main_invalid_arguments_too_many(self) -> None:
        with (
            patch.object(sys, "argv", ["script.py", "1", "2", "3", "4", "5"]),
            pytest.raises(SystemExit),
        ):
            main()

    def test_main_input_file_not_found(
        self, setup_files: tuple[Path, Path, Path]
    ) -> None:
        input_log, output_csv, header_list = setup_files
        with (
            patch.object(
                sys,
                "argv",
                ["script.py", str(input_log), str(output_csv), str(header_list)],
            ),
            pytest.raises(SystemExit),
        ):
            main()

    def test_main_input_output_collision(
        self, setup_files: tuple[Path, Path, Path]
    ) -> None:
        input_log, _, header_list = setup_files
        with (
            patch.object(
                sys,
                "argv",
                ["script.py", str(input_log), str(input_log), str(header_list)],
            ),
            pytest.raises(SystemExit),
        ):
            main()

    def test_main_cleans_up_temp_file_on_failure(
        self, setup_files: tuple[Path, Path, Path]
    ) -> None:
        input_log, output_csv, header_list = setup_files
        # Force a fatal error during processing (e.g. invalid key)
        input_log.write_text('UUID="123",BAD_KEY="uh oh"\\n')

        with (
            patch.object(
                sys,
                "argv",
                ["script.py", str(input_log), str(output_csv), str(header_list)],
            ),
            pytest.raises(SystemExit),
        ):
            main()

        # The final output file must not have been created (since it failed before moving it)
        assert not output_csv.exists()
        # Verify that no orphan temporary files exist in the directory
        tmp_files = list(output_csv.parent.glob(f".{output_csv.name}.*.tmp"))
        assert len(tmp_files) == 0
