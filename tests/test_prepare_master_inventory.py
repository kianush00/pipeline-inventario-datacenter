import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from prepare_master_inventory import (
    convert_spreadsheet_to_csv,
    find_libreoffice,
    find_real_header,
    main,
    process_csv,
)


class TestFindLibreOffice:
    """Valida la detección del ejecutable de LibreOffice."""

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
    """Valida la conversión mediante LibreOffice CLI."""

    @patch("subprocess.run")
    def test_conversion_success(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        input_path = tmp_path / "inventario.ods"
        # Crear el archivo CSV simulando que libreoffice lo creó
        expected_csv = tmp_path / "inventario.csv"
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
        input_path = tmp_path / "inventario.ods"

        with pytest.raises(SystemExit):
            convert_spreadsheet_to_csv("/usr/bin/libreoffice", input_path, tmp_path)

    @patch("subprocess.run")
    def test_conversion_success_but_no_output_file(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        input_path = tmp_path / "inventario.ods"
        # Libreoffice termina bien pero el csv NO se genera.
        with pytest.raises(SystemExit):
            convert_spreadsheet_to_csv("/usr/bin/libreoffice", input_path, tmp_path)


class TestFindRealHeader:
    """Valida la heurística para encontrar la cabecera real en medio de la basura."""

    def test_happy_path_first_row(self) -> None:
        rows = [["UUID", "IP", "MAC", "EXTRA"]]
        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]
        idx, header = find_real_header(rows, rundeck)
        assert idx == 0
        assert header == rows[0]

    def test_skip_garbage_rows(self) -> None:
        rows = [
            ["Inventario General", "", "", ""],
            ["UUID", "IP", "MAC", "EXTRA"],
            ["123", "10.0.0.1", "aa:bb", "data"],
        ]
        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]
        idx, header = find_real_header(rows, rundeck)
        assert idx == 1
        assert header == rows[1]

    def test_missing_required_columns(self) -> None:
        rows = [
            ["UUID", "IP", "EXTRA"],  # Falta MAC
        ]
        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]
        with pytest.raises(SystemExit):
            find_real_header(rows, rundeck)

    def test_required_columns_duplicated(self) -> None:
        rows = [
            ["UUID", "IP", "MAC", "IP"],  # IP duplicada
        ]
        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]
        with pytest.raises(SystemExit):
            find_real_header(rows, rundeck)


class TestProcessCsv:
    """Valida la sanitización y validación del CSV procesado."""

    def test_process_csv_happy_path(self, tmp_path: Path) -> None:
        source_csv = tmp_path / "source.csv"
        out_csv = tmp_path / "out.csv"

        source_csv.write_text(
            'Basura,,\nUUID,IP,MAC,EXTRA\n123,10.0.0.1,aa:bb,"hola\nmundo"\n'
        )

        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]
        process_csv(source_csv, out_csv, rundeck)

        assert out_csv.exists()
        lines = out_csv.read_text().splitlines()
        # Se elimina la basura y se sanitan los saltos de línea
        assert lines[0] == '"UUID","IP","MAC","EXTRA"'
        assert lines[1] == '"123","10.0.0.1","aa:bb","hola mundo"'

    def test_process_csv_unnamed_columns(self, tmp_path: Path) -> None:
        source_csv = tmp_path / "source.csv"
        out_csv = tmp_path / "out.csv"
        # La cuarta columna no tiene nombre
        source_csv.write_text("UUID,IP,MAC,\n123,10.0.0.1,aa:bb,data\n")
        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]

        with pytest.raises(SystemExit):
            process_csv(source_csv, out_csv, rundeck)

    def test_process_csv_duplicated_unrequired_columns(self, tmp_path: Path) -> None:
        source_csv = tmp_path / "source.csv"
        out_csv = tmp_path / "out.csv"
        # EXTRA duplicado (no requerido)
        source_csv.write_text("UUID,IP,MAC,EXTRA,EXTRA\n123,10.0.0.1,aa:bb,data,data\n")
        rundeck = [("UUID", 2), ("IP", 1), ("MAC", 1)]

        with pytest.raises(SystemExit):
            process_csv(source_csv, out_csv, rundeck)

    def test_process_csv_row_length_mismatch(self, tmp_path: Path) -> None:
        source_csv = tmp_path / "source.csv"
        out_csv = tmp_path / "out.csv"
        # Fila 2 le falta 1 columna
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

        # Archivo no existe, levantará FileNotFoundError (OSError)
        with pytest.raises(SystemExit):
            process_csv(source_csv, out_csv, rundeck)

    def test_process_csv_empty_file(self, tmp_path: Path) -> None:
        source_csv = tmp_path / "source.csv"
        out_csv = tmp_path / "out.csv"
        source_csv.touch()  # Archivo vacío, 0 filas
        rundeck = [("UUID", 2)]

        with pytest.raises(SystemExit):
            process_csv(source_csv, out_csv, rundeck)


class TestMainPrepareMasterInventory:
    """Valida flujos de la capa principal de entrada/salida y limpieza."""

    @pytest.fixture
    def setup_files(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        input_ods = tmp_path / "inventario.ods"
        output_csv = tmp_path / "prepared.csv"
        header_list = tmp_path / "rundeck_header_list.txt"

        input_ods.write_text("dummy")  # Create dummy file
        header_list.write_text("UUID|2\nIP|1\n")
        return input_ods, output_csv, header_list

    def test_main_unsupported_extension(
        self, setup_files: tuple[Path, Path, Path]
    ) -> None:
        _, output_csv, header_list = setup_files
        # Extension no soportada .txt
        input_txt = output_csv.parent / "inventario.txt"
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
        "prepare_master_inventory.find_libreoffice", return_value="dummy_libreoffice"
    )
    @patch("prepare_master_inventory.convert_spreadsheet_to_csv")
    def test_main_clean_temp_file_on_failure(
        self,
        mock_convert: MagicMock,
        mock_find: MagicMock,
        setup_files: tuple[Path, Path, Path],
    ) -> None:
        input_ods, output_csv, header_list = setup_files

        # Simulamos que convert_spreadsheet_to_csv devuelve un CSV que tiene columnas sin nombre
        # Esto hará que process_csv detone un SystemExit antes de escribir el archivo final.
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
        # Verificar que no hay temporales
        tmp_files = list(output_csv.parent.glob(f".{output_csv.name}.*.tmp"))
        assert len(tmp_files) == 0

    @patch(
        "prepare_master_inventory.find_libreoffice", return_value="dummy_libreoffice"
    )
    @patch("prepare_master_inventory.convert_spreadsheet_to_csv")
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

        # is_file() llama a stat() internamente, por lo que la primera llamada
        # debe ser exitosa, y la segunda (explícita) debe fallar.
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
