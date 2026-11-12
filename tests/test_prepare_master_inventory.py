import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from inventory_pipeline.prepare_master_inventory import (
    convert_spreadsheet_to_csv,
    find_libreoffice,
    find_real_header,
    main,
    process_csv,
)


class TestFindLibreOffice:
    """Validates the detection of the LibreOffice executable."""

    @patch("shutil.which")
    def test_find_libreoffice_found_primary(self, mock_which: MagicMock) -> None:
        def side_effect(name: str) -> str | None:
            return "/usr/bin/libreoffice" if name == "libreoffice" else None

        mock_which.side_effect = side_effect
        assert find_libreoffice() == "/usr/bin/libreoffice"

    @patch("shutil.which")
    def test_find_libreoffice_found_fallback(self, mock_which: MagicMock) -> None:
        def side_effect(name: str) -> str | None:
            return "/usr/bin/soffice" if name == "soffice" else None

        mock_which.side_effect = side_effect
        assert find_libreoffice() == "/usr/bin/soffice"

    @patch("shutil.which", return_value=None)
    def test_find_libreoffice_not_found(self, mock_which: MagicMock) -> None:
        with pytest.raises(SystemExit):
            find_libreoffice()


class TestConvertSpreadsheetToCsv:
    """Validates the conversion through the LibreOffice CLI."""

    @patch("subprocess.run")
    def test_conversion_success(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        input_path = tmp_path / "inventory.ods"
        # Create the CSV file simulating that libreoffice created it
        expected_csv = tmp_path / "inventory.csv"
        expected_csv.touch()

        result = convert_spreadsheet_to_csv(
            "/usr/bin/libreoffice", input_path, tmp_path
        )
        assert result == expected_csv

    @patch("subprocess.run")
    def test_conversion_fails_nonzero_exit(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="err", stderr="err")
        input_path = tmp_path / "inventory.ods"

        with pytest.raises(SystemExit):
            convert_spreadsheet_to_csv("/usr/bin/libreoffice", input_path, tmp_path)

    @patch("subprocess.run")
    def test_conversion_success_but_no_output_file(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        input_path = tmp_path / "inventory.ods"
        # Libreoffice finishes fine but the csv is NOT generated.
        with pytest.raises(SystemExit):
            convert_spreadsheet_to_csv("/usr/bin/libreoffice", input_path, tmp_path)


class TestFindRealHeader:
    """Validates the heuristic to find the real header amid the garbage."""

    def test_happy_path_first_row(self) -> None:
        rows = [["UUID", "IP", "MAC", "EXTRA"]]
        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]
        idx, header = find_real_header(rows, rundeck)
        assert idx == 0
        assert header == rows[0]

    def test_skip_garbage_rows(self) -> None:
        rows = [
            ["General Inventory", "", "", ""],
            ["UUID", "IP", "MAC", "EXTRA"],
            ["123", "10.0.0.1", "aa:bb", "data"],
        ]
        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]
        idx, header = find_real_header(rows, rundeck)
        assert idx == 1
        assert header == rows[1]

    def test_missing_required_columns(self) -> None:
        rows = [
            ["UUID", "IP", "EXTRA"],  # MAC is missing
        ]
        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]
        with pytest.raises(SystemExit):
            find_real_header(rows, rundeck)

    def test_required_columns_duplicated(self) -> None:
        rows = [
            ["UUID", "IP", "MAC", "IP"],  # duplicate IP
        ]
        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]
        with pytest.raises(SystemExit):
            find_real_header(rows, rundeck)


class TestProcessCsv:
    """Validates the sanitization and validation of the processed CSV."""

    def test_process_csv_happy_path(self, tmp_path: Path) -> None:
        source_csv = tmp_path / "source.csv"
        out_csv = tmp_path / "out.csv"

        source_csv.write_text(
            'Garbage,,\nUUID,IP,MAC,EXTRA\n123,10.0.0.1,aa:bb,"hello\nworld"\n'
        )

        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]
        process_csv(source_csv, out_csv, rundeck)

        assert out_csv.exists()
        lines = out_csv.read_text().splitlines()
        # The garbage is removed and line breaks are sanitized
        assert lines[0] == '"UUID","IP","MAC","EXTRA"'
        assert lines[1] == '"123","10.0.0.1","aa:bb","hello world"'

    def test_process_csv_unnamed_columns(self, tmp_path: Path) -> None:
        source_csv = tmp_path / "source.csv"
        out_csv = tmp_path / "out.csv"
        # The fourth column has no name
        source_csv.write_text("UUID,IP,MAC,\n123,10.0.0.1,aa:bb,data\n")
        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]

        with pytest.raises(SystemExit):
            process_csv(source_csv, out_csv, rundeck)

    def test_process_csv_duplicated_unrequired_columns(self, tmp_path: Path) -> None:
        source_csv = tmp_path / "source.csv"
        out_csv = tmp_path / "out.csv"
        # Duplicate EXTRA (not required)
        source_csv.write_text("UUID,IP,MAC,EXTRA,EXTRA\n123,10.0.0.1,aa:bb,data,data\n")
        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]

        with pytest.raises(SystemExit):
            process_csv(source_csv, out_csv, rundeck)

    def test_process_csv_row_length_mismatch(self, tmp_path: Path) -> None:
        source_csv = tmp_path / "source.csv"
        out_csv = tmp_path / "out.csv"
        # Row 2 is missing 1 column
        source_csv.write_text(
            "UUID,IP,MAC,EXTRA\n123,10.0.0.1,aa:bb,data\n456,10.0.0.2,cc:dd\n"
        )
        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]

        with pytest.raises(SystemExit):
            process_csv(source_csv, out_csv, rundeck)

    def test_process_csv_oserror_on_open(self, tmp_path: Path) -> None:
        source_csv = tmp_path / "source.csv"
        out_csv = tmp_path / "out.csv"
        rundeck = [("UUID", 2)]

        # The file does not exist, it will raise FileNotFoundError (OSError)
        with pytest.raises(SystemExit):
            process_csv(source_csv, out_csv, rundeck)

    def test_process_csv_empty_file(self, tmp_path: Path) -> None:
        source_csv = tmp_path / "source.csv"
        out_csv = tmp_path / "out.csv"
        source_csv.touch()  # Empty file, 0 rows
        rundeck = [("UUID", 2)]

        with pytest.raises(SystemExit):
            process_csv(source_csv, out_csv, rundeck)


class TestMainPrepareMasterInventory:
    """Validates flows of the main input/output and cleanup layer."""

    @pytest.fixture
    def setup_files(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        input_ods = tmp_path / "inventory.ods"
        output_csv = tmp_path / "prepared.csv"
        header_list = tmp_path / "rundeck_header_list.txt"

        input_ods.write_text("dummy")  # Create dummy file
        header_list.write_text("UUID|2\nIP|1\n")
        return input_ods, output_csv, header_list

    def test_main_unsupported_extension(
        self, setup_files: tuple[Path, Path, Path]
    ) -> None:
        _, output_csv, header_list = setup_files
        # Unsupported .txt extension
        input_txt = output_csv.parent / "inventory.txt"
        input_txt.touch()

        with (
            patch.object(
                sys,
                "argv",
                ["script.py", str(input_txt), str(output_csv), str(header_list)],
            ),
            pytest.raises(SystemExit),
        ):
            main()

    def test_main_empty_input_file(self, setup_files: tuple[Path, Path, Path]) -> None:
        input_ods, output_csv, header_list = setup_files
        input_ods.write_text("")  # 0 bytes
        with (
            patch.object(
                sys,
                "argv",
                ["script.py", str(input_ods), str(output_csv), str(header_list)],
            ),
            pytest.raises(SystemExit),
        ):
            main()

    @patch(
        "inventory_pipeline.prepare_master_inventory.find_libreoffice",
        return_value="dummy_libreoffice",
    )
    @patch("inventory_pipeline.prepare_master_inventory.convert_spreadsheet_to_csv")
    def test_main_clean_temp_file_on_failure(
        self,
        mock_convert: MagicMock,
        mock_find: MagicMock,
        setup_files: tuple[Path, Path, Path],
    ) -> None:
        input_ods, output_csv, header_list = setup_files

        # We simulate that convert_spreadsheet_to_csv returns a CSV that has unnamed columns
        # This will make process_csv trigger a SystemExit before writing the final file.
        def convert_side_effect(lo: str, ip: Path, tmp: Path) -> Path:
            csv_path = tmp / "converted.csv"
            csv_path.write_text("UUID,IP,\n1,10.0.0.1,bad\n")
            return csv_path

        mock_convert.side_effect = convert_side_effect

        with (
            patch.object(
                sys,
                "argv",
                ["script.py", str(input_ods), str(output_csv), str(header_list)],
            ),
            pytest.raises(SystemExit),
        ):
            main()

        assert not output_csv.exists()
        # Verify that there are no temporary files
        tmp_files = list(output_csv.parent.glob(f".{output_csv.name}.*.tmp"))
        assert len(tmp_files) == 0

    @patch(
        "inventory_pipeline.prepare_master_inventory.find_libreoffice",
        return_value="dummy_libreoffice",
    )
    @patch("inventory_pipeline.prepare_master_inventory.convert_spreadsheet_to_csv")
    def test_main_happy_path(
        self,
        mock_convert: MagicMock,
        mock_find: MagicMock,
        setup_files: tuple[Path, Path, Path],
    ) -> None:
        input_ods, output_csv, header_list = setup_files

        def convert_side_effect(lo: str, ip: Path, tmp: Path) -> Path:
            csv_path = tmp / "converted.csv"
            csv_path.write_text("UUID,IP\n123,10.0.0.1\n")
            return csv_path

        mock_convert.side_effect = convert_side_effect

        with patch.object(
            sys,
            "argv",
            ["script.py", str(input_ods), str(output_csv), str(header_list)],
        ):
            main()

        assert output_csv.exists()
        assert output_csv.read_text().splitlines()[0] == '"UUID","IP"'
        assert output_csv.read_text().splitlines()[1] == '"123","10.0.0.1"'

    def test_main_input_output_collision(
        self, setup_files: tuple[Path, Path, Path]
    ) -> None:
        input_ods, _, header_list = setup_files
        with (
            patch.object(
                sys,
                "argv",
                ["script.py", str(input_ods), str(input_ods), str(header_list)],
            ),
            pytest.raises(SystemExit),
        ):
            main()

    def test_main_oserror_on_stat(self, setup_files: tuple[Path, Path, Path]) -> None:
        input_ods, output_csv, header_list = setup_files

        # is_file() calls stat() internally, so the first call
        # must succeed, and the second (explicit) one must fail.
        valid_stat = MagicMock()
        valid_stat.st_mode = 33188  # S_IFREG

        with (
            patch.object(
                sys,
                "argv",
                ["script.py", str(input_ods), str(output_csv), str(header_list)],
            ),
            patch.object(
                Path, "stat", side_effect=[valid_stat, OSError("Permission denied")]
            ),
            pytest.raises(SystemExit),
        ):
            main()
