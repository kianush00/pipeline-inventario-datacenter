import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from parse_job_output import csv_field, main, parse_key_value_field


class TestCsvField:
    """Valida la conversión a formato CSV y el escape de comillas."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("hello", '"hello"'),
            ("", '""'),
            (" ", '" "'),
            ("N/A", '"N/A"'),
            # Escape de comillas dobles internas
            ('say "hi"', '"say ""hi"""'),
            ('""', '""""""'),
            ('"already_quoted"', '"""already_quoted"""'),
        ],
    )
    def test_csv_field(self, value: str, expected: str) -> None:
        assert csv_field(value) == expected


class TestParseKeyValueField:
    """Valida el parseo de campos en formato CLAVE="VALOR"."""

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
        # El parser actual no hace strip de la clave
        # Esto significa que los espacios son tomados literalmente.
        # Puede ser considerado un edge case de comportamiento actual.
        assert parse_key_value_field(' MY_KEY ="123"') == (" MY_KEY ", "123")

    def test_parse_value_with_internal_quotes(self) -> None:
        # Si el string arranca y termina con comillas, strip_quotes se las saca.
        # El resto queda intacto.
        assert parse_key_value_field('DESC="Server "01""') == ("DESC", 'Server "01"')


class TestParseJobOutputMain:
    """Valida el flujo de integración principal (main)."""

    @pytest.fixture
    def setup_files(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        input_log = tmp_path / "job_output.log"
        output_csv = tmp_path / "parsed_job_output.csv"
        header_list = tmp_path / "rundeck_header_list.txt"

        header_list.write_text("UUID|2\nIP|1\nMAC|1\nNOTES|0\n")
        return input_log, output_csv, header_list

    def test_main_happy_path(self, setup_files: tuple[Path, Path, Path]) -> None:
        input_log, output_csv, header_list = setup_files
        # Agregamos una línea basura que debe ser ignorada.
        input_log.write_text(
            "Starting job...\n"
            'UUID="123",IP="10.0.0.1",MAC="aa:bb"\n'
            'UUID="456",IP="10.0.0.2",NOTES="hello"\n'  # NOTES flag 0, se descarta
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
        # BAD_KEY no está en rundeck_header_list
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
        # IP declarada dos veces en la misma línea
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
        # Línea con al menos un key válido (UUID), pero tiene un campo corrupto
        input_log.write_text('UUID="123",CORRUPTO,IP="10.0.0.1"\n')

        with (
            patch.object(
                sys,
                "argv",
                ["script.py", str(input_log), str(output_csv), str(header_list)],
            ),
            pytest.raises(SystemExit),
        ):
            main()
