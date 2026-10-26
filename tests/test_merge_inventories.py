import os
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from inventory_pipeline import merge_inventories
from inventory_pipeline.merge_inventories import (
    CliPaths,
    LoadedInventory,
    MergePlan,
    build_merge_pairs,
    build_parsed_data,
    extract_key,
    find_master_duplicated_keys,
    index_rows_by_unique_key,
    is_identity_duplicate,
    is_invalid_key,
    merge_row,
    parse_cli_paths,
    resolve_key_column,
    resolve_output_row,
    resolve_positions,
    validate_rows,
    warn_if_no_keys_match,
)
from inventory_pipeline.paths import (
    DEFAULT_HEADER_LIST_PATH,
    MERGED_CSV_NAME,
    OUTPUT_DIR,
)


class TestIsInvalidKey:
    """Valida los edge cases de claves invalidas."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("", True),
            ("   ", True),
            ("N/A", True),
            ("n/a", True),
            ("123e4567-e89b-12d3-a456-426614174000", False),
        ],
    )
    def test_invalid_keys(self, value: str, expected: bool) -> None:
        assert is_invalid_key(value) is expected


class TestIsIdentityDuplicate:
    """Valida la detección de duplicados de identidad en filas con el mismo UUID."""

    @pytest.fixture
    def base_positions(self) -> dict[str, int]:
        return {
            "UUID": 0,
            "Nombre maquina": 1,
            "MAC": 2,
            "IP": 3,
            "Serial Number": 4,
            "Other": 5,
        }

    def test_identity_duplicate(self, base_positions: dict[str, int]) -> None:
        row_a = ["uuid1", "host1", "aa:bb", "10.0.0.1", "SN1", "info_A"]
        row_b = ["uuid1", "host1", "aa:bb", "10.0.0.1", "SN1", "info_B"]
        assert is_identity_duplicate(row_a, row_b, base_positions) is True

    def test_identity_duplicate_case_and_whitespace_tolerance(
        self, base_positions: dict[str, int]
    ) -> None:
        row_a = ["uuid1", " host1 ", "AA:BB", "10.0.0.1", "sn1", ""]
        row_b = ["uuid1", "host1", "aa:bb", "10.0.0.1", "SN1", ""]
        assert is_identity_duplicate(row_a, row_b, base_positions) is True

    def test_identity_duplicate_differs_in_mac(
        self, base_positions: dict[str, int]
    ) -> None:
        row_a = ["uuid1", "host1", "aa:bb", "10.0.0.1", "SN1", ""]
        row_b = ["uuid1", "host1", "cc:dd", "10.0.0.1", "SN1", ""]
        assert is_identity_duplicate(row_a, row_b, base_positions) is False

    def test_identity_duplicate_out_of_bounds(
        self, base_positions: dict[str, int]
    ) -> None:
        row_a = ["uuid1", "host1", "aa:bb", "10.0.0.1", "SN1", ""]
        row_b = ["uuid1", "host1", "aa:bb", "10.0.0.1"]
        assert is_identity_duplicate(row_a, row_b, base_positions) is True

    def test_identity_duplicate_missing_positions(self) -> None:
        row_a = ["uuid1", "host1"]
        row_b = ["uuid1", "host2"]
        assert is_identity_duplicate(row_a, row_b, {}) is True


class TestBuildParsedData:
    """Valida la creación de inventario parseado con edge cases de UUIDs."""

    def test_build_parsed_data_clean(self) -> None:
        rows = [
            ["uuid1", "host1"],
            ["uuid2", "host2"],
        ]
        parsed, dups = build_parsed_data(
            rows, key_idx=0, positions={"Nombre maquina": 1}
        )
        assert len(parsed) == 2
        assert len(dups) == 0
        assert "uuid1" in parsed
        assert "uuid2" in parsed

    def test_build_parsed_data_with_identity_duplicates(self) -> None:
        rows = [
            ["uuid1", "host1", "mac1", "ip1", "sn1"],
            ["uuid1", "host1", "mac1", "ip1", "sn1"],  # Duplicado de identidad
        ]
        positions = {
            "Nombre maquina": 1,
            "MAC": 2,
            "IP": 3,
            "Serial Number": 4,
        }
        parsed, dups = build_parsed_data(rows, key_idx=0, positions=positions)
        assert len(parsed) == 1
        assert "uuid1" in parsed
        assert len(dups) == 0

    def test_build_parsed_data_with_clashing_duplicates(self) -> None:
        rows = [
            ["uuid1", "host1", "mac1", "ip1", "sn1"],
            ["uuid1", "host1", "mac2", "ip1", "sn1"],  # MAC differs
        ]
        positions = {
            "Nombre maquina": 1,
            "MAC": 2,
            "IP": 3,
            "Serial Number": 4,
        }
        parsed, dups = build_parsed_data(rows, key_idx=0, positions=positions)
        assert len(parsed) == 0
        assert "uuid1" in dups


class TestFindMasterDuplicatedKeys:
    """Valida la detección de duplicados en maestro."""

    def test_find_master_no_duplicates(self) -> None:
        rows = [
            ["uuid1", "host1"],
            ["uuid2", "host2"],
        ]
        dups = find_master_duplicated_keys(
            rows, key_idx=0, positions={"Nombre maquina": 1}
        )
        assert len(dups) == 0

    def test_find_master_with_identity_duplicates(self) -> None:
        rows = [
            ["uuid1", "host1", "mac1"],
            ["uuid1", "host1", "mac1"],
        ]
        positions = {"Nombre maquina": 1, "MAC": 2}
        dups = find_master_duplicated_keys(rows, key_idx=0, positions=positions)
        assert len(dups) == 0

    def test_find_master_with_clashing_duplicates(self) -> None:
        rows = [
            ["uuid1", "host1", "mac1"],
            ["uuid1", "host2", "mac1"],  # hostname differs
        ]
        positions = {"Nombre maquina": 1, "MAC": 2}
        dups = find_master_duplicated_keys(rows, key_idx=0, positions=positions)
        assert "uuid1" in dups
        assert len(dups) == 1


class TestIndexRowsByUniqueKey:
    """Valida la regla común de unicidad compartida por parseado y maestro."""

    POSITIONS: ClassVar[dict[str, int]] = {"Nombre maquina": 1, "MAC": 2}

    def test_first_row_kept_for_identity_duplicates_and_invalid_keys_ignored(
        self,
    ) -> None:
        rows = [
            ["u1", "h1", "m1", "extra-A"],
            ["u1", "h1", "m1", "extra-B"],
            ["N/A", "h2", "m2", "x"],
            ["", "h3", "m3", "x"],
        ]
        unique, dups = index_rows_by_unique_key(rows, 0, self.POSITIONS, "parseado")
        assert unique == {"u1": rows[0]}
        assert dups == set()

    def test_clashing_key_stays_excluded_even_with_further_rows(self) -> None:
        rows = [
            ["u1", "h1", "m1"],
            ["u1", "h2", "m1"],
            ["u1", "h1", "m1"],  # tercera fila: no debe reintroducir la clave
        ]
        unique, dups = index_rows_by_unique_key(rows, 0, self.POSITIONS, "maestro")
        assert unique == {}
        assert dups == {"u1"}

    def test_keys_are_normalized_to_lowercase(self) -> None:
        rows = [["UUID-1", "h1", "m1"], ["uuid-1", "h2", "m1"]]
        _, dups = index_rows_by_unique_key(rows, 0, self.POSITIONS, "maestro")
        assert dups == {"uuid-1"}

    @pytest.mark.parametrize("label", ["parseado", "maestro"])
    def test_logs_identify_the_inventory(
        self, label: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rows = [["u1", "h1", "m1"], ["u1", "h2", "m1"]]
        index_rows_by_unique_key(rows, 0, self.POSITIONS, label)
        assert (
            f"Clave duplicada en el inventario {label}: 'u1'" in capsys.readouterr().err
        )


class TestResolvePositions:
    """Valida la extracción de cabeceras."""

    def test_resolve_positions_happy_path(self) -> None:
        header_fields = ["A", "B", "C"]
        rundeck_header_list = [("B", 1), ("C", 2)]
        positions = resolve_positions(
            rundeck_header_list, header_fields, Path("dummy.csv")
        )
        assert positions["B"] == 1
        assert positions["C"] == 2

    def test_resolve_positions_missing_required(self) -> None:
        header_fields = ["A", "B"]
        rundeck_header_list = [("B", 1), ("C", 1)]
        with pytest.raises(SystemExit):
            resolve_positions(rundeck_header_list, header_fields, Path("dummy.csv"))

    def test_resolve_positions_missing_optional(self) -> None:
        header_fields = ["A", "B"]
        rundeck_header_list = [("B", 1), ("C", 0)]
        with pytest.raises(SystemExit):
            resolve_positions(rundeck_header_list, header_fields, Path("dummy.csv"))

    def test_resolve_positions_duplicated_headers(self) -> None:
        header_fields = ["A", "B", "A"]
        rundeck_header_list = [("A", 1), ("B", 1)]
        with pytest.raises(SystemExit):
            resolve_positions(rundeck_header_list, header_fields, Path("dummy.csv"))


class TestResolveKeyColumn:
    """Valida la resolución de la clave principal (flag 2)."""

    def test_resolve_key_column_happy_path(self) -> None:
        rundeck_header_list = [("A", 1), ("B", 2), ("C", 0)]
        key_column = resolve_key_column(rundeck_header_list)
        assert key_column == "B"

    def test_resolve_key_column_missing_flag2(self) -> None:
        rundeck_header_list = [("A", 1), ("B", 1)]
        with pytest.raises(SystemExit):
            resolve_key_column(rundeck_header_list)

    def test_resolve_key_column_multiple_flag2(self) -> None:
        rundeck_header_list = [("A", 2), ("B", 2)]
        with pytest.raises(SystemExit):
            resolve_key_column(rundeck_header_list)


class TestValidateRows:
    """Valida el filtro de filas en base al número de columnas."""

    def test_validate_rows_happy_path(self) -> None:
        rows = [["a", "b", "c"], ["d", "e", "f"]]
        valid = validate_rows(rows, 3, Path("dummy.csv"))
        assert len(valid) == 2
        assert valid == rows

    def test_validate_rows_invalid_lengths_skipped(self) -> None:
        rows = [
            ["a", "b", "c"],  # valid
            ["d", "e"],  # invalid (too short)
            ["f", "g", "h", "i"],  # invalid (too long)
        ]
        valid = validate_rows(rows, 3, Path("dummy.csv"))
        assert len(valid) == 1
        assert valid[0] == ["a", "b", "c"]


HEADER_LIST = "UUID|2\nNombre maquina|0\nMAC|1\nIP|1\nSerial Number|0\n"
CSV_HEADER = "UUID,Nombre maquina,MAC,IP,Serial Number,Extra"


class TestMainEndToEnd:
    """Caracteriza el flujo completo de main(): validaciones, fusión y escritura atómica."""

    @pytest.fixture
    def workdir(self, tmp_path: Path) -> Path:
        (tmp_path / "header.txt").write_text(HEADER_LIST, encoding="utf-8")
        return tmp_path

    @staticmethod
    def _write(path: Path, rows: list[str], header: str = CSV_HEADER) -> Path:
        path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _run_raw(monkeypatch: pytest.MonkeyPatch, *args: Path | str) -> None:
        monkeypatch.setattr(sys, "argv", ["merge_inventories.py", *map(str, args)])
        merge_inventories.main()

    @classmethod
    def _run(
        cls,
        monkeypatch: pytest.MonkeyPatch,
        parsed: Path,
        master: Path,
        output: Path,
        header_list: Path,
    ) -> None:
        cls._run_raw(
            monkeypatch,
            "--parsed",
            parsed,
            "--master",
            master,
            "--output",
            output,
            "--header-list",
            header_list,
        )

    def _paths(self, workdir: Path) -> tuple[Path, Path, Path, Path]:
        return (
            workdir / "parsed.csv",
            workdir / "master.csv",
            workdir / "out.csv",
            workdir / "header.txt",
        )

    def test_merge_semantics(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed, master, out, header = self._paths(workdir)
        # El parseado solo trae las 5 columnas definidas (sin Extra)
        parsed.write_text(
            "UUID,Nombre maquina,MAC,IP,Serial Number\n"
            "u1,h1,NEW-MAC,N/A,SN-PARSED\n"
            "u2,h2,mac2,ip2,SN2\n",
            encoding="utf-8",
        )
        self._write(
            master,
            [
                "u1,host-master,old-mac,old-ip,SN-MASTER,x1",  # fusiona MAC; IP N/A se conserva
                "u3,h3,m3,i3,SN3,x3",  # no existe en parseado: sin fusionar
                "N/A,h4,m4,i4,SN4,x4",  # clave inválida: sin fusionar
                "dup,h5,m5,i5,SN5,x5",  # clave duplicada en maestro
                "dup,h6,m6,i6,SN6,x6",
            ],
        )

        self._run(monkeypatch, parsed, master, out, header)

        assert out.read_text(encoding="utf-8").splitlines() == [
            CSV_HEADER,
            "u1,host-master,NEW-MAC,old-ip,SN-MASTER,x1",
            "u3,h3,m3,i3,SN3,x3",
            "N/A,h4,m4,i4,SN4,x4",
            "dup,h5,m5,i5,SN5,x5",
            "dup,h6,m6,i6,SN6,x6",
        ]
        assert not list(workdir.glob(".*.tmp"))

    def test_duplicated_key_in_parsed_is_not_merged(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed, master, out, header = self._paths(workdir)
        parsed.write_text(
            "UUID,Nombre maquina,MAC,IP,Serial Number\n"
            "u1,h1,mac-a,ip,SN\n"
            "u1,h1,mac-b,ip,SN\n",
            encoding="utf-8",
        )
        self._write(master, ["u1,h1,old,old-ip,SN,x"])

        self._run(monkeypatch, parsed, master, out, header)

        assert (
            out.read_text(encoding="utf-8").splitlines()[1] == "u1,h1,old,old-ip,SN,x"
        )

    @pytest.mark.parametrize(
        "args",
        [
            [],
            ["--parsed", "p.csv"],
            ["--master", "m.csv"],
            ["p.csv", "m.csv"],  # el formato posicional antiguo ya no es válido
        ],
    )
    def test_missing_required_options_exit(
        self, monkeypatch: pytest.MonkeyPatch, args: list[str]
    ) -> None:
        with pytest.raises(SystemExit):
            self._run_raw(monkeypatch, *args)

    def test_option_order_does_not_change_roles(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con opciones nombradas, el orden en la línea de comandos es irrelevante."""
        parsed, master, out, header = self._paths(workdir)
        parsed.write_text(
            "UUID,Nombre maquina,MAC,IP,Serial Number\nu1,h1,NEW,ip,SN\n",
            encoding="utf-8",
        )
        self._write(master, ["u1,h1,old,old-ip,SN,x"])

        self._run_raw(
            monkeypatch,
            "--master",
            master,
            "--header-list",
            header,
            "--output",
            out,
            "--parsed",
            parsed,
        )

        assert out.read_text(encoding="utf-8").splitlines()[1] == "u1,h1,NEW,ip,SN,x"

    def test_missing_input_file_exits(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed, master, out, header = self._paths(workdir)
        self._write(master, [])
        with pytest.raises(SystemExit):
            self._run(monkeypatch, parsed, master, out, header)

    @pytest.mark.parametrize("clobbered", ["parsed", "master"])
    def test_output_cannot_overwrite_inputs(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch, clobbered: str
    ) -> None:
        parsed, master, _, header = self._paths(workdir)
        parsed.write_text(
            "UUID,Nombre maquina,MAC,IP,Serial Number\n", encoding="utf-8"
        )
        self._write(master, [])
        output = parsed if clobbered == "parsed" else master
        with pytest.raises(SystemExit):
            self._run(monkeypatch, parsed, master, output, header)

    def test_temporary_file_removed_when_replace_fails(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed, master, out, header = self._paths(workdir)
        parsed.write_text(
            "UUID,Nombre maquina,MAC,IP,Serial Number\n", encoding="utf-8"
        )
        self._write(master, ["u1,h1,m,i,SN,x"])

        def failing_replace(
            src: str | os.PathLike[str], dst: str | os.PathLike[str]
        ) -> None:
            raise OSError("fallo simulado")

        monkeypatch.setattr(merge_inventories.os, "replace", failing_replace)

        with pytest.raises(OSError):
            self._run(monkeypatch, parsed, master, out, header)

        assert not out.exists()
        assert not list(workdir.glob(".*.tmp"))

    def test_swapped_arguments_abort_when_master_has_extra_columns(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed, master, out, header = self._paths(workdir)
        parsed.write_text(
            "UUID,Nombre maquina,MAC,IP,Serial Number\nu1,h1,m,i,SN\n",
            encoding="utf-8",
        )
        self._write(master, ["u1,h1,m,i,SN,x"])

        with pytest.raises(SystemExit):
            self._run(monkeypatch, master, parsed, out, header)

        assert not out.exists()

    def test_no_matching_keys_emits_warning_but_still_writes(
        self,
        workdir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        parsed, master, out, header = self._paths(workdir)
        parsed.write_text(
            "UUID,Nombre maquina,MAC,IP,Serial Number\nz9,hz,m,i,SN\n",
            encoding="utf-8",
        )
        self._write(master, ["u1,h1,m,i,SN,x"])

        self._run(monkeypatch, parsed, master, out, header)

        assert (
            "Ninguna clave del inventario maestro coincide" in capsys.readouterr().err
        )
        assert out.read_text(encoding="utf-8").splitlines()[1] == "u1,h1,m,i,SN,x"

    def test_row_with_wrong_column_count_is_discarded(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed, master, out, header = self._paths(workdir)
        parsed.write_text(
            "UUID,Nombre maquina,MAC,IP,Serial Number\n", encoding="utf-8"
        )
        self._write(master, ["u1,h1,m,i,SN,x", "truncada,h2"])

        self._run(monkeypatch, parsed, master, out, header)

        assert out.read_text(encoding="utf-8").splitlines() == [
            CSV_HEADER,
            "u1,h1,m,i,SN,x",
        ]


class TestParseCliPaths:
    """Valida el parseo de argumentos nombrados y sus valores por defecto."""

    def test_defaults_point_to_project_output_and_config(self) -> None:
        paths = parse_cli_paths(["--parsed", "parsed.csv", "--master", "master.csv"])
        assert paths == CliPaths(
            Path("parsed.csv"),
            Path("master.csv"),
            OUTPUT_DIR / MERGED_CSV_NAME,
            DEFAULT_HEADER_LIST_PATH,
        )

    def test_explicit_output_and_header_list(self) -> None:
        paths = parse_cli_paths(
            [
                "--parsed",
                "p.csv",
                "--master",
                "m.csv",
                "--output",
                "o.csv",
                "--header-list",
                "h.txt",
            ]
        )
        assert paths.output == Path("o.csv")
        assert paths.rundeck_header_list == Path("h.txt")

    @pytest.mark.parametrize(
        "argv",
        [[], ["--parsed", "p.csv"], ["--master", "m.csv"], ["p.csv", "m.csv"]],
    )
    def test_missing_required_options_exit(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit):
            parse_cli_paths(argv)

    def test_short_options_are_equivalent_to_long_ones(self) -> None:
        short = parse_cli_paths(
            ["-p", "p.csv", "-m", "m.csv", "-o", "o.csv", "-l", "h.txt"]
        )
        long = parse_cli_paths(
            [
                "--parsed",
                "p.csv",
                "--master",
                "m.csv",
                "--output",
                "o.csv",
                "--header-list",
                "h.txt",
            ]
        )
        assert short == long

    def test_unknown_option_exits(self) -> None:
        with pytest.raises(SystemExit):
            parse_cli_paths(["--parsed", "p", "--master", "m", "--otra", "x"])


class TestBuildMergePairs:
    """Valida que solo las columnas con flag 1 sean fusionables."""

    def test_only_flag_one_columns_are_paired(self) -> None:
        header_list = [("K", 2), ("A", 0), ("B", 1), ("C", 1)]
        master = {"K": 0, "A": 1, "B": 2, "C": 5}
        parsed = {"K": 3, "A": 0, "B": 1, "C": 2}
        assert build_merge_pairs(header_list, master, parsed) == [(2, 1), (5, 2)]


class TestMergeRow:
    """Valida la fusión pura de una fila."""

    def test_overrides_only_non_empty_and_does_not_mutate(self) -> None:
        master = ["k", "old1", "old2"]
        parsed = ["k", "new1", "N/A"]
        merged = merge_row(master, parsed, [(1, 1), (2, 2)])
        assert merged == ["k", "new1", "old2"]
        assert master == ["k", "old1", "old2"]

    @pytest.mark.parametrize("empty", ["", "  ", "N/A"])
    def test_empty_parsed_values_keep_master(self, empty: str) -> None:
        assert merge_row(["k", "old"], ["k", empty], [(1, 1)]) == ["k", "old"]


class TestResolveOutputRow:
    """Valida el destino de cada fila del maestro (LEFT JOIN)."""

    @pytest.fixture
    def plan(self) -> MergePlan:
        return MergePlan(
            key_master_idx=0,
            parsed_data={"u1": ["u1", "new"]},
            master_duplicated_keys={"dup"},
            merge_pairs=[(1, 1)],
        )

    def test_merges_when_key_is_valid_and_unique(self, plan: MergePlan) -> None:
        assert resolve_output_row(2, ["U1", "old"], plan) == ["U1", "new"]

    @pytest.mark.parametrize("key", ["", "N/A", "dup", "missing"])
    def test_keeps_row_unmerged(self, plan: MergePlan, key: str) -> None:
        row = [key, "old"]
        assert resolve_output_row(2, row, plan) == [key, "old"]

    def test_missing_key_logs_info_with_line_number(
        self, plan: MergePlan, capsys: pytest.CaptureFixture[str]
    ) -> None:
        resolve_output_row(7, ["missing", "old"], plan)
        assert "Línea 7" in capsys.readouterr().err


class TestExtractKey:
    """Valida la normalización de la clave de unión."""

    def test_strips_quotes_and_lowercases(self) -> None:
        assert extract_key(["x", '"UUID-1"'], 1) == "uuid-1"


class TestWarnIfNoKeysMatch:
    """Valida la advertencia de claves sin coincidencias."""

    @staticmethod
    def _master(rows: list[list[str]]) -> LoadedInventory:
        return LoadedInventory("", [], {}, rows)

    @staticmethod
    def _plan(parsed_keys: set[str]) -> MergePlan:
        return MergePlan(0, {k: [k] for k in parsed_keys}, set(), [])

    def test_warns_when_no_master_key_is_in_parsed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        warn_if_no_keys_match(self._master([["u1"], ["u2"]]), self._plan({"z"}))
        assert "[WARNING]" in capsys.readouterr().err

    def test_no_warning_with_at_least_one_match(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        warn_if_no_keys_match(self._master([["u1"], ["U2"]]), self._plan({"u2"}))
        assert capsys.readouterr().err == ""

    def test_no_warning_when_master_is_empty(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        warn_if_no_keys_match(self._master([]), self._plan(set()))
        assert capsys.readouterr().err == ""
