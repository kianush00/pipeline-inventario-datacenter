from pathlib import Path

import pytest

from merge_inventories import (
    build_parsed_data,
    find_master_duplicated_keys,
    is_invalid_key,
    is_replica,
    resolve_key_column,
    resolve_positions,
    validate_rows,
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


class TestIsReplica:
    """Valida la detección de réplicas en filas con el mismo UUID."""

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

    def test_exact_replica(self, base_positions: dict[str, int]) -> None:
        row_a = ["uuid1", "host1", "aa:bb", "10.0.0.1", "SN1", "info_A"]
        row_b = ["uuid1", "host1", "aa:bb", "10.0.0.1", "SN1", "info_B"]
        assert is_replica(row_a, row_b, base_positions) is True

    def test_replica_case_and_whitespace_tolerance(
        self, base_positions: dict[str, int]
    ) -> None:
        row_a = ["uuid1", " host1 ", "AA:BB", "10.0.0.1", "sn1", ""]
        row_b = ["uuid1", "host1", "aa:bb", "10.0.0.1", "SN1", ""]
        assert is_replica(row_a, row_b, base_positions) is True

    def test_replica_differs_in_mac(self, base_positions: dict[str, int]) -> None:
        row_a = ["uuid1", "host1", "aa:bb", "10.0.0.1", "SN1", ""]
        row_b = ["uuid1", "host1", "cc:dd", "10.0.0.1", "SN1", ""]
        assert is_replica(row_a, row_b, base_positions) is False

    def test_replica_out_of_bounds(self, base_positions: dict[str, int]) -> None:
        row_a = ["uuid1", "host1", "aa:bb", "10.0.0.1", "SN1", ""]
        row_b = ["uuid1", "host1", "aa:bb", "10.0.0.1"]
        assert is_replica(row_a, row_b, base_positions) is True

    def test_replica_missing_positions(self) -> None:
        row_a = ["uuid1", "host1"]
        row_b = ["uuid1", "host2"]
        assert is_replica(row_a, row_b, {}) is True


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

    def test_build_parsed_data_with_replicas(self) -> None:
        rows = [
            ["uuid1", "host1", "mac1", "ip1", "sn1"],
            ["uuid1", "host1", "mac1", "ip1", "sn1"],  # Perfect replica
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

    def test_find_master_with_replicas(self) -> None:
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
