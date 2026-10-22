"""
Pruebas unitarias para export_to_netbox.py
"""

import argparse
import hashlib
import logging
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from pynetbox.core.query import RequestError  # type: ignore

import export_to_netbox
from export_to_netbox import (
    DEFAULT_DEVICE_TYPE_U_HEIGHT,
    CacheStore,
    CastType,
    ChoiceItemConfig,
    ChoiceSetConfig,
    ClusterTypeConfig,
    ClusterTypeDefaultConfig,
    ConfigValidationError,
    CsvColumnDef,
    CsvData,
    CsvIdentityCounts,
    CsvRow,
    CustomFieldConfig,
    DeviceRoleConfig,
    DeviceTypeAttributes,
    DeviceTypeKey,
    Endpoint,
    FieldMappingConfig,
    FieldParseError,
    InterfaceFingerprint,
    InterfaceRename,
    ManufModelCache,
    MockNetBoxRecord,
    NameCache,
    NetBoxApiError,
    NetBoxEndpoints,
    NetBoxMappingConfig,
    NetBoxObject,
    NetworkInterfaceData,
    NodeMappingConfig,
    NodeType,
    NodeTypesConfig,
    NumberedRow,
    RowSkipCondition,
    RowValidationError,
    SiteConfig,
    SiteNameCache,
    SiteScopedName,
    StatusDefaultConfig,
    SyncCounts,
    SyncResult,
    SyncStatus,
    ValidationError,
    _assign_ip,
    _assign_mac,
    _assign_primary_ipv4,
    _build_interface_cidr,
    _classify_rows,
    _create_record,
    _create_with_fallback_slug,
    _csv_interface_fingerprint,
    _describe_assignment,
    _ensure_custom_field,
    _execute_sync,
    _extract_concat_dot_value,
    _extract_raw_source_value,
    _find_by_uuid,
    _get_ip_role,
    _is_collision_error,
    _is_custom_field_changed,
    _is_name_safely_unique,
    _is_relation_changed,
    _is_uuid_safely_unique,
    _network_from_cidr,
    _parse_network_interfaces,
    _parse_single_network_interface,
    _prune_network_orphans,
    _prune_orphan_ips,
    _read_csv,
    _resolve_cluster,
    _resolve_default_or_empty,
    _resolve_device_role,
    _resolve_device_type,
    _resolve_existing_object,
    _resolve_field_value,
    _resolve_host_device,
    _resolve_interface_renames,
    _resolve_netbox_status,
    _sanitize_mac_address,
    _search_in_netbox,
    _split_ip_and_embedded_prefix,
    _sync_device_type_attributes,
    _sync_interfaces_for_object,
    _sync_rack_location,
    _sync_row,
    _sync_single_device_role,
    _sync_single_interface,
    _upsert_interface_record,
    _validate_csv_headers,
    _validate_interface_ip,
    _validate_select_choice,
    apply_cast,
    apply_update,
    build_payload,
    check_record_changes,
    concat_dot,
    count_inventory_uuids,
    count_machine_names,
    detect_interface_renames,
    ensure_cluster,
    ensure_device_type,
    ensure_dynamic_cluster_types,
    ensure_location,
    ensure_manufacturer,
    ensure_prefix,
    ensure_rack,
    ensure_site,
    extract_csv_value,
    format_diff_keys,
    generate_fallback_slug,
    get_netbox_object_id,
    get_node_type_from_row,
    get_or_create_cached,
    load_config,
    main,
    parse_bool_si_no,
    parse_device_type_attributes,
    parse_float,
    parse_float_to_int,
    parse_int,
    parse_int_gb_to_mb,
    parse_rack_position,
    precompute_cluster_type_map,
    precompute_device_type_attributes,
    process_interfaces_and_ips,
    rekey_interfaces_cache,
    select_device_type_attributes,
    slugify,
    sync_device,
    sync_vm,
    update_if_changed,
)


class TestSlugify:
    """Verifica la generación de slugs válidos para NetBox."""

    def test_simple_name(self) -> None:
        """Un nombre limpio se convierte a minúsculas sin alteraciones."""
        assert slugify("Produccion") == "produccion"

    def test_spaces_and_dashes(self) -> None:
        """Espacios y guiones múltiples se unifican en '-', y se conservan guiones bajos."""
        assert slugify("Data  Center__Principal--Rack") == "data-center__principal-rack"

    def test_special_characters(self) -> None:
        """Caracteres no alfanuméricos (excepto guiones) se eliminan."""
        assert slugify("Rack #3 (Piso 2)") == "rack-3-piso-2"

    def test_truncates_to_100_chars(self) -> None:
        """NetBox limita los slugs a 100 caracteres."""
        nombre_largo = "a" * 150
        resultado = slugify(nombre_largo)
        assert len(resultado) == 100
        assert resultado == "a" * 100

    def test_empty_string(self) -> None:
        """Un string vacío produce un slug vacío."""
        assert slugify("") == ""

    def test_strips_extreme_dashes(self) -> None:
        """Guiones o guiones bajos al inicio y final del slug se eliminan."""
        assert slugify("--__nombre__--") == "nombre"

    def test_removes_accents(self) -> None:
        """Caracteres acentuados se normalizan a ASCII válido para NetBox."""
        assert slugify("Producción") == "produccion"
        assert slugify("Genérico") == "generico"
        assert slugify("Niño") == "nino"
        assert slugify("Árbol_Cúbico") == "arbol_cubico"

    def test_slugify_accents_and_symbols_complex(self) -> None:
        """Comportamiento QA ante strings basura con símbolos y múltiples espacios."""
        assert slugify("  --H.P.!!__  ") == "hp"
        assert slugify("¿Quién es el #1?") == "quien-es-el-1"
        assert slugify("  -_H_P_-  ") == "h_p"
        assert slugify("   ___---___   ") == ""
        assert slugify("a" * 150) == "a" * 100  # truncamiento


class TestGenerateFallbackSlug:
    """Verifica la generación determinista de slugs de respaldo."""

    def test_slug_format_with_hash(self) -> None:
        """El slug de fallback tiene el formato 'base-XXXX' (hash MD5 de 4 chars)."""
        resultado = generate_fallback_slug("mi-slug", "Nombre Original")
        expected_hash = hashlib.md5(b"Nombre Original").hexdigest()[:4]
        assert resultado == f"mi-slug-{expected_hash}"

    def test_determinism(self) -> None:
        """La misma entrada siempre produce el mismo slug de fallback."""
        a = generate_fallback_slug("base", "Mismo Nombre")
        b = generate_fallback_slug("base", "Mismo Nombre")
        assert a == b

    def test_different_names_produce_different_hashes(self) -> None:
        """Nombres distintos generan sufijos hash distintos."""
        a = generate_fallback_slug("base", "Nombre A")
        b = generate_fallback_slug("base", "Nombre B")
        assert a != b

    def test_generate_fallback_slug(self) -> None:
        base = "srv-01"
        fallback = generate_fallback_slug(base, "SRV-01")
        assert fallback.startswith("srv-01-")
        assert len(fallback) == 7 + 4  # 'srv-01-' + 4 chars md5

    def test_success_on_retry(self) -> None:

        endpoint = MagicMock()
        mock_obj = MockNetBoxRecord(id=10, name="SRV", slug="srv-1234")

        # Simula error 400 por slug
        mock_req = MagicMock(status_code=400, reason="Collision")
        mock_req.json.return_value = {"slug": ["already exists"]}

        # Falla la primera, funciona la segunda
        endpoint.create.side_effect = [
            RequestError(mock_req),
            mock_obj,
        ]

        result = endpoint.name = "devices"
        result = _create_with_fallback_slug(endpoint, "SRV", name="SRV", slug="srv")
        assert result.id == 10
        assert endpoint.create.call_count == 2
        # Verifica que la segunda llamada uso un slug diferente
        call_kwargs = endpoint.create.call_args_list[1][1]
        assert call_kwargs["slug"] != "srv"
        assert call_kwargs["slug"].startswith("srv-")

    def test_failure_on_retry(self) -> None:

        endpoint = MagicMock()
        # Falla siempre por slug
        mock_req = MagicMock(status_code=400, reason="Persistent Collision")
        mock_req.json.return_value = {"slug": ["already exists"]}
        endpoint.name = "clusters"
        endpoint.create.side_effect = RequestError(mock_req)

        with pytest.raises(
            NetBoxApiError, match="Imposible crear objeto en 'devices' con nombre 'SRV'"
        ):
            endpoint.name = "devices"
            _create_with_fallback_slug(endpoint, "SRV", name="SRV", slug="srv")


class TestParseInt:
    """Verifica la conversión robusta de valores a enteros."""

    def test_valid_integer(self) -> None:
        assert parse_int("42") == 42

    def test_integer_with_spaces(self) -> None:
        """El strip() interno elimina espacios antes de parsear."""
        assert parse_int("  128  ") == 128

    def test_negative_integer(self) -> None:
        assert parse_int("-7") == -7

    def test_invalid_value_raises_error(self) -> None:
        with pytest.raises(ValueError, match="No es un número entero válido"):
            parse_int("abc")

    def test_float_string_raises_error(self) -> None:
        """Un float como '3.14' no es un entero válido."""
        with pytest.raises(ValueError, match="No es un número entero válido"):
            parse_int("3.14")


class TestParseFloat:
    """Verifica la conversión robusta de valores a decimales."""

    def test_valid_float(self) -> None:
        assert parse_float("3.14") == 3.14

    def test_integer_to_float(self) -> None:
        assert parse_float("42") == 42.0

    def test_float_with_comma(self) -> None:
        assert parse_float("3,14") == 3.14

    def test_float_with_spaces(self) -> None:
        assert parse_float("  -2.5  ") == -2.5

    def test_invalid_value_raises_error(self) -> None:
        with pytest.raises(ValueError, match="No es un número decimal válido"):
            parse_float("abc")


class TestParseFloatToInt:
    """Verifica la conversión robusta de floats a enteros más cercanos."""

    def test_float_string_dot(self) -> None:
        """Punto decimal se redondea correctamente."""
        assert parse_float_to_int("50.5") == 50
        assert parse_float_to_int("50.6") == 51

    def test_float_string_comma(self) -> None:
        """Coma decimal se soporta y redondea."""
        assert parse_float_to_int("50,5") == 50
        assert parse_float_to_int("50,6") == 51

    def test_integer_string(self) -> None:
        """Strings enteros se mantienen intactos."""
        assert parse_float_to_int("42") == 42

    def test_with_spaces(self) -> None:
        """Espacios alrededor se limpian."""
        assert parse_float_to_int("  10.2  ") == 10

    def test_invalid_value_raises_error(self) -> None:
        """Strings no numéricos lanzan ValueError."""
        with pytest.raises(ValueError, match="No es un valor numérico válido"):
            parse_float_to_int("cincuenta")

    def test_none_value(self) -> None:
        """None lanza ValueError."""
        with pytest.raises(ValueError, match="No es un valor numérico válido"):
            parse_float_to_int(None)


class TestParseRackPosition:
    """Verifica las reglas de Device.position de NetBox (>= 1, múltiplos de 0.5)."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("12", 12.0),
            ("12.5", 12.5),
            ("12,5", 12.5),
            ("  1  ", 1.0),
            ("1.0", 1.0),
            ("42.5", 42.5),
        ],
    )
    def test_valid_positions(self, raw: str, expected: float) -> None:
        """Enteros y medias unidades (con punto o coma) son válidos."""
        assert parse_rack_position(raw) == expected

    @pytest.mark.parametrize("raw", ["12.25", "12.3", "1.75", "2.75"])
    def test_non_half_unit_increments_rejected(self, raw: str) -> None:
        """Valores que no son múltiplos de 0.5 se rechazan antes de llamar a la API."""
        with pytest.raises(ValueError, match="múltiplo de 0.5"):
            parse_rack_position(raw)

    @pytest.mark.parametrize("raw", ["0", "0.5", "-1", "-2.5"])
    def test_below_minimum_rejected(self, raw: str) -> None:
        """Posiciones menores a 1 (incluidas 0.5 y negativas) se rechazan."""
        with pytest.raises(ValueError, match="mayor o igual a 1"):
            parse_rack_position(raw)

    @pytest.mark.parametrize("raw", ["abc", "", "U12", "nan", "inf", "-inf"])
    def test_non_numeric_or_non_finite_rejected(self, raw: str) -> None:
        """Texto, vacío y valores no finitos lanzan ValueError."""
        with pytest.raises(ValueError):
            parse_rack_position(raw)

    def test_none_rejected(self) -> None:
        """None lanza ValueError con mensaje de posición numérica."""
        with pytest.raises(ValueError, match="posición de rack numérica"):
            parse_rack_position(None)


class TestParseIntGbToMb:
    """Verifica la conversión de GB a MB (NetBox espera MB para RAM)."""

    def test_gb_integer(self) -> None:
        """8 GB = 8000 MB."""
        assert parse_int_gb_to_mb("8") == 8000

    def test_gb_decimal(self) -> None:
        """0.5 GB = 500 MB."""
        assert parse_int_gb_to_mb("0.5") == 500

    def test_gb_decimal_comma(self) -> None:
        """Soporta formato hispano: 7,8 GB = 7800 MB."""
        assert parse_int_gb_to_mb("7,8") == 7800

    def test_with_spaces(self) -> None:
        assert parse_int_gb_to_mb("  16  ") == 16000

    def test_invalid_value_raises_error(self) -> None:
        with pytest.raises(ValueError, match="No es un valor numérico válido"):
            parse_int_gb_to_mb("no-es-numero")

    def test_zero_gb(self) -> None:
        """0 GB = 0 MB."""
        assert parse_int_gb_to_mb("0") == 0

    def test_parse_float_string_raises_error(self) -> None:
        with pytest.raises(ValueError):
            parse_int_gb_to_mb("invalid_number")


class TestParseBoolSiNo:
    """Verifica la conversión de valores a booleanos con variantes en español."""

    @pytest.mark.parametrize(
        "valor",
        ["si", "sí", "SI", "Sí", "yes", "YES", "true", "True", "1"],
    )
    def test_true_values(self, valor: str) -> None:
        assert parse_bool_si_no(valor) is True

    @pytest.mark.parametrize(
        "valor",
        ["no", "NO", "No", "false", "False", "0"],
    )
    def test_false_values(self, valor: str) -> None:
        assert parse_bool_si_no(valor) is False

    def test_invalid_value_raises_error(self) -> None:
        with pytest.raises(ValueError, match="No es 'si' ni 'no'"):
            parse_bool_si_no("quizás")

    def test_with_spaces(self) -> None:
        """El strip() interno tolera espacios."""
        assert parse_bool_si_no("  si  ") is True


class TestApplyCast:
    """Verifica el dispatcher de casteos dinámicos definidos en el YAML."""

    def test_cast_int(self) -> None:
        assert apply_cast("42", CastType.INT, "cpu_cores") == 42

    def test_cast_int_gb_to_mb(self) -> None:
        assert apply_cast("8", CastType.INT_GB_TO_MB, "memory") == 8000

    def test_cast_rack_position(self) -> None:
        assert apply_cast("12,5", CastType.RACK_POSITION, "position") == 12.5

    def test_invalid_rack_position_raises_row_validation_error(self) -> None:
        """Una posición no múltiplo de 0.5 se reporta como error de fila con contexto."""
        with pytest.raises(RowValidationError, match="Valor inválido.*position"):
            apply_cast("12.25", CastType.RACK_POSITION, "position")

    def test_cast_bool_si_no(self) -> None:
        assert apply_cast("si", CastType.BOOL_SI_NO, "is_virtual") is True

    def test_cast_lower(self) -> None:
        assert apply_cast("MAYUS", CastType.LOWER, "uuid") == "mayus"
        assert apply_cast(None, CastType.LOWER, "uuid") is None

    def test_invalid_cast_raises_row_validation_error(self) -> None:
        """apply_cast envuelve ValueError en RowValidationError con contexto."""
        with pytest.raises(RowValidationError, match="Valor inválido.*cpu_cores"):
            apply_cast("abc", CastType.INT, "cpu_cores")

    def test_none_with_int_cast_raises_error(self) -> None:
        """None no es convertible a int; se espera RowValidationError."""
        with pytest.raises(RowValidationError, match="Valor inválido.*campo"):
            apply_cast(None, CastType.INT, "campo")

    def test_non_numeric_string_with_int_cast_raises_error(self) -> None:
        """Un string no-numérico con cast INT produce RowValidationError."""
        with pytest.raises(RowValidationError, match="Valor inválido.*campo"):
            apply_cast("texto", CastType.INT, "campo")


class TestApplyCastEdgeCases:
    """Casos específicos del branch final de apply_cast."""

    def test_valid_int_returns_int(self) -> None:
        """Verifica que un int ya parseado sale como int."""
        assert apply_cast("100", CastType.INT, "pos_u") == 100

    def test_gb_to_mb_precision(self) -> None:
        """Verifica la precisión del redondeo: 1.5 GB = 1500 MB."""
        assert apply_cast("1.5", CastType.INT_GB_TO_MB, "memory") == 1500


class TestGetNetboxObjectId:
    """Verifica la extracción segura del ID de un objeto NetBox."""

    def test_object_with_id(self, mock_record: MockNetBoxRecord) -> None:
        """Un objeto con id=42 retorna 42."""
        assert get_netbox_object_id(mock_record) == 42

    def test_object_without_id_returns_zero(self) -> None:
        """Un objeto con id=0 (mock de dry-run) retorna 0."""
        obj = MockNetBoxRecord(id=0, name="dry-run-mock")
        assert get_netbox_object_id(obj) == 0

    def test_object_with_string_id_converts(self) -> None:
        """El ID se convierte a int incluso si viene como string."""
        # MockNetBoxRecord usa Pydantic que coerce automáticamente,
        # pero verificamos el contrato de get_netbox_object_id.
        obj = MockNetBoxRecord(id=99, name="test")
        assert get_netbox_object_id(obj) == 99
        assert isinstance(get_netbox_object_id(obj), int)


class TestExtractCsvValue:
    """Verifica la extracción y sanitización de valores del CSV."""

    def test_normal_value(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["machine_name"].source
        row = {col_name: "SRV-01"}
        assert extract_csv_value(row, "machine_name", config) == "SRV-01"

    def test_empty_value(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["machine_name"].source
        row = {col_name: ""}
        assert extract_csv_value(row, "machine_name", config) == ""

    def test_na_value_converts_to_empty(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["machine_name"].source
        row = {col_name: "N/A"}
        assert extract_csv_value(row, "machine_name", config) == ""

    def test_strict_map_raises_error_when_unmapped(
        self, config: NetBoxMappingConfig
    ) -> None:
        """Prueba destructiva: si se provee un valor que NO está en el map, strict_map=True lanza error"""
        col_name = config.csv_columns["machine_type"].source
        row = {col_name: "INVALID_TYPE"}
        with pytest.raises(
            RowValidationError, match="no está definido en el mapa configurado"
        ):
            extract_csv_value(row, "machine_type", config, strict_map=True)

    def test_non_strict_map_returns_fallback_when_unmapped(
        self, config: NetBoxMappingConfig
    ) -> None:
        """Prueba de fallback: si falla el mapeo y strict_map=False, debe devolver el fallback (o el raw_val si fallback=None)"""
        col_name = config.csv_columns["machine_type"].source
        row = {col_name: "INVALID_TYPE"}

        # Con fallback definido
        assert (
            extract_csv_value(
                row, "machine_type", config, strict_map=False, fallback="default_vm"
            )
            == "default_vm"
        )

        # Sin fallback definido, devuelve crudo
        assert (
            extract_csv_value(row, "machine_type", config, strict_map=False)
            == "INVALID_TYPE"
        )

    def test_fallback_returned_when_cell_empty(
        self, config: NetBoxMappingConfig
    ) -> None:
        """Prueba de celda vacía: el fallback actúa directamente sin intentar el mapeo"""
        col_name = config.csv_columns["status"].source
        row = {col_name: ""}
        assert extract_csv_value(row, "status", config, fallback="active") == "active"

    def test_required_empty_field_raises_error(
        self, config: NetBoxMappingConfig
    ) -> None:
        col_name = config.csv_columns["machine_name"].source
        row = {col_name: " "}
        with pytest.raises(
            RowValidationError, match="obligatorio 'machine_name' está vacío"
        ):
            extract_csv_value(row, "machine_name", config, strict_extract=True)

    def test_nonexistent_alias_raises_critical_error(
        self, config: NetBoxMappingConfig
    ) -> None:
        col_name = config.csv_columns["machine_name"].source
        row = {col_name: "SRV-01"}
        with pytest.raises(
            ConfigValidationError, match="El alias 'alias_falso' solicitado"
        ):
            extract_csv_value(row, "alias_falso", config)

    def test_column_with_null_source_returns_empty(
        self, config: NetBoxMappingConfig
    ) -> None:
        config_copy = config.model_copy(deep=True)
        col = config_copy.csv_columns["machine_name"].model_copy(
            update={"source": None}
        )
        config_copy.csv_columns["machine_name"] = col
        row = {"Cualquier_Columna": "SRV-01"}
        assert extract_csv_value(row, "machine_name", config_copy) == ""

    def test_null_source_and_required_raises_error(
        self, config: NetBoxMappingConfig
    ) -> None:
        config_copy = config.model_copy(deep=True)
        col = config_copy.csv_columns["machine_name"].model_copy(
            update={"source": None}
        )
        config_copy.csv_columns["machine_name"] = col
        row = {"Cualquier_Columna": "SRV-01"}
        with pytest.raises(
            RowValidationError, match="no está mapeado en la configuración"
        ):
            extract_csv_value(row, "machine_name", config_copy, strict_extract=True)


class TestConcatDot:
    """Verifica la concatenación de campos de texto."""

    def test_valid_parts(self, config: NetBoxMappingConfig) -> None:
        assert concat_dot(["A", "B", "C"], config) == "A. B. C"

    def test_empty_filtered(self, config: NetBoxMappingConfig) -> None:
        assert concat_dot(["A", "N/A", "", "C"], config) == "A. C"

        assert concat_dot(["", "N/A"], config) == ""


class TestExtractConcatDotValue:
    """QA Tests para _extract_concat_dot_value."""

    def test_extract_multiple_non_empty_values(
        self, config: NetBoxMappingConfig
    ) -> None:
        """
        Escenario: Múltiples columnas source tienen valores.
        El código NO debe levantar RowValidationError (a diferencia de coalesce),
        sino que debe unirlos correctamente.
        """
        row = {"col1": "Data1", "col2": "Data2", "col3": "N/A", "col4": "Data4"}
        source = ["col1", "col2", "col3", "col4", "missing_col"]

        result = _extract_concat_dot_value(row, source, config)
        # col3 ('N/A') es purgado. missing_col devuelve '' y se purga.
        assert result == "Data1. Data2. Data4"


class TestGetNodeTypeFromRow:
    """Verifica la resolución del NodeType desde una fila CSV."""

    def test_device_type(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["machine_type"].source
        row = {col_name: "Dedicada"}
        assert get_node_type_from_row(row, config) == NodeType.DEVICE

    def test_vm_type(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["machine_type"].source
        row = {col_name: "VM"}
        assert get_node_type_from_row(row, config) == NodeType.VIRTUAL_MACHINE

    def test_unmapped_type_raises_error(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["machine_type"].source
        row = {col_name: "Desconocido"}
        with pytest.raises(
            RowValidationError, match="no está definido en el mapa configurado"
        ):
            get_node_type_from_row(row, config)

    def test_missing_key_in_row(self, config: NetBoxMappingConfig) -> None:
        with pytest.raises(
            RowValidationError, match="El campo 'machine_type' está vacío."
        ):
            get_node_type_from_row({}, config)


class TestCountMachineNames:
    """Verifica el contador de nombres de máquinas."""

    def test_correct_count(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["machine_name"].source
        rows = [
            {col_name: "SRV-01"},
            {col_name: "SRV-02"},
            {col_name: "SRV-01"},
            {col_name: "SRV-03"},
            {col_name: ""},
            {col_name: "   "},
            {},  # None from get
        ]
        counts = count_machine_names(rows, config)
        assert counts["SRV-01"] == 2
        assert counts["SRV-02"] == 1
        assert counts["SRV-03"] == 1
        assert counts["SRV-04"] == 0
        assert counts[""] == 0
        assert counts["   "] == 0


class TestCountInventoryUuids:
    """Verifica el contador de UUIDs de inventario."""

    def test_counts_normalized_and_ignores_empty(
        self, config: NetBoxMappingConfig
    ) -> None:
        col_uuid = config.csv_columns["inventory_uuid"].source
        rows = [
            {col_uuid: "ABC-1"},
            {col_uuid: "abc-1"},
            {col_uuid: "abc-2"},
            {col_uuid: ""},
            {col_uuid: "N/A"},
            {},
        ]
        counts = count_inventory_uuids(rows, config)
        assert counts["abc-1"] == 2
        assert counts["abc-2"] == 1
        assert sum(counts.values()) == 3


class TestValidateCsvHeaders:
    """Verifica la validación de los encabezados del CSV."""

    def test_valid_headers(self, config: NetBoxMappingConfig) -> None:
        # Extraemos los requeridos de la configuración cargada
        headers = [
            col.source
            for col in config.csv_columns.values()
            if col.required and col.source
        ]
        # Debería pasar sin lanzar excepciones
        _validate_csv_headers(headers, config)

    def test_headers_with_allowed_extras(self, config: NetBoxMappingConfig) -> None:
        headers = [
            col.source
            for col in config.csv_columns.values()
            if col.required and col.source
        ]
        headers.extend(["Columna Extra 1", "Columna Extra 2"])
        _validate_csv_headers(headers, config)

    def test_missing_required_headers_raises_error(
        self, config: NetBoxMappingConfig
    ) -> None:
        headers = ["Solo una columna irrelevante"]
        with pytest.raises(
            ValueError, match="El CSV no contiene las siguientes columnas obligatorias"
        ):
            _validate_csv_headers(headers, config)


class TestFieldMappingConfig:
    """Verifica las validaciones de configuración de mapeo."""

    def test_list_source_requires_transform(self) -> None:
        """Una lista en source requiere un transform explícito."""
        with pytest.raises(
            ValidationError, match="debes definir explícitamente un 'transform'"
        ):
            FieldMappingConfig(target="mi_campo", source=["col1", "col2"])

    def test_list_source_with_transform_ok(self) -> None:
        """Una lista en source es válida si tiene transform."""
        config = FieldMappingConfig(
            target="mi_campo", source=["col1", "col2"], transform="concat_dot"
        )
        assert config.source == ["col1", "col2"]


class TestLoadConfig:
    """Verifica la carga y validación del archivo YAML de configuración."""

    def test_successful_real_yaml_load(self, yaml_path) -> None:
        """Verifica que el archivo yaml del proyecto se carga correctamente."""
        config = load_config(yaml_path)
        assert isinstance(config, NetBoxMappingConfig)
        assert len(config.csv_columns) > 0

    def test_nonexistent_yaml_raises_error(self, tmp_path) -> None:
        """Una ruta falsa lanza ConfigValidationError."""
        with pytest.raises(
            ConfigValidationError, match="No se encontró el archivo de mapping"
        ):
            load_config(tmp_path / "falso.yaml")

    def test_invalid_yaml_raises_error(self, tmp_path) -> None:
        """Un YAML mal formado o que no cumple el esquema lanza ConfigValidationError."""
        yaml_invalido = tmp_path / "invalido.yaml"
        yaml_invalido.write_text("csv_columns: [lista_en_lugar_de_dict]")

        with pytest.raises(ConfigValidationError, match="Error de validación"):
            load_config(yaml_invalido)


class TestResolveDefaultOrEmpty:
    """Verifica la política de fallback (default > None > error)."""

    def test_with_default(self) -> None:
        assert (
            _resolve_default_or_empty("mi_default", is_optional=True, target="campo")
            == "mi_default"
        )
        assert (
            _resolve_default_or_empty("mi_default", is_optional=False, target="campo")
            == "mi_default"
        )

    def test_optional_without_default_returns_none(self) -> None:
        assert _resolve_default_or_empty(None, is_optional=True, target="campo") is None

    def test_required_without_default_raises_error(self) -> None:
        with pytest.raises(RowValidationError, match="obligatorio 'campo' está vacío"):
            _resolve_default_or_empty(None, is_optional=False, target="campo")


class TestExtractRawSourceValue:
    """Verifica la extracción cruda de origen simple o multi-columna."""

    def test_single_source(self) -> None:
        row = {"ColA": "ValorA"}
        assert _extract_raw_source_value(row, "ColA") == "ValorA"

    def test_list_source_raises_error(self) -> None:
        # Extraer una lista sin transform explicit no está permitido
        row = {"ColA": "ValorA", "ColB": "ValorB"}
        with pytest.raises(
            TypeError, match="No se puede extraer un valor crudo a partir de una lista"
        ):
            _extract_raw_source_value(row, ["ColA", "ColB"])

    def test_missing_key_returns_empty(self) -> None:
        row = {"ColA": "ValorA"}
        assert _extract_raw_source_value(row, "ColX") == ""


class TestValidateSelectChoice:
    """Verifica la validación contra choice_sets."""

    @pytest.fixture
    def cf_select(self) -> CustomFieldConfig:
        return CustomFieldConfig(
            name="my_cf",
            label="My CF",
            type="select",
            required=False,
            choice_set=ChoiceSetConfig(
                name="my_choices",
                choices=[
                    ChoiceItemConfig(value="Opcion1", label="Op 1"),
                    ChoiceItemConfig(value="Opcion2", label="Op 2"),
                ],
            ),
        )

    def test_valid_choice(self, cf_select: CustomFieldConfig) -> None:
        assert (
            _validate_select_choice("Opcion1", cf_select, "my_cf", is_optional=False)
            == "Opcion1"
        )

    def test_invalid_choice_optional_returns_none(
        self, cf_select: CustomFieldConfig
    ) -> None:
        assert (
            _validate_select_choice("OpcionX", cf_select, "my_cf", is_optional=True)
            is None
        )

    def test_invalid_choice_required_raises_error(
        self, cf_select: CustomFieldConfig
    ) -> None:
        with pytest.raises(
            RowValidationError,
            match="Valor inválido 'OpcionX' para el campo requerido 'my_cf'",
        ):
            _validate_select_choice("OpcionX", cf_select, "my_cf", is_optional=False)

    @pytest.fixture
    def cf_multiselect(self) -> CustomFieldConfig:
        return CustomFieldConfig(
            name="my_cf_multi",
            label="My CF Multi",
            type="multiselect",
            required=False,
            choice_set=ChoiceSetConfig(
                name="multi_choices",
                choices=[
                    ChoiceItemConfig(value="Opcion1", label="Op 1"),
                    ChoiceItemConfig(value="Opcion2", label="Op 2"),
                    ChoiceItemConfig(value="Opcion3", label="Op 3"),
                ],
            ),
        )

    def test_multiselect_valid_single(self, cf_multiselect: CustomFieldConfig) -> None:
        assert _validate_select_choice(
            "Opcion1", cf_multiselect, "my_cf_multi", is_optional=False
        ) == ["Opcion1"]

    def test_multiselect_valid_multiple(
        self, cf_multiselect: CustomFieldConfig
    ) -> None:
        assert _validate_select_choice(
            "Opcion1, Opcion3", cf_multiselect, "my_cf_multi", is_optional=False
        ) == ["Opcion1", "Opcion3"]

    def test_multiselect_invalid_optional_returns_none(
        self, cf_multiselect: CustomFieldConfig
    ) -> None:
        assert (
            _validate_select_choice(
                "Opcion1, Invalida", cf_multiselect, "my_cf_multi", is_optional=True
            )
            is None
        )

    def test_multiselect_invalid_required_raises_error(
        self, cf_multiselect: CustomFieldConfig
    ) -> None:
        with pytest.raises(
            RowValidationError,
            match="Valores inválidos '\\['Invalida'\\]' para el campo requerido 'my_cf_multi'",
        ):
            _validate_select_choice(
                "Opcion1, Invalida", cf_multiselect, "my_cf_multi", is_optional=False
            )

    def test_non_select_field_is_noop(self) -> None:
        cf_text = CustomFieldConfig(
            name="my_cf", label="My CF", type="text", required=False
        )
        assert (
            _validate_select_choice(
                "CualquierCosa", cf_text, "my_cf", is_optional=False
            )
            == "CualquierCosa"
        )


class TestResolveFieldValue:
    """Verifica el flujo Pipe-and-Filter de resolución de campos."""

    def test_simple_field(self, config: NetBoxMappingConfig) -> None:
        row = {"SO": "Ubuntu"}
        field_def = FieldMappingConfig(target="os", source="SO")
        assert (
            _resolve_field_value(row, field_def, config, is_optional=True) == "Ubuntu"
        )

    def test_mapped_field(self, config: NetBoxMappingConfig) -> None:
        # machine_type ya tiene mapeo en YAML: Dedicada -> dedicated
        col_name = config.csv_columns["machine_type"].source
        row = {col_name: "Dedicada"}
        field_def = FieldMappingConfig(target="tipo", source=col_name)
        assert (
            _resolve_field_value(row, field_def, config, is_optional=True)
            == "dedicated"
        )

    def test_cast_field(self, config: NetBoxMappingConfig) -> None:
        row = {"RAM": "8"}
        field_def = FieldMappingConfig(
            target="memory", source="RAM", cast=CastType.INT_GB_TO_MB
        )
        assert _resolve_field_value(row, field_def, config, is_optional=True) == 8000

    def test_concat_dot_field(self, config: NetBoxMappingConfig) -> None:
        row = {"Nota1": "Hola", "Nota2": "Mundo"}
        field_def = FieldMappingConfig(
            target="comments", source=["Nota1", "Nota2"], transform="concat_dot"
        )
        assert (
            _resolve_field_value(row, field_def, config, is_optional=True)
            == "Hola. Mundo"
        )

    def test_coalesce_field(self, config: NetBoxMappingConfig) -> None:
        row = {"S1": "", "S2": "Val2", "S3": ""}
        field_def = FieldMappingConfig(
            target="serial", source=["S1", "S2", "S3"], transform="coalesce"
        )
        assert _resolve_field_value(row, field_def, config, is_optional=True) == "Val2"

    def test_coalesce_field_conflict(self, config: NetBoxMappingConfig) -> None:
        row = {"S1": "Val1", "S2": "Val2"}
        field_def = FieldMappingConfig(
            target="serial", source=["S1", "S2"], transform="coalesce"
        )
        with pytest.raises(RowValidationError, match="Conflicto"):
            _resolve_field_value(row, field_def, config, is_optional=True)

    def test_empty_with_default(self, config: NetBoxMappingConfig) -> None:
        row = {"Col": ""}
        field_def = FieldMappingConfig(target="target", source="Col")
        assert (
            _resolve_field_value(
                row, field_def, config, is_optional=True, default="def_val"
            )
            == "def_val"
        )

    def test_empty_required_raises_error(self, config: NetBoxMappingConfig) -> None:
        row = {"Col": ""}
        field_def = FieldMappingConfig(target="target", source="Col")
        with pytest.raises(RowValidationError, match="obligatorio 'target' está vacío"):
            _resolve_field_value(row, field_def, config, is_optional=False)


class TestBuildPayload:
    """Verifica la construcción del payload de NetBox."""

    def test_valid_payload(self, config: NetBoxMappingConfig) -> None:
        row = {"hostname": "SRV-01", "memoria": "8", "rack_u": "10"}
        native_maps = [
            FieldMappingConfig(target="name", source="hostname"),
            FieldMappingConfig(
                target="memory", source="memoria", cast=CastType.INT_GB_TO_MB
            ),
        ]

        # 'inventory_uuid' está definido en el YAML real
        custom_maps = [FieldMappingConfig(target="inventory_uuid", source="rack_u")]

        payload = build_payload(row, native_maps, custom_maps, config)

        assert payload["name"] == "SRV-01"
        assert payload["memory"] == 8000
        assert "custom_fields" in payload
        assert payload["custom_fields"]["inventory_uuid"] == "10"

    def test_none_fields_are_excluded(self, config: NetBoxMappingConfig) -> None:
        row = {"hostname": "SRV-01", "memoria": ""}
        native_maps = [
            FieldMappingConfig(target="name", source="hostname"),
            # Al ser vacío y opcional, resolverá a None
            FieldMappingConfig(target="memory", source="memoria"),
        ]
        payload = build_payload(row, native_maps, [], config)
        assert "name" in payload
        assert "memory" not in payload

    def test_unique_empty_is_none(self, config: NetBoxMappingConfig) -> None:
        row = {"serial": ""}
        native_maps = [
            FieldMappingConfig(target="serial", source="serial", is_unique=True)
        ]
        # Al ser is_unique=True y resolverse como "", el assigner lo pasa a None y lo excluye
        payload = build_payload(row, native_maps, [], config)
        assert "serial" not in payload

    def test_custom_map_not_in_definitions_raises_error(
        self, config: NetBoxMappingConfig
    ) -> None:
        row = {"hostname": "SRV-01"}
        custom_maps = [FieldMappingConfig(target="cf_inexistente", source="hostname")]
        with pytest.raises(
            ConfigValidationError, match="no está definido en custom_field_definitions"
        ):
            build_payload(row, [], custom_maps, config)

    def test_custom_fields_nested(self, config: NetBoxMappingConfig) -> None:
        # Usando campos reales del mapping (vCPUs y Tipo de maquina)
        native_map = [FieldMappingConfig(source="Nombre", target="name")]
        custom_map = [
            FieldMappingConfig(source="vCPUs", target="cpu_cores"),
            FieldMappingConfig(source="Entorno", target="environment"),
        ]
        row = {"Nombre": "SRV", "vCPUs": "4", "Entorno": "production"}

        payload = build_payload(row, native_map, custom_map, config)

        assert payload["name"] == "SRV"
        assert "custom_fields" in payload
        assert payload["custom_fields"]["cpu_cores"] == "4"
        assert payload["custom_fields"]["environment"] == "production"


class TestResolveNetboxStatus:
    """Verifica la resolución del estado en NetBox."""

    def test_mapped_status(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["status"].source
        row = {
            col_name: "Activo",
            config.csv_columns["machine_type"].source: "Dedicada",
        }
        assert _resolve_netbox_status(row, config, NodeType.DEVICE) == "active"

    def test_unmapped_status_raises_error(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["status"].source
        row = {col_name: "Desconocido", config.csv_columns["machine_type"].source: "VM"}
        with pytest.raises(
            RowValidationError, match="no está definido en el mapa configurado"
        ):
            _resolve_netbox_status(row, config, NodeType.VIRTUAL_MACHINE)

    def test_empty_status_fallback(self, config: NetBoxMappingConfig) -> None:
        row = {config.csv_columns["machine_type"].source: "Dedicada"}
        assert _resolve_netbox_status(row, config, NodeType.DEVICE) == "inventory"


class TestResolveDeviceRole:
    """Verifica la búsqueda y fallback de roles."""

    def test_role_exists(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["role"].source
        row = {col_name: "DB", config.csv_columns["machine_name"].source: "SRV-01"}
        cache: NameCache = {"db": MockNetBoxRecord(id=10, name="DB")}
        assert _resolve_device_role(row, cache, config) == 10

    def test_role_not_exists_uses_others(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["role"].source
        row = {
            col_name: "Inexistente",
            config.csv_columns["machine_name"].source: "SRV-01",
        }
        cache: NameCache = {"others": MockNetBoxRecord(id=99, name="Others")}
        assert _resolve_device_role(row, cache, config) == 99

    def test_others_not_exists_raises_error(self, config: NetBoxMappingConfig) -> None:
        row = {config.csv_columns["machine_name"].source: "SRV-01"}
        cache: NameCache = {}
        with pytest.raises(
            RowValidationError, match="No existe el DeviceRole 'Others'"
        ):
            _resolve_device_role(row, cache, config)


SITE_ID = 1  # Site configurado usado por las pruebas de resolución de identidad


def _counts(
    names: dict[str, int] | None = None, uuids: dict[str, int] | None = None
) -> CsvIdentityCounts:
    """Construye los conteos globales del CSV para las pruebas de resolución."""
    return CsvIdentityCounts(Counter(names or {}), Counter(uuids or {}))


def _node(record_id: int, uuid: str = "", name: str = "SRV-01") -> MockNetBoxRecord:
    """Construye un objeto NetBox simulado con su custom field `inventory_uuid`."""
    return MockNetBoxRecord(
        id=record_id, name=name, custom_fields={"inventory_uuid": uuid}
    )


class TestFindByUuid:
    """Verifica la búsqueda por UUID con coincidencia exacta."""

    def test_returns_exact_matches(self) -> None:
        endpoint = MagicMock()
        record = _node(5, "uuid-123")
        endpoint.filter.return_value = [record]

        assert _find_by_uuid(endpoint, "uuid-123") == [record]
        endpoint.filter.assert_called_once_with(cf_inventory_uuid="uuid-123")

    def test_discards_partial_matches_from_loose_filter(self) -> None:
        """NetBox filtra de forma parcial por defecto: 'abc' no debe resolver 'abc-extra'."""
        endpoint = MagicMock()
        endpoint.filter.return_value = [_node(1, "abc-extra"), _node(2, "abc")]

        result = _find_by_uuid(endpoint, "abc")
        assert [r.id for r in result] == [2]

    def test_case_insensitive_match(self) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = [_node(1, "UUID-ABC")]
        assert len(_find_by_uuid(endpoint, "uuid-abc")) == 1


class TestIsUuidSafelyUnique:
    """Verifica la validación de unicidad de UUIDs."""

    def test_unique_in_both(self) -> None:
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"
        existing: list[Any] = [_node(1, "u1")]
        assert _is_uuid_safely_unique(endpoint, existing, "u1", Counter({"u1": 1}))

    def test_duplicate_in_netbox(self, caplog: pytest.LogCaptureFixture) -> None:
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"
        existing: list[Any] = [_node(1, "u1"), _node(2, "u1")]
        with caplog.at_level(logging.WARNING):
            assert not _is_uuid_safely_unique(
                endpoint, existing, "u1", Counter({"u1": 1})
            )
        assert "no es único" in caplog.text

    def test_duplicate_in_csv(self) -> None:
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"
        existing: list[Any] = [_node(1, "u1")]
        assert not _is_uuid_safely_unique(endpoint, existing, "u1", Counter({"u1": 2}))

    def test_not_in_netbox_is_not_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Un nodo nuevo (sin coincidencias en NetBox) no es una ambigüedad."""
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"
        with caplog.at_level(logging.WARNING):
            assert not _is_uuid_safely_unique(endpoint, [], "u1", Counter({"u1": 1}))
        assert caplog.text == ""


class TestResolveExistingObject:
    """Verifica la resolución de identidad en dos etapas: UUID único y luego nombre único."""

    def test_unique_uuid_resolves_without_name_lookup(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoint = MagicMock()
        record = _node(5, "uuid-123", name="OTRO-NOMBRE")
        endpoint.filter.return_value = [record]

        result = _resolve_existing_object(
            endpoint,
            "SRV-01",
            "uuid-123",
            _counts({"SRV-01": 1}, {"uuid-123": 1}),
            config,
            SITE_ID,
        )

        assert result == [record]
        endpoint.filter.assert_called_once_with(cf_inventory_uuid="uuid-123")

    def test_falls_back_to_name_when_uuid_not_found(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoint = MagicMock()
        by_name = _node(6, "")
        endpoint.filter.side_effect = [[], [by_name]]

        result = _resolve_existing_object(
            endpoint,
            "SRV-01",
            "uuid-123",
            _counts({"SRV-01": 1}, {"uuid-123": 1}),
            config,
            SITE_ID,
        )

        assert result == [by_name]
        assert endpoint.filter.call_count == 2
        # La etapa de nombre se acota al Site configurado.
        endpoint.filter.assert_called_with(name="SRV-01", site_id=SITE_ID)

    def test_name_stage_accepts_uuid_with_different_case(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoint = MagicMock()
        record = _node(7, "UUID-123-ABC")
        endpoint.filter.side_effect = [[], [record]]

        result = _resolve_existing_object(
            endpoint,
            "SRV-01",
            "uuid-123-abc",
            _counts({"SRV-01": 1}, {"uuid-123-abc": 1}),
            config,
            SITE_ID,
        )
        assert result == [record]

    def test_not_found_returns_empty_to_create(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = []

        result = _resolve_existing_object(
            endpoint,
            "SRV-01",
            "uuid-123",
            _counts({"SRV-01": 1}, {"uuid-123": 1}),
            config,
            SITE_ID,
        )
        assert result == []

    def test_empty_uuid_searches_only_by_name(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoint = MagicMock()
        record = _node(7, "")
        endpoint.filter.return_value = [record]

        result = _resolve_existing_object(
            endpoint, "SRV-01", "", _counts({"SRV-01": 1}), config, SITE_ID
        )

        assert result == [record]
        endpoint.filter.assert_called_once_with(name="SRV-01", site_id=SITE_ID)

    def test_duplicated_uuid_in_csv_falls_back_to_name(
        self, config: NetBoxMappingConfig
    ) -> None:
        """Dos filas con el mismo UUID no se pisan: cada una se resuelve por su nombre."""
        endpoint = MagicMock()
        # Etapa UUID: el nodo de la otra fila ya existe con ese UUID; etapa nombre: no existe.
        endpoint.filter.side_effect = [[_node(1, "dup", name="SRV-A")], []]

        result = _resolve_existing_object(
            endpoint,
            "SRV-B",
            "dup",
            _counts({"SRV-A": 1, "SRV-B": 1}, {"dup": 2}),
            config,
            SITE_ID,
        )
        assert result == []

    def test_duplicated_uuid_in_netbox_resolves_by_name(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoint = MagicMock()
        mine = _node(2, "dup", name="SRV-B")
        endpoint.filter.side_effect = [
            [_node(1, "dup", name="SRV-A"), mine],
            [mine],
        ]

        result = _resolve_existing_object(
            endpoint,
            "SRV-B",
            "dup",
            _counts({"SRV-B": 1}, {"dup": 1}),
            config,
            SITE_ID,
        )
        assert result == [mine]

    def test_ambiguous_name_after_uuid_failure_skips(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoint = MagicMock()
        endpoint.name = "devices"
        endpoint.filter.side_effect = [
            [],
            [_node(1, "", name="SRV"), _node(2, "", name="SRV")],
        ]

        # TODO: me aparece este warning de sonarqube: Refactor this exception test to have only one invocation possibly throwing an exception
        with pytest.raises(RowSkipCondition):
            _resolve_existing_object(
                endpoint,
                "SRV",
                "uuid-1",
                _counts({"SRV": 1}, {"uuid-1": 1}),
                config,
                SITE_ID,
            )

    def test_duplicated_name_in_csv_skips(self, config: NetBoxMappingConfig) -> None:
        endpoint = MagicMock()
        endpoint.name = "devices"
        endpoint.filter.return_value = [_node(1, "")]

        with pytest.raises(RowSkipCondition):
            _resolve_existing_object(
                endpoint, "SRV-01", "", _counts({"SRV-01": 2}), config, SITE_ID
            )

    def test_name_match_with_different_uuid_skips(
        self, config: NetBoxMappingConfig
    ) -> None:
        """No se suplanta la identidad de un nodo que ya tiene otro UUID."""
        endpoint = MagicMock()
        endpoint.name = "devices"
        endpoint.filter.side_effect = [[], [_node(1, "otro-uuid")]]

        with pytest.raises(RowSkipCondition, match="distinto UUID"):
            _resolve_existing_object(
                endpoint,
                "SRV-01",
                "uuid-123",
                _counts({"SRV-01": 1}, {"uuid-123": 1}),
                config,
                SITE_ID,
            )

    def test_name_match_with_empty_uuid_is_adopted(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoint = MagicMock()
        record = _node(1, "")
        endpoint.filter.side_effect = [[], [record]]

        result = _resolve_existing_object(
            endpoint,
            "SRV-01",
            "uuid-123",
            _counts({"SRV-01": 1}, {"uuid-123": 1}),
            config,
            SITE_ID,
        )
        assert result == [record]

    def test_uuid_owned_by_other_site_skips_without_name_fallback(
        self, config: NetBoxMappingConfig
    ) -> None:
        """Un UUID que ya existe en otro Site no se actualiza ni se duplica."""
        endpoint = MagicMock()
        foreign = MockNetBoxRecord.model_validate(
            {
                "id": 9,
                "name": "SRV-01",
                "custom_fields": {"inventory_uuid": "uuid-123"},
                "site": SimpleNamespace(id=SITE_ID + 1),
            }
        )
        endpoint.filter.return_value = [foreign]

        with pytest.raises(RowSkipCondition, match="otro Site"):
            _resolve_existing_object(
                endpoint,
                "SRV-01",
                "uuid-123",
                _counts({"SRV-01": 1}, {"uuid-123": 1}),
                config,
                SITE_ID,
            )
        endpoint.filter.assert_called_once()  # no cae a la etapa de nombre

    def test_uuid_in_configured_site_resolves(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoint = MagicMock()
        mine = MockNetBoxRecord.model_validate(
            {
                "id": 9,
                "custom_fields": {"inventory_uuid": "uuid-123"},
                "site": SimpleNamespace(id=SITE_ID),
            }
        )
        endpoint.filter.return_value = [mine]

        result = _resolve_existing_object(
            endpoint,
            "SRV-01",
            "uuid-123",
            _counts({"SRV-01": 1}, {"uuid-123": 1}),
            config,
            SITE_ID,
        )
        assert result == [mine]

    def test_same_name_in_other_site_is_not_a_match(
        self, config: NetBoxMappingConfig
    ) -> None:
        """El filtro por Site excluye homónimos de otros Sites: el nodo se crea."""
        endpoint = MagicMock()
        endpoint.filter.return_value = []

        result = _resolve_existing_object(
            endpoint, "SRV-01", "", _counts({"SRV-01": 1}), config, SITE_ID
        )

        assert result == []
        endpoint.filter.assert_called_once_with(name="SRV-01", site_id=SITE_ID)

    def test_nonexistent_site_has_no_nodes_to_match(
        self, config: NetBoxMappingConfig
    ) -> None:
        """Site simulado (ID 0, dry-run): no se consulta por nombre."""
        endpoint = MagicMock()

        result = _resolve_existing_object(
            endpoint, "SRV-01", "", _counts({"SRV-01": 1}), config, 0
        )

        assert result == []
        endpoint.filter.assert_not_called()


def _sync_node(
    endpoint: Endpoint,
    payload: dict[str, Any],
    name: str,
    uuid: str,
    counts: CsvIdentityCounts,
    config: NetBoxMappingConfig,
    site_id: int,
    dry_run: bool,
) -> SyncResult:
    """Fases 2 y 3 de la sincronización de un nodo: resolver identidad y ejecutar."""
    existing = _resolve_existing_object(endpoint, name, uuid, counts, config, site_id)
    return _execute_sync(endpoint, payload, existing, name, uuid, dry_run)


class _FakeNodeEndpoint:
    """Endpoint en memoria con estado, para validar el flujo completo de identidad."""

    name = "devices"
    url = "http://test/api/dcim/devices/"

    def __init__(self) -> None:
        self.records: list[MockNetBoxRecord] = []

    def filter(self, **kwargs: Any) -> list[MockNetBoxRecord]:
        if "cf_inventory_uuid" in kwargs:
            wanted = kwargs["cf_inventory_uuid"]
            return [r for r in self.records if _uuid_of(r) == wanted]
        return [
            r
            for r in self.records
            if r.name == kwargs["name"]
            and getattr(r.site, "id", None) == kwargs["site_id"]  # type: ignore[attr-defined]
        ]

    def create(self, **payload: Any) -> MockNetBoxRecord:
        """Crea el registro; `site` se guarda como objeto anidado, como hace pynetbox."""
        site_ref = SimpleNamespace(id=payload.pop("site", SITE_ID))
        record = MockNetBoxRecord(id=len(self.records) + 1, site=site_ref, **payload)  # type: ignore[call-arg]
        self.records.append(record)
        return record


def _uuid_of(record: MockNetBoxRecord) -> str:
    return str(getattr(record, "custom_fields", {}).get("inventory_uuid", ""))


class TestValidateSyncDuplicatedUuid:
    """Escenario end-to-end: dos filas del CSV con el mismo UUID y nombres distintos."""

    @staticmethod
    def _payload(name: str, uuid: str) -> dict[str, Any]:
        return {
            "name": name,
            "site": SITE_ID,
            "custom_fields": {"inventory_uuid": uuid},
        }

    def test_rows_do_not_overwrite_each_other_across_runs(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoint = _FakeNodeEndpoint()
        counts = _counts({"SRV-A": 1, "SRV-B": 1}, {"dup": 2})
        rows = [("SRV-A", "dup"), ("SRV-B", "dup")]

        for run in range(2):
            statuses = [
                _sync_node(
                    cast(Endpoint, endpoint),
                    self._payload(name, uuid),
                    name,
                    uuid,
                    counts,
                    config,
                    SITE_ID,
                    dry_run=False,
                ).status
                for name, uuid in rows
            ]
            expected = (
                [SyncStatus.CREATED] * 2 if run == 0 else [SyncStatus.UNCHANGED] * 2
            )
            assert statuses == expected

        assert sorted(r.name for r in endpoint.records) == ["SRV-A", "SRV-B"]


class TestValidateSyncMultiSite:
    """Escenario end-to-end: nodos homónimos o con el mismo UUID en otro Site."""

    OTHER_SITE_ID = SITE_ID + 1

    @staticmethod
    def _payload(name: str, uuid: str, site_id: int) -> dict[str, Any]:
        return {
            "name": name,
            "site": site_id,
            "custom_fields": {"inventory_uuid": uuid},
        }

    def test_same_name_in_other_site_is_never_touched(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoint = _FakeNodeEndpoint()
        endpoint.create(**self._payload("SRV-01", "uuid-other", self.OTHER_SITE_ID))
        counts = _counts({"SRV-01": 1}, {"uuid-mine": 1})

        result = _sync_node(
            cast(Endpoint, endpoint),
            self._payload("SRV-01", "uuid-mine", SITE_ID),
            "SRV-01",
            "uuid-mine",
            counts,
            config,
            SITE_ID,
            dry_run=False,
        )

        assert result.status == SyncStatus.CREATED
        assert len(endpoint.records) == 2
        other = endpoint.records[0]
        assert _uuid_of(other) == "uuid-other"
        assert other.site.id == self.OTHER_SITE_ID  # type: ignore[attr-defined]

    def test_uuid_in_other_site_is_skipped_not_moved_or_duplicated(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoint = _FakeNodeEndpoint()
        endpoint.create(**self._payload("SRV-01", "uuid-1", self.OTHER_SITE_ID))

        with pytest.raises(RowSkipCondition, match="otro Site"):
            _sync_node(
                cast(Endpoint, endpoint),
                self._payload("SRV-01", "uuid-1", SITE_ID),
                "SRV-01",
                "uuid-1",
                _counts({"SRV-01": 1}, {"uuid-1": 1}),
                config,
                SITE_ID,
                dry_run=False,
            )

        assert len(endpoint.records) == 1
        assert endpoint.records[0].site.id == self.OTHER_SITE_ID  # type: ignore[attr-defined]


class TestEnsureCustomFieldFilterLogic:
    """Verifica que `filter_logic` del YAML se envíe a NetBox al crear el Custom Field."""

    @pytest.mark.parametrize(
        ("filter_logic", "expected_in_payload"), [("exact", True), (None, False)]
    )
    def test_filter_logic_in_create_payload(
        self, filter_logic: str | None, expected_in_payload: bool
    ) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = []
        cf_def = CustomFieldConfig.model_validate(
            {
                "name": "inventory_uuid",
                "label": "UUID",
                "type": "text",
                "filter_logic": filter_logic,
            }
        )

        _ensure_custom_field(endpoint, {}, cf_def, ["dcim.device"], None, False)

        create_kwargs = endpoint.create.call_args.kwargs
        assert ("filter_logic" in create_kwargs) is expected_in_payload
        if expected_in_payload:
            assert create_kwargs["filter_logic"] == "exact"

    def test_inventory_uuid_uses_exact_filter_in_yaml(
        self, config: NetBoxMappingConfig
    ) -> None:
        defs = {cf.name: cf for cf in config.get_all_custom_field_defs()}
        assert defs["inventory_uuid"].filter_logic == "exact"


class TestIsNameSafelyUnique:
    """Verifica la validación de unicidad de nombres."""

    def test_unique_in_both(self) -> None:
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"
        existing: list[Any] = [MockNetBoxRecord(id=1)]
        counts = Counter({"SRV-01": 1})
        assert _is_name_safely_unique(endpoint, existing, "SRV-01", counts) is True

    def test_duplicate_in_netbox(self) -> None:
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"
        existing: list[Any] = [MockNetBoxRecord(id=1), MockNetBoxRecord(id=2)]
        counts = Counter({"SRV-01": 1})
        assert _is_name_safely_unique(endpoint, existing, "SRV-01", counts) is False

    def test_duplicate_in_csv(self) -> None:
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"
        existing: list[Any] = [MockNetBoxRecord(id=1)]
        counts = Counter({"SRV-01": 2})
        assert _is_name_safely_unique(endpoint, existing, "SRV-01", counts) is False


class TestExecuteSync:
    """Verifica el flujo de creación, actualización y dry-run."""

    def test_create(self) -> None:
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"
        mock_created = MockNetBoxRecord(id=100)
        endpoint.create.return_value = mock_created

        status, obj_id, _ = _execute_sync(
            endpoint, {"name": "SRV-01"}, [], "SRV-01", "uuid-1", dry_run=False
        )
        assert status == SyncStatus.CREATED
        assert obj_id == 100
        endpoint.create.assert_called_once_with(name="SRV-01")

    def test_update(self) -> None:
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"

        mock_existing = MagicMock()
        mock_existing.id = 50
        mock_existing.status = "active"  # Unchanged field
        mock_existing.name = "SRV-01"  # Changed field
        del mock_existing._init_cache
        mock_existing.update.return_value = True  # Hubo cambios

        payload = {
            "name": "SRV-01-nuevo",
            "status": "active",  # Debe ser ignorado por el diff
        }

        status, obj_id, _ = _execute_sync(
            endpoint,
            payload,
            [mock_existing],
            "SRV-01",
            "uuid-1",
            dry_run=False,
        )
        assert status == SyncStatus.UPDATED
        assert obj_id == 50
        # Se asegura que solo se envía el diff parcial, excluyendo "status"
        mock_existing.update.assert_called_once_with({"name": "SRV-01-nuevo"})

    def test_unchanged(self) -> None:
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"

        mock_existing = MagicMock()
        mock_existing.id = 50
        mock_existing.name = "SRV-01"
        del mock_existing._init_cache

        status, obj_id, _ = _execute_sync(
            endpoint,
            {"name": "SRV-01"},
            [mock_existing],
            "SRV-01",
            "uuid-1",
            dry_run=False,
        )
        assert status == SyncStatus.UNCHANGED
        assert obj_id == 50
        mock_existing.update.assert_not_called()

    def test_update_returns_false(self) -> None:
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"

        mock_existing = MagicMock()
        mock_existing.id = 50
        mock_existing.name = "SRV-01"
        del mock_existing._init_cache
        mock_existing.update.return_value = (
            False  # Hubo diff, pero update devolvió False
        )

        status, obj_id, _ = _execute_sync(
            endpoint,
            {"name": "SRV-01-nuevo"},
            [mock_existing],
            "SRV-01",
            "uuid-1",
            dry_run=False,
        )
        assert status == SyncStatus.UNCHANGED
        assert obj_id == 50
        mock_existing.update.assert_called_once_with({"name": "SRV-01-nuevo"})

    def test_dry_run_create(self) -> None:
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"

        status, obj_id, _ = _execute_sync(
            endpoint, {"name": "SRV-01"}, [], "SRV-01", "uuid-1", dry_run=True
        )
        assert status == SyncStatus.CREATED
        assert obj_id == 0  # obj_id simulado es 0
        endpoint.create.assert_not_called()

    def test_dry_run_update(self) -> None:
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"

        mock_existing = MagicMock()
        mock_existing.id = 50
        mock_existing.status = "active"
        mock_existing.name = "SRV-01"
        # Eliminar _init_cache para forzar el fallback de _check_record_changes
        del mock_existing._init_cache

        payload = {"name": "SRV-01-nuevo", "status": "active"}

        status, obj_id, _ = _execute_sync(
            endpoint,
            payload,
            [mock_existing],
            "SRV-01",
            "uuid-1",
            dry_run=True,
        )
        assert status == SyncStatus.UPDATED
        assert obj_id == 50
        mock_existing.update.assert_not_called()

    def test_execute_sync_record_update_error(self) -> None:
        endpoint = MagicMock()
        mock_existing = MagicMock()
        mock_existing.id = 50
        # Simula cambio (para que haga update) y que update() falle

        mock_existing.update.side_effect = RequestError(
            MagicMock(status_code=400, reason="NetBox Reject")
        )
        del mock_existing._init_cache

        with pytest.raises(
            NetBoxApiError, match="Error actualizando NodeType.VIRTUAL_MACHINE 'SRV'"
        ):
            _execute_sync(
                endpoint,
                {"name": "SRV-nuevo"},
                [mock_existing],
                "SRV",
                "uuid",
                dry_run=False,
            )

    def test_execute_sync_record_create_error(self) -> None:
        endpoint = MagicMock()
        endpoint.name = "clusters"
        endpoint.create.side_effect = RequestError(
            MagicMock(status_code=400, reason="NetBox Reject")
        )

        with pytest.raises(
            NetBoxApiError, match="Error al crear NodeType.VIRTUAL_MACHINE 'SRV'"
        ):
            _execute_sync(
                endpoint,
                {"name": "SRV"},
                [],
                "SRV",
                "uuid",
                dry_run=False,
            )


class TestValidateInterfaceIp:
    """Verifica la validación de direcciones IP crudas."""

    def test_valid_ipv4(self) -> None:
        assert _validate_interface_ip("192.168.1.10", "eth0") == "192.168.1.10"

    def test_valid_ipv6(self) -> None:
        assert _validate_interface_ip("2001:db8::1", "eth0") == "2001:db8::1"

    def test_invalid_ip_raises_error(self) -> None:
        with pytest.raises(FieldParseError, match="no es una dirección IP válida"):
            _validate_interface_ip("256.0.0.1", "eth0")


class TestBuildInterfaceCidr:
    """Verifica la combinación de IP y prefijo/máscara a CIDR canónico."""

    def test_prefix_only(self) -> None:
        assert _build_interface_cidr("192.168.1.10", "24", "eth0") == "192.168.1.10/24"

    def test_network_and_prefix(self) -> None:
        # Extrae el '26'
        assert (
            _build_interface_cidr("136.12.34.129", "136.12.34.128/26", "eth0")
            == "136.12.34.129/26"
        )

    def test_network_and_mask(self) -> None:
        # Extrae la máscara y la convierte a prefijo CIDR equivalente
        assert (
            _build_interface_cidr("192.168.1.5", "192.168.1.0/255.255.255.0", "eth0")
            == "192.168.1.5/24"
        )

    def test_invalid_prefix_raises_error(self) -> None:
        with pytest.raises(FieldParseError, match="Prefijo o CIDR inválido"):
            _build_interface_cidr("192.168.1.5", "33", "eth0")  # /33 no existe en IPv4

    def test_mask_only(self) -> None:
        assert (
            _build_interface_cidr("192.168.1.5", "255.255.255.0", "eth0")
            == "192.168.1.5/24"
        )

    def test_network_with_host_bits_is_tolerated(self) -> None:
        """'192.168.1.5/24' como Red IP se interpreta como la red 192.168.1.0/24."""
        assert (
            _build_interface_cidr("192.168.1.10", "192.168.1.5/24", "eth0")
            == "192.168.1.10/24"
        )

    def test_ip_outside_declared_network_raises_error(self) -> None:
        with pytest.raises(FieldParseError, match="no pertenece a la 'Red IP'"):
            _build_interface_cidr("192.168.1.10", "10.0.0.0/8", "eth0")

    def test_ipv6_in_ipv4_network_raises_error(self) -> None:
        with pytest.raises(FieldParseError, match="no pertenece a la 'Red IP'"):
            _build_interface_cidr("2001:db8::1", "10.0.0.0/8", "eth0")

    @pytest.mark.parametrize("pfx", ["0.0.0.0/0", "0", "0.0.0.0"])
    def test_zero_prefix_raises_error(self, pfx: str) -> None:
        with pytest.raises(FieldParseError, match="/0 no permitido"):
            _build_interface_cidr("192.168.1.5", pfx, "eth0")


class TestNetworkFromCidr:
    @pytest.mark.parametrize(
        ("cidr", "expected"),
        [
            ("192.168.1.10/24", "192.168.1.0/24"),
            ("136.12.34.129/26", "136.12.34.128/26"),
            ("2001:db8::1/64", "2001:db8::/64"),
        ],
    )
    def test_network_from_cidr(self, cidr: str, expected: str) -> None:
        assert _network_from_cidr(cidr) == expected


class TestEnsurePrefix:
    """Verifica el get-or-create idempotente del Prefix en NetBox."""

    def test_existing_prefix_is_not_recreated(self) -> None:
        endpoint = MagicMock()
        existing = MagicMock()
        endpoint.filter.return_value = [existing]
        obj, changed = ensure_prefix(endpoint, "192.168.1.0/24", dry_run=False)
        assert obj is existing
        assert changed is False
        endpoint.filter.assert_called_once_with(prefix="192.168.1.0/24")
        endpoint.create.assert_not_called()

    def test_missing_prefix_is_created(self) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = []
        _, changed = ensure_prefix(endpoint, "192.168.1.0/24", dry_run=False)
        assert changed is True
        endpoint.create.assert_called_once_with(
            prefix="192.168.1.0/24", status="active"
        )

    def test_dry_run_does_not_create(self) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = []
        _, changed = ensure_prefix(endpoint, "192.168.1.0/24", dry_run=True)
        assert changed is True
        endpoint.create.assert_not_called()

    def test_api_error_on_create_is_wrapped(self) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = []
        endpoint.create.side_effect = RequestError(MagicMock(status_code=400))
        with pytest.raises(NetBoxApiError):
            ensure_prefix(endpoint, "192.168.1.0/24", dry_run=False)


class TestCreateRecord:
    """Verifica la primitiva de creación compartida (real y dry-run)."""

    def test_creates_in_netbox(self) -> None:
        endpoint = MagicMock()
        obj = _create_record(endpoint, {"name": "eth0"}, "eth0", dry_run=False)
        endpoint.create.assert_called_once_with(name="eth0")
        assert obj is endpoint.create.return_value

    def test_dry_run_returns_mock_without_creating(self) -> None:
        endpoint = MagicMock()
        obj = _create_record(endpoint, {"name": "eth0"}, "eth0", dry_run=True)
        endpoint.create.assert_not_called()
        assert isinstance(obj, MockNetBoxRecord)
        assert obj.id == 0
        assert obj.name == "eth0"

    def test_api_error_is_wrapped(self) -> None:
        endpoint = MagicMock()
        endpoint.create.side_effect = RequestError(MagicMock(status_code=400))
        with pytest.raises(NetBoxApiError):
            _create_record(endpoint, {"name": "eth0"}, "eth0", dry_run=False)


class TestApplyUpdate:
    """Verifica la primitiva de escritura para diffs precalculados."""

    def test_propagates_false_when_netbox_applies_nothing(self) -> None:
        existing = MagicMock()
        existing.update.return_value = False
        assert apply_update(existing, {"mtu": 9000}, "eth0", dry_run=False) is False
        existing.update.assert_called_once_with({"mtu": 9000})

    def test_dry_run_does_not_call_api(self) -> None:
        existing = MagicMock()
        assert apply_update(existing, {"mtu": 9000}, "eth0", dry_run=True) is True
        existing.update.assert_not_called()


class TestUpdateIfChanged:
    """Verifica la primitiva de actualización basada en diff."""

    def test_no_diff_does_not_update(self) -> None:
        existing = MagicMock(enabled=True)
        obj, changed = update_if_changed(existing, {"enabled": True}, "eth0", False)
        assert obj is existing
        assert changed is False
        existing.update.assert_not_called()

    def test_diff_updates_only_changed_fields(self) -> None:
        existing = MagicMock(enabled=True, mtu=1500)
        _, changed = update_if_changed(
            existing, {"enabled": False, "mtu": 1500}, "eth0", False
        )
        assert changed is True
        existing.update.assert_called_once_with({"enabled": False})

    def test_netbox_applying_nothing_reports_no_change(self) -> None:
        existing = MagicMock(enabled=True)
        existing.update.return_value = False
        _, changed = update_if_changed(existing, {"enabled": False}, "eth0", False)
        assert changed is False

    def test_dry_run_reports_change_without_update(self) -> None:
        existing = MagicMock(enabled=True)
        _, changed = update_if_changed(existing, {"enabled": False}, "eth0", True)
        assert changed is True
        existing.update.assert_not_called()

    def test_api_error_is_wrapped(self) -> None:
        existing = MagicMock(enabled=True)
        existing.update.side_effect = RequestError(MagicMock(status_code=400))
        with pytest.raises(NetBoxApiError):
            update_if_changed(existing, {"enabled": False}, "eth0", False)


class TestUpsertInterfaceRecord:
    """Verifica que el upsert de interfaces componga creación y actualización."""

    def test_creates_when_not_cached(self) -> None:
        endpoint = MagicMock()
        payload = {"name": "eth0", "enabled": True}
        obj, changed = _upsert_interface_record(
            "eth0", payload, None, endpoint, 7, False
        )
        endpoint.create.assert_called_once_with(**payload)
        assert obj is endpoint.create.return_value
        assert changed is True

    def test_dry_run_create_does_not_call_api(self) -> None:
        endpoint = MagicMock()
        payload = {"name": "eth0", "enabled": True}
        obj, changed = _upsert_interface_record(
            "eth0", payload, None, endpoint, 7, True
        )
        endpoint.create.assert_not_called()
        assert isinstance(obj, MockNetBoxRecord)
        assert changed is True

    def test_existing_unchanged_is_noop(self) -> None:
        existing = MagicMock(enabled=True)
        existing.name = "eth0"
        _, changed = _upsert_interface_record(
            "eth0", {"name": "eth0", "enabled": True}, existing, MagicMock(), 7, False
        )
        assert changed is False
        existing.update.assert_not_called()


class TestSanitizeMacAddress:
    """Verifica el saneamiento y validación de direcciones MAC."""

    def test_valid_macs(self) -> None:
        # Formato canónico
        assert _sanitize_mac_address("AA:BB:CC:DD:EE:FF", "eth0") == "AA:BB:CC:DD:EE:FF"
        # Formato Windows (guiones)
        assert _sanitize_mac_address("aa-bb-cc-dd-ee-ff", "eth0") == "AA:BB:CC:DD:EE:FF"
        # Formato Cisco (puntos)
        assert _sanitize_mac_address("aabb.ccdd.eeff", "eth0") == "AA:BB:CC:DD:EE:FF"
        # Sin separadores
        assert _sanitize_mac_address("AABBCCDDEEFF", "eth0") == "AA:BB:CC:DD:EE:FF"

    def test_invalid_macs(self) -> None:
        # Basura (no hexa)
        with pytest.raises(FieldParseError, match="no es válida"):
            _sanitize_mac_address("N/A", "eth0")

        # Demasiado corta
        with pytest.raises(FieldParseError, match="no es válida"):
            _sanitize_mac_address("AA:BB:CC:DD:EE", "eth0")

        # Demasiado larga
        with pytest.raises(FieldParseError, match="no es válida"):
            _sanitize_mac_address("AA:BB:CC:DD:EE:FF:11", "eth0")


class TestParseSingleNetworkInterface:
    """Verifica el parseo de una única interfaz."""

    def test_complete_interface(self, config: NetBoxMappingConfig) -> None:
        status_map = {"up": True, "down": False}
        parsed = _parse_single_network_interface(
            name="eth0",
            status_raw="up",
            ip_raw="10.0.0.1",
            pfx_raw="24",
            mac_raw="AA:BB:CC:DD:EE:FF",
            status_map=status_map,
            config=config,
        )
        assert parsed["name"] == "eth0"
        assert parsed["enabled"] is True
        assert parsed["ip"] == "10.0.0.1"
        assert parsed["prefix"] == "24"
        assert parsed["cidr"] == "10.0.0.1/24"
        assert parsed["mac"] == "AA:BB:CC:DD:EE:FF"

    def test_interface_without_ip(self, config: NetBoxMappingConfig) -> None:
        status_map = {"up": True}
        parsed = _parse_single_network_interface(
            name="eth1",
            status_raw="up",
            ip_raw="",
            pfx_raw="",
            mac_raw="",
            status_map=status_map,
            config=config,
        )
        assert parsed["ip"] is None
        assert parsed["prefix"] is None
        assert parsed["cidr"] is None

    def test_empty_name_raises_error(self, config: NetBoxMappingConfig) -> None:
        with pytest.raises(
            RowValidationError,
            match="El nombre de una interfaz de red no puede estar vacío",
        ):
            _parse_single_network_interface("", "up", "1.1.1.1", "24", "", {}, config)

    def test_prefix_without_ip_is_discarded_with_warning(
        self, config: NetBoxMappingConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """'Red IP' sin 'IP': no hay CIDR y se advierte del descarte."""
        with caplog.at_level(logging.WARNING):
            parsed = _parse_single_network_interface(
                "eth0", "up", "", "192.168.1.0/24", "", {}, config
            )
        assert parsed["cidr"] is None
        assert parsed["ip"] is None
        assert "sin 'IP'" in caplog.text

    def test_prefix_without_ip_is_not_validated(
        self, config: NetBoxMappingConfig
    ) -> None:
        """Una 'Red IP' basura sin 'IP' no debe romper la fila."""
        parsed = _parse_single_network_interface(
            "eth0", "up", "N/A", "basura", "", {}, config
        )
        assert parsed["cidr"] is None

    def test_ip_without_prefix_warns_and_skips_cidr(
        self, config: NetBoxMappingConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """IP pura sin 'Red IP': no se inventa prefijo y se advierte."""
        with caplog.at_level(logging.WARNING):
            parsed = _parse_single_network_interface(
                "eth0", "up", "10.0.0.5", "N/A", "", {}, config
            )
        assert parsed["ip"] == "10.0.0.5"
        assert parsed["cidr"] is None
        assert "sin 'Red IP' ni prefijo" in caplog.text

    def test_ip_with_embedded_prefix_builds_cidr(
        self, config: NetBoxMappingConfig
    ) -> None:
        """IP en notación CIDR y sin 'Red IP': el prefijo embebido se usa."""
        parsed = _parse_single_network_interface(
            "eth0", "up", "10.0.0.5/24", "", "", {}, config
        )
        assert parsed["ip"] == "10.0.0.5"
        assert parsed["cidr"] == "10.0.0.5/24"

    def test_red_ip_has_priority_over_embedded_prefix(
        self, config: NetBoxMappingConfig
    ) -> None:
        parsed = _parse_single_network_interface(
            "eth0", "up", "10.0.0.5/24", "16", "", {}, config
        )
        assert parsed["cidr"] == "10.0.0.5/16"

    def test_ip_with_empty_embedded_prefix_warns(
        self, config: NetBoxMappingConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """'10.0.0.5/' no aporta prefijo: se trata como IP sin prefijo."""
        with caplog.at_level(logging.WARNING):
            parsed = _parse_single_network_interface(
                "eth0", "up", "10.0.0.5/", "", "", {}, config
            )
        assert parsed["cidr"] is None
        assert "sin 'Red IP' ni prefijo" in caplog.text

    def test_ip_with_invalid_embedded_prefix_raises(
        self, config: NetBoxMappingConfig
    ) -> None:
        with pytest.raises(RowValidationError, match="inválido"):
            _parse_single_network_interface(
                "eth0", "up", "10.0.0.5/99", "", "", {}, config
            )

    def test_ip_outside_red_ip_raises_row_validation_error(
        self, config: NetBoxMappingConfig
    ) -> None:
        with pytest.raises(RowValidationError, match="no pertenece a la 'Red IP'"):
            _parse_single_network_interface(
                "eth0", "up", "192.168.1.10", "10.0.0.0/8", "", {}, config
            )

    def test_network_derived_from_full_red_ip(
        self, config: NetBoxMappingConfig
    ) -> None:
        parsed = _parse_single_network_interface(
            "eth0", "up", "192.168.1.10", "192.168.1.0/24", "", {}, config
        )
        assert parsed["cidr"] == "192.168.1.10/24"
        assert parsed["network"] == "192.168.1.0/24"

    def test_network_derived_from_prefix_only(
        self, config: NetBoxMappingConfig
    ) -> None:
        parsed = _parse_single_network_interface(
            "eth0", "up", "192.168.1.10", "24", "", {}, config
        )
        assert parsed["network"] == "192.168.1.0/24"

    def test_network_is_none_without_cidr(self, config: NetBoxMappingConfig) -> None:
        parsed = _parse_single_network_interface("eth0", "up", "", "", "", {}, config)
        assert parsed["network"] is None


class TestSplitIpAndEmbeddedPrefix:
    """Verifica la separación de IP y prefijo embebido."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("10.0.0.5", ("10.0.0.5", None)),
            ("10.0.0.5/24", ("10.0.0.5", "24")),
            (" 10.0.0.5 / 24 ", ("10.0.0.5", "24")),
            ("10.0.0.5/", ("10.0.0.5", None)),
            ("2001:db8::1/64", ("2001:db8::1", "64")),
        ],
    )
    def test_split(self, raw: str, expected: tuple[str, str | None]) -> None:
        assert _split_ip_and_embedded_prefix(raw) == expected


class TestParseNetworkInterfaces:
    """Verifica el procesamiento de columnas CSV enteras de red."""

    def test_multiple_interfaces(self, config: NetBoxMappingConfig) -> None:
        row = {
            config.csv_columns["iface_names"].source: "eth0, eth1",
            config.csv_columns["iface_status"].source: "up, down",
            config.csv_columns["iface_ip"].source: "192.168.1.1, 10.0.0.1",
            config.csv_columns["iface_pfx"].source: "24, 8",
            config.csv_columns[
                "iface_mac"
            ].source: "AA:AA:AA:AA:AA:AA, BB:BB:BB:BB:BB:BB",
        }
        interfaces = _parse_network_interfaces(row, config)
        assert len(interfaces) == 2
        assert interfaces[0]["name"] == "eth0"
        assert interfaces[0]["cidr"] == "192.168.1.1/24"
        assert interfaces[1]["name"] == "eth1"
        assert interfaces[1]["cidr"] == "10.0.0.1/8"
        assert interfaces[1]["enabled"] is False

    def test_no_interfaces(self, config: NetBoxMappingConfig) -> None:
        row = {config.csv_columns["iface_names"].source: ""}
        assert _parse_network_interfaces(row, config) == []

    def test_inconsistent_lengths_raises_error(
        self, config: NetBoxMappingConfig
    ) -> None:
        row = {
            config.csv_columns["iface_names"].source: "eth0, eth1",
            config.csv_columns["iface_ip"].source: "192.168.1.1",  # falta una IP
        }
        with pytest.raises(
            RowValidationError, match="Discrepancia de elementos en red"
        ):
            _parse_network_interfaces(row, config)

    def test_empty_columns_filled(self, config: NetBoxMappingConfig) -> None:
        row = {
            config.csv_columns["iface_names"].source: "eth0, eth1",
            config.csv_columns["iface_ip"].source: "",  # Completamente vacío
        }
        interfaces = _parse_network_interfaces(row, config)
        assert len(interfaces) == 2
        assert interfaces[0]["ip"] is None
        assert interfaces[1]["ip"] is None

    def test_partial_ips_with_na(self, config: NetBoxMappingConfig) -> None:
        row = {
            config.csv_columns["iface_names"].source: "eth0, eth1",
            config.csv_columns["iface_ip"].source: "192.168.1.1, N/A",
            config.csv_columns["iface_pfx"].source: "24, N/A",
        }
        interfaces = _parse_network_interfaces(row, config)
        assert len(interfaces) == 2
        assert interfaces[0]["cidr"] == "192.168.1.1/24"
        assert interfaces[1]["cidr"] is None

    def test_missing_status_key_filled_with_default(
        self, config: NetBoxMappingConfig
    ) -> None:
        # Fila donde la columna de status ni siquiera existe en el dict (ej. N/A en pandas/DictReader)
        row = {
            config.csv_columns["iface_names"].source: "eth0",
            config.csv_columns["iface_ip"].source: "1.1.1.1",
        }
        interfaces = _parse_network_interfaces(row, config)
        assert len(interfaces) == 1
        assert interfaces[0]["enabled"] is True  # Por default


class TestEnsureSite:
    """Verifica la lógica de ensure_site (búsqueda, creación y dry-run)."""

    def test_site_exists(self) -> None:
        endpoints = MagicMock()
        endpoints.sites.name = "sites"
        mock_site = MockNetBoxRecord(id=1, name="DC1", slug="dc1")
        endpoints.sites.filter.return_value = [mock_site]

        site_cfg = SiteConfig(name="DC1", slug="dc1")
        result = ensure_site(endpoints.sites, site_cfg, dry_run=False)

        assert result.id == 1
        endpoints.sites.filter.assert_called_once_with(name="DC1")
        endpoints.sites.create.assert_not_called()

    def test_site_created(self) -> None:
        endpoints = MagicMock()
        endpoints.sites.name = "sites"
        endpoints.sites.filter.return_value = []
        mock_site = MockNetBoxRecord(id=2, name="DC2", slug="dc2")
        endpoints.sites.create.return_value = mock_site

        site_cfg = SiteConfig(name="DC2", slug="dc2")
        result = ensure_site(endpoints.sites, site_cfg, dry_run=False)

        assert result.id == 2
        endpoints.sites.create.assert_called_once_with(name="DC2", slug="dc2")

    def test_dry_run_create_mock_site(self) -> None:
        endpoints = MagicMock()
        endpoints.sites.name = "sites"
        endpoints.sites.filter.return_value = []

        site_cfg = SiteConfig(name="DC3", slug="dc3")
        result = ensure_site(endpoints.sites, site_cfg, dry_run=True)

        assert result.id == 0  # Mock
        assert result.name == "DC3"
        endpoints.sites.create.assert_not_called()


class TestEnsureManufacturer:
    """Verifica la lógica de ensure_manufacturer con cache y fallback de slug."""

    def test_found_in_cache(self) -> None:
        endpoint = MagicMock()
        cache: NameCache = {"Dell": MockNetBoxRecord(id=5, name="Dell")}

        result, returned_cache = ensure_manufacturer(
            endpoint, "Dell", cache, dry_run=False
        )
        assert result.id == 5
        assert returned_cache is cache
        assert returned_cache["Dell"].id == 5
        endpoint.filter.assert_not_called()

    def test_found_by_name(self) -> None:
        endpoint = MagicMock()
        mock_mfg = MockNetBoxRecord(id=6, name="HP")
        endpoint.filter.return_value = [mock_mfg]
        cache: NameCache = {}

        result, returned_cache = ensure_manufacturer(
            endpoint, "HP", cache, dry_run=False
        )
        assert result.id == 6
        assert returned_cache is cache
        assert returned_cache["HP"].id == 6
        endpoint.filter.assert_called_once_with(name="HP")

    def test_found_by_slug_fallback(self) -> None:
        endpoint = MagicMock()
        # filter(name="H.P.") retorna vacío, pero filter(slug="hp") retorna un registro
        mock_mfg = MockNetBoxRecord(id=7, name="HP")
        endpoint.filter.side_effect = [[], [mock_mfg]]
        cache: NameCache = {}

        result, returned_cache = ensure_manufacturer(
            endpoint, "H.P.", cache, dry_run=False
        )
        assert result.id == 7
        assert returned_cache is cache
        assert returned_cache["H.P."].id == 7
        assert endpoint.filter.call_count == 2

    def test_created_when_not_found(self) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = []
        endpoint.name = "racks"
        mock_created = MockNetBoxRecord(id=8, name="Lenovo")
        endpoint.create.return_value = mock_created
        cache: NameCache = {}

        result, returned_cache = ensure_manufacturer(
            endpoint, "Lenovo", cache, dry_run=False
        )
        assert result.id == 8
        assert returned_cache is cache
        assert returned_cache["Lenovo"].id == 8
        endpoint.create.assert_called_once_with(name="Lenovo", slug="lenovo")


class TestEnsureRack:
    """Verifica que ensure_rack capture RequestError al crear."""

    def test_ensure_rack_request_error(self) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = []
        endpoint.name = "racks"
        # Simulamos que la creación falla
        endpoint.create.side_effect = RequestError(
            MagicMock(status_code=400, reason="Bad Request")
        )

        site_mock = MockNetBoxRecord(id=1, name="Site1")
        cache: SiteNameCache = {}

        with pytest.raises(
            NetBoxApiError,
            match="No se pudo crear el objeto en 'racks' con nombre 'Rack1'",
        ):
            ensure_rack(endpoint, "Rack1", site_mock, cache, dry_run=False)

    @patch("export_to_netbox._sync_rack_location")
    @patch("export_to_netbox.get_or_create_cached")
    def test_ensure_rack_calls_sync_location(
        self, mock_get: MagicMock, mock_sync_loc: MagicMock
    ) -> None:
        endpoint = MagicMock()
        site_mock = MockNetBoxRecord(id=1, name="Site1")
        cache: SiteNameCache = {}

        mock_rack = MagicMock(id=5, name="Rack1")
        mock_get.return_value = (mock_rack, cache)

        _, _ = ensure_rack(
            endpoint, "Rack1", site_mock, cache, dry_run=False, location_id=99
        )

        mock_sync_loc.assert_called_once_with(mock_rack, "Rack1", 99, False)

    @patch("export_to_netbox._sync_rack_location")
    @patch("export_to_netbox.get_or_create_cached")
    def test_ensure_rack_without_location(
        self, mock_get: MagicMock, mock_sync_loc: MagicMock
    ) -> None:
        endpoint = MagicMock()
        site_mock = MockNetBoxRecord(id=1, name="Site1")
        cache: SiteNameCache = {}

        mock_rack = MagicMock(id=5, name="Rack1")
        mock_get.return_value = (mock_rack, cache)

        _, _ = ensure_rack(
            endpoint, "Rack1", site_mock, cache, dry_run=False, location_id=None
        )

        mock_get.assert_called_once_with(
            endpoint=endpoint,
            cache=cache,
            cache_key=(1, "Rack1"),
            filter_kwargs={"name": "Rack1", "site_id": 1},
            create_kwargs={"name": "Rack1", "site": 1},
            name="Rack1",
            dry_run=False,
            skip_filter=False,
        )
        mock_sync_loc.assert_not_called()


class TestSyncRackLocation:
    """Verifica la lógica de _sync_rack_location."""

    def test_sync_rack_location_different(self) -> None:
        mock_old_loc = MockNetBoxRecord(id=88, name="Fila B")
        mock_rack = MagicMock(id=5, name="Rack1", location=mock_old_loc)

        result = _sync_rack_location(mock_rack, "Rack1", 99, dry_run=False)

        assert result is True
        mock_rack.update.assert_called_once_with({"location": 99})

    def test_sync_rack_location_no_previous(self) -> None:
        mock_rack = MagicMock(id=5, name="Rack1", location=None)

        result = _sync_rack_location(mock_rack, "Rack1", 99, dry_run=False)

        assert result is True
        mock_rack.update.assert_called_once_with({"location": 99})

    def test_sync_rack_location_matches(self) -> None:
        mock_loc = MockNetBoxRecord(id=99, name="Fila A")
        mock_rack = MagicMock(id=5, name="Rack1", location=mock_loc)

        result = _sync_rack_location(mock_rack, "Rack1", 99, dry_run=False)

        assert result is False
        mock_rack.update.assert_not_called()

    def test_sync_rack_location_dry_run(self) -> None:
        mock_rack = MagicMock(id=5, name="Rack1", location=None)

        result = _sync_rack_location(mock_rack, "Rack1", 99, dry_run=True)

        assert result is True
        mock_rack.update.assert_not_called()

    def test_sync_rack_location_raises_error(self) -> None:
        mock_rack = MagicMock(id=5, name="Rack1", location=None)
        mock_rack.update.side_effect = RequestError(
            MagicMock(status_code=400, reason="Bad Request")
        )

        with pytest.raises(
            NetBoxApiError,
            match="Error actualizando rack 'Rack1'",
        ):
            _sync_rack_location(mock_rack, "Rack1", 99, dry_run=False)


class TestEnsureLocation:
    """Verifica que ensure_location cree o recupere localizaciones correctamente."""

    @patch("export_to_netbox.get_or_create_cached")
    def test_ensure_location_calls_get_or_create(self, mock_get: MagicMock) -> None:
        endpoint = MagicMock()
        site_mock = MockNetBoxRecord(id=10, name="Site1")
        cache: SiteNameCache = {}
        mock_get.return_value = (MockNetBoxRecord(id=5, name="Fila A"), cache)

        obj, _ = ensure_location(endpoint, "Fila A", site_mock, cache, False)

        mock_get.assert_called_once_with(
            endpoint=endpoint,
            cache=cache,
            cache_key=(10, "Fila A"),
            filter_kwargs={"name": "Fila A", "site_id": 10},
            create_kwargs={"name": "Fila A", "site": 10},
            name="Fila A",
            dry_run=False,
            skip_filter=False,
        )
        assert getattr(obj, "id", 0) == 5


class TestEnsureCluster:
    """Verifica que ensure_cluster capture RequestError al crear."""

    def test_ensure_cluster_request_error(self) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = []
        endpoint.name = "clusters"
        endpoint.create.side_effect = RequestError(
            MagicMock(status_code=400, reason="Bad Request")
        )

        cluster_type_mock = MockNetBoxRecord(id=2, name="Type1")
        cache: NameCache = {}

        with pytest.raises(
            NetBoxApiError,
            match="No se pudo crear el objeto en 'clusters' con nombre 'Cluster1'",
        ):
            ensure_cluster(
                endpoint, "Cluster1", cluster_type_mock, cache, dry_run=False
            )


class TestEnsureDynamicClusterTypesEdgeCases:
    """Verifica que la caché se propague correctamente y evite peticiones N+1."""

    def test_cache_efficiency_no_extra_api_calls(self) -> None:
        """
        Escenario destructivo/Edge case: Verifica que cuando el caché ya contiene
        objetos, no haya avalanchas O(N) hacia NetBox.
        """
        endpoints = MagicMock()
        mock_ct_endpoint = MagicMock()
        mock_ct_endpoint.name = "cluster_types"
        endpoints.cluster_types = mock_ct_endpoint

        # Simula que los objetos NO existen en NetBox (requieren create si no hay caché)
        mock_ct_endpoint.filter.return_value = []

        fallback_cfg = MagicMock()
        fallback_cfg.default.name = "FallbackCluster"
        fallback_cfg.default.slug = "fallback-cluster"

        cluster_type_map = {"vm1": "Ubuntu", "vm2": "Debian"}

        # Ya existe Ubuntu en el caché
        mock_ubuntu = MagicMock()
        cache: dict[str, Any] = {"Ubuntu": mock_ubuntu}

        _, updated_cache = ensure_dynamic_cluster_types(
            endpoints=endpoints,
            cluster_type_map=cluster_type_map,
            fallback_cfg=fallback_cfg,
            cache=cache,
            dry_run=False,
        )

        assert "FallbackCluster" in updated_cache
        assert "Debian" in updated_cache
        assert updated_cache["Ubuntu"] == mock_ubuntu

        # Fallback y Debian se crearon, Ubuntu usó caché
        assert mock_ct_endpoint.create.call_count == 2
        call_names = [
            call.kwargs.get("name") for call in mock_ct_endpoint.create.call_args_list
        ]
        assert "Ubuntu" not in call_names


class TestAssignPrimaryIPv4:
    def test_one_ip_assigns_primary(self) -> None:
        mock_obj = MagicMock()
        mock_obj.primary_ip4 = None
        res = _assign_primary_ipv4(mock_obj, 15, "srv", False)
        assert res is True
        mock_obj.update.assert_called_once_with({"primary_ip4": 15})

    def test_idempotent_if_already_assigned(self) -> None:
        mock_obj = MagicMock()
        mock_ip = MagicMock()
        mock_ip.id = 15
        mock_obj.primary_ip4 = mock_ip
        res = _assign_primary_ipv4(mock_obj, 15, "srv", False)
        assert res is False
        mock_obj.update.assert_not_called()

    def test_updates_if_assigned_to_different_ip(self) -> None:
        mock_obj = MagicMock()
        mock_ip = MagicMock()
        mock_ip.id = 10
        mock_obj.primary_ip4 = mock_ip
        res = _assign_primary_ipv4(mock_obj, 15, "srv", False)
        assert res is True
        mock_obj.update.assert_called_once_with({"primary_ip4": 15})


class TestResolveCluster:
    """Verifica los edge cases de _resolve_cluster."""

    @pytest.fixture
    def mock_endpoints(self) -> MagicMock:
        endpoints = MagicMock()
        endpoints.clusters = MagicMock()
        endpoints.clusters.name = "clusters"
        return endpoints

    @pytest.fixture
    def mock_config(self) -> MagicMock:
        config = MagicMock(spec=NetBoxMappingConfig)
        config.fields = {
            "cluster_name": MagicMock(
                source="Nombre Cluster",
                target="cluster_name",
                default=None,
                transform=None,
                is_unique=False,
                type="str",
            ),
        }
        config.is_empty.side_effect = lambda v: v in (None, "", "NaN", "NA")
        config.map_value_by_source.side_effect = lambda s, v, strict: v
        return config

    @patch("export_to_netbox.extract_csv_value")
    @patch("export_to_netbox.ensure_cluster")
    def test_cluster_name_present_os_empty(
        self,
        mock_ensure_cluster: MagicMock,
        mock_extract: MagicMock,
        mock_endpoints: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """Si cluster_name está presente pero SO está vacío, debe usar fallback_cluster_type."""
        row = {"Nombre Cluster": "Cluster 1", "SO hipervisor": ""}
        mock_extract.side_effect = lambda r, k, c: (
            "Cluster 1" if k == "cluster_name" else ""
        )

        cluster_type_map: dict[str, str] = {}
        fallback_cluster_type = MockNetBoxRecord(id=99, name="FallbackType")
        cluster_type_cache: NameCache = {}
        cluster_cache: NameCache = {}

        mock_cluster = MockNetBoxRecord(id=42, name="Cluster 1")
        mock_ensure_cluster.return_value = (mock_cluster, {"Cluster 1": mock_cluster})

        cluster_id, cache = _resolve_cluster(
            mock_endpoints.clusters,
            row,
            cluster_type_map,
            cluster_type_cache,
            fallback_cluster_type,
            cluster_cache,
            dry_run=False,
            config=mock_config,
        )

        assert cluster_id == 42
        assert "Cluster 1" in cache
        mock_ensure_cluster.assert_called_once_with(
            mock_endpoints.clusters,
            "Cluster 1",
            fallback_cluster_type,
            cluster_cache,
            False,
        )

    @patch("export_to_netbox.extract_csv_value")
    def test_cluster_name_empty_os_present(
        self, mock_extract: MagicMock, mock_endpoints: MagicMock, mock_config: MagicMock
    ) -> None:
        """Si cluster_name está vacío (incluso si SO está presente), aborta y retorna None."""
        row = {"Nombre Cluster": "", "SO hipervisor": "VMware"}
        mock_extract.side_effect = lambda r, k, c: (
            "" if k == "cluster_name" else "VMware"
        )

        cluster_type_map = {"": "VMware"}
        fallback_cluster_type = MockNetBoxRecord(id=99, name="FallbackType")
        cluster_type_cache: NameCache = {
            "VMware": MockNetBoxRecord(id=1, name="VMware")
        }
        cluster_cache: NameCache = {}

        cluster_id, cache = _resolve_cluster(
            mock_endpoints.clusters,
            row,
            cluster_type_map,
            cluster_type_cache,
            fallback_cluster_type,
            cluster_cache,
            dry_run=False,
            config=mock_config,
        )

        assert cluster_id is None
        assert cache == {}

    @patch("export_to_netbox.extract_csv_value")
    def test_both_empty(
        self, mock_extract: MagicMock, mock_endpoints: MagicMock, mock_config: MagicMock
    ) -> None:
        """Si ambos están vacíos, aborta y retorna None."""
        row = {"Nombre Cluster": "", "SO hipervisor": ""}
        mock_extract.side_effect = lambda r, k, c: ""

        cluster_type_map: dict[str, str] = {}
        fallback_cluster_type = MockNetBoxRecord(id=99, name="FallbackType")
        cluster_type_cache: NameCache = {}
        cluster_cache: NameCache = {}

        cluster_id, cache = _resolve_cluster(
            mock_endpoints.clusters,
            row,
            cluster_type_map,
            cluster_type_cache,
            fallback_cluster_type,
            cluster_cache,
            dry_run=False,
            config=mock_config,
        )

        assert cluster_id is None
        assert cache == {}


class TestSyncDeviceTypeAttributes:
    """Verifica la sincronización de atributos en DeviceTypes."""

    def test_sync_no_update_needed(self) -> None:
        existing = MagicMock()
        existing.u_height = 1.5
        existing.part_number = "PN1"
        result = _sync_device_type_attributes(
            existing, "Model A", DeviceTypeAttributes(1.5, "PN1"), False
        )
        assert result is False
        existing.update.assert_not_called()

    def test_sync_update_needed(self) -> None:
        existing = MagicMock()
        existing.u_height = 1.0
        existing.part_number = ""
        result = _sync_device_type_attributes(
            existing, "Model B", DeviceTypeAttributes(1.5, "PN2"), False
        )
        assert result is True
        existing.update.assert_called_once_with({"u_height": 1.5, "part_number": "PN2"})


class TestSelectDeviceTypeAttributes:
    """Qué atributos se sincronizan en el DeviceType a partir de la fila."""

    KEY = DeviceTypeKey("Dell", "R740")

    @staticmethod
    def _row(config: NetBoxMappingConfig, height: str, pn: str = "") -> CsvRow:
        return {
            config.csv_columns["height_u"].source: height,
            config.csv_columns["part_number"].source: pn,
        }

    def test_fractional_u_height(self, config: NetBoxMappingConfig) -> None:
        attrs = select_device_type_attributes(
            self._row(config, "1.5"), self.KEY, {}, config
        )
        assert attrs == DeviceTypeAttributes(1.5, None)

    def test_empty_u_height_is_not_informed(self, config: NetBoxMappingConfig) -> None:
        """Una altura vacía ya no se convierte en 1.0: se conserva la del tipo existente."""
        attrs = select_device_type_attributes(
            self._row(config, ""), self.KEY, {}, config
        )
        assert attrs == DeviceTypeAttributes(None, None)

    def test_consolidated_attributes_take_precedence_over_row(
        self, config: NetBoxMappingConfig
    ) -> None:
        consolidated = {self.KEY: DeviceTypeAttributes(2.0, "PN-A")}
        attrs = select_device_type_attributes(
            self._row(config, "1", "PN-B"), self.KEY, consolidated, config
        )
        assert attrs == DeviceTypeAttributes(2.0, "PN-A")

    def test_invalid_row_value_is_a_row_error_even_if_consolidated(
        self, config: NetBoxMappingConfig
    ) -> None:
        consolidated = {self.KEY: DeviceTypeAttributes(2.0, "PN-A")}
        with pytest.raises(RowValidationError, match="height_u"):
            select_device_type_attributes(
                self._row(config, "1.25"), self.KEY, consolidated, config
            )

    def test_resolve_device_type_forwards_attributes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            export_to_netbox,
            "ensure_manufacturer",
            lambda *args, **kwargs: (MockNetBoxRecord(id=1, name="Dell"), {}),
        )
        mock_ensure = MagicMock(return_value=(MockNetBoxRecord(id=5), {}))
        monkeypatch.setattr(export_to_netbox, "ensure_device_type", mock_ensure)

        device_type_id, _ = _resolve_device_type(
            MagicMock(), self.KEY, DeviceTypeAttributes(2.0, None), CacheStore(), False
        )

        assert device_type_id == 5
        assert mock_ensure.call_args.args[2:4] == (
            "R740",
            DeviceTypeAttributes(2.0, None),
        )


class TestParseDeviceTypeAttributes:
    """Validación por fila de 'height_u' y 'part_number'."""

    @staticmethod
    def _row(config: NetBoxMappingConfig, height: str, pn: str = "") -> CsvRow:
        return {
            config.csv_columns["height_u"].source: height,
            config.csv_columns["part_number"].source: pn,
        }

    @pytest.mark.parametrize(
        ("height", "expected"),
        [("1", 1.0), ("1,5", 1.5), ("0", 0.0), ("0.5", 0.5), ("", None), ("N/A", None)],
    )
    def test_heights(
        self, config: NetBoxMappingConfig, height: str, expected: float | None
    ) -> None:
        """El 0 es un valor explícito (blades 0U); el vacío es 'no informado'."""
        attrs = parse_device_type_attributes(self._row(config, height), config)
        assert attrs.u_height == expected

    @pytest.mark.parametrize("height", ["1.25", "-1", "abc", "nan", "inf"])
    def test_invalid_heights(self, config: NetBoxMappingConfig, height: str) -> None:
        with pytest.raises(RowValidationError, match="height_u"):
            parse_device_type_attributes(self._row(config, height), config)

    def test_part_number_limits(self, config: NetBoxMappingConfig) -> None:
        ok = parse_device_type_attributes(self._row(config, "1", "x" * 50), config)
        assert ok.part_number == "x" * 50
        assert parse_device_type_attributes(self._row(config, "1"), config) == (
            DeviceTypeAttributes(1.0, None)
        )
        with pytest.raises(RowValidationError, match="50 caracteres"):
            parse_device_type_attributes(self._row(config, "1", "x" * 51), config)


class TestCompositeKeys:
    """Las claves con nombre siguen siendo tuplas: compatibles como clave de diccionario."""

    def test_device_type_key_is_tuple_compatible_and_has_label(self) -> None:
        key = DeviceTypeKey("Dell", "R640")
        assert key == ("Dell", "R640")
        assert {("Dell", "R640"): 1}[key] == 1
        assert key.label == "Dell / R640"

    def test_site_scoped_name_is_tuple_compatible(self) -> None:
        cache: dict[tuple[int, str], str] = {SiteScopedName(1, "Rack-A"): "ok"}
        assert cache[(1, "Rack-A")] == "ok"


class TestPrecomputeDeviceTypeAttributes:
    """Consolidación por modelo: primer valor no vacío, por atributo, con aviso de conflictos."""

    @staticmethod
    def _row(config: NetBoxMappingConfig, model: str, height: str, pn: str) -> CsvRow:
        src = {k: v.source for k, v in config.csv_columns.items()}
        return {
            src["manufacturer"]: "Dell",
            src["model"]: model,
            src["height_u"]: height,
            src["part_number"]: pn,
        }

    def test_first_non_empty_value_wins_per_attribute(
        self, config: NetBoxMappingConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        rows = [
            NumberedRow(2, self._row(config, "R640", "1", "")),
            NumberedRow(3, self._row(config, "R640", "", "PN-A")),
            NumberedRow(4, self._row(config, "R640", "2", "PN-B")),
        ]
        with caplog.at_level(logging.WARNING):
            result = precompute_device_type_attributes(rows, config)

        assert result == {
            DeviceTypeKey("Dell", "R640"): DeviceTypeAttributes(1.0, "PN-A")
        }
        assert "'Altura (U)' = 1.0 (línea 2) vs 2.0 (línea 4)" in caplog.text
        assert "'P/N' = 'PN-A' (línea 3) vs 'PN-B' (línea 4)" in caplog.text

    def test_models_are_independent_and_zero_is_a_value(
        self, config: NetBoxMappingConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        rows = [
            NumberedRow(2, self._row(config, "M1000e", "10", "")),
            NumberedRow(3, self._row(config, "M630", "0", "")),
            NumberedRow(4, self._row(config, "M630", "0", "")),
        ]
        with caplog.at_level(logging.WARNING):
            result = precompute_device_type_attributes(rows, config)

        assert result[DeviceTypeKey("Dell", "M1000e")] == DeviceTypeAttributes(
            10.0, None
        )
        assert result[DeviceTypeKey("Dell", "M630")] == DeviceTypeAttributes(0.0, None)
        assert "Conflicto" not in caplog.text

    def test_invalid_and_incomplete_rows_are_ignored_without_raising(
        self, config: NetBoxMappingConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """El pre-escaneo global nunca aborta: esas filas fallan al sincronizarse."""
        rows = [
            NumberedRow(2, self._row(config, "R640", "1.25", "PN-X")),
            NumberedRow(3, self._row(config, "", "1", "PN-Y")),
            NumberedRow(4, self._row(config, "R640", "2", "PN-OK")),
        ]
        with caplog.at_level(logging.DEBUG):
            result = precompute_device_type_attributes(rows, config)

        assert result == {
            DeviceTypeKey("Dell", "R640"): DeviceTypeAttributes(2.0, "PN-OK")
        }
        # La fila inválida no aporta valores, pero deja traza (solo visible con --verbose).
        assert "Línea 2 ignorada en la consolidación del DeviceType 'Dell / R640'" in (
            caplog.text
        )
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


class _FakeDeviceType:
    """DeviceType simulado: update() aplica los cambios in-place, como pynetbox."""

    def __init__(self, u_height: float, part_number: str) -> None:
        self.id = 5
        self.name = "R640"
        self.u_height = u_height
        self.part_number = part_number
        self.patches: list[dict[str, Any]] = []

    def update(self, data: dict[str, Any]) -> bool:
        self.patches.append(dict(data))
        for key, value in data.items():
            setattr(self, key, value)
        return True


class TestEnsureDeviceTypeIdempotency:
    """El DeviceType compartido se escribe una vez por modelo y nunca oscila."""

    MANUFACTURER = MockNetBoxRecord(id=1, name="Dell")

    @staticmethod
    def _endpoint(device_type: Any) -> MagicMock:
        endpoint = MagicMock()
        endpoint.name = "device-types"
        endpoint.filter.return_value = [device_type]
        return endpoint

    def _run(self, dt: _FakeDeviceType, attrs: DeviceTypeAttributes, rows: int) -> None:
        """Simula una ejecución: `rows` filas del mismo modelo con caché compartida."""
        cache: ManufModelCache = {}
        endpoint = self._endpoint(dt)
        for _ in range(rows):
            _, cache = ensure_device_type(
                endpoint, self.MANUFACTURER, "R640", attrs, cache, dry_run=False
            )

    def test_single_write_per_run_and_none_on_rerun(self) -> None:
        dt = _FakeDeviceType(2.0, "PN-MANUAL")
        attrs = DeviceTypeAttributes(1.0, "PN-A")

        self._run(dt, attrs, rows=3)
        assert dt.patches == [{"u_height": 1.0, "part_number": "PN-A"}]

        dt.patches.clear()
        self._run(dt, attrs, rows=3)
        assert dt.patches == []

    def test_empty_values_never_overwrite_existing(self) -> None:
        """P/N cargado a mano y tipo hijo 0U se conservan si el CSV no los informa."""
        dt = _FakeDeviceType(0.0, "PN-MANUAL")
        self._run(dt, DeviceTypeAttributes(None, None), rows=2)
        assert dt.patches == []
        assert (dt.u_height, dt.part_number) == (0.0, "PN-MANUAL")

    def test_explicit_zero_height_is_applied(self) -> None:
        dt = _FakeDeviceType(1.0, "")
        self._run(dt, DeviceTypeAttributes(0.0, None), rows=1)
        assert dt.patches == [{"u_height": 0.0}]

    def test_rejected_update_is_a_warning_not_an_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        dt = MagicMock(id=5, u_height=1.0, part_number="")
        dt.update.side_effect = RequestError(MagicMock(status_code=400))
        endpoint = self._endpoint(dt)

        with caplog.at_level(logging.WARNING):
            obj, _ = ensure_device_type(
                endpoint,
                self.MANUFACTURER,
                "R640",
                DeviceTypeAttributes(0.0, None),
                {},
                dry_run=False,
            )

        assert obj is dt
        assert "rechazó actualizar el DeviceType 'Dell / R640'" in caplog.text

    def test_new_type_with_empty_height_uses_default_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        endpoint = MagicMock()
        endpoint.name = "device-types"
        endpoint.filter.return_value = []
        endpoint.create.return_value = MagicMock(id=7, u_height=1.0, part_number="")

        with caplog.at_level(logging.WARNING):
            ensure_device_type(
                endpoint,
                self.MANUFACTURER,
                "R750",
                DeviceTypeAttributes(None, None),
                {},
                dry_run=False,
            )

        create_kwargs = endpoint.create.call_args.kwargs
        assert create_kwargs["u_height"] == DEFAULT_DEVICE_TYPE_U_HEIGHT
        assert create_kwargs["part_number"] == ""
        assert "Altura (U) vacía para el modelo 'Dell / R750'" in caplog.text

    def test_dry_run_reports_without_writing(self) -> None:
        dt = _FakeDeviceType(2.0, "PN-MANUAL")
        ensure_device_type(
            self._endpoint(dt),
            self.MANUFACTURER,
            "R640",
            DeviceTypeAttributes(1.0, "PN-A"),
            {},
            dry_run=True,
        )
        assert dt.patches == []


class TestPruneNetworkOrphans:
    """Verifica la poda de interfaces, IPs y MACs huérfanas."""

    def test_pruning_removes_orphans(self) -> None:
        mock_iface1 = MagicMock(id=101)
        mock_iface1.name = "eth0"
        mock_iface2 = MagicMock(id=102)
        mock_iface2.name = "eth1"

        ifaces_cache: dict[str, Any] = {"eth0": mock_iface1, "eth1": mock_iface2}
        csv_iface_names = {"eth0"}  # eth1 becomes orphan
        csv_ips = {"10.0.0.1/24"}
        csv_macs = {"AA:BB:CC"}

        endpoints = MagicMock()
        mock_ip_orphan = MagicMock(address="192.168.1.5/24", role=None)
        mock_ip_valid = MagicMock(address="10.0.0.1/24", role=None)

        # Simula que eth0 tiene una IP válida y una IP huérfana
        def filter_ips(**kwargs):
            if kwargs.get("assigned_object_id") == 101:
                return [mock_ip_valid, mock_ip_orphan]
            return []

        endpoints.ip_addresses.filter.side_effect = filter_ips

        mock_mac_orphan = MagicMock(mac_address="FF:EE:DD")

        def filter_macs(**kwargs):
            if kwargs.get("assigned_object_id") == 101:
                return [mock_mac_orphan]
            return []

        endpoints.mac_addresses.filter.side_effect = filter_macs

        metrics = _prune_network_orphans(
            endpoints=endpoints,
            node_type=NodeType.DEVICE,
            ifaces_cache=ifaces_cache,
            csv_iface_names=csv_iface_names,
            csv_ips=csv_ips,
            csv_macs=csv_macs,
            dry_run=False,
        )

        assert metrics.pruned_interfaces == 1
        assert metrics.pruned_ips == 1
        assert metrics.pruned_macs == 1
        assert metrics.errors == 0

        # Interfaz 1 no se borra, la 2 sí
        mock_iface1.delete.assert_not_called()
        mock_iface2.delete.assert_called_once()

        # IP huérfana se desvincula, válida no
        mock_ip_orphan.update.assert_called_once_with(
            {"assigned_object_type": None, "assigned_object_id": None}
        )
        mock_ip_valid.update.assert_not_called()

        # MAC huérfana se borra
        mock_mac_orphan.delete.assert_called_once()

    def test_pruning_dry_run_skips_actions(self) -> None:
        mock_iface = MagicMock(id=101)
        mock_iface.name = "eth1"
        ifaces_cache: dict[str, Any] = {"eth1": mock_iface}
        csv_iface_names = set()

        endpoints = MagicMock()
        mock_ip = MagicMock(address="192.168.1.5/24", role=None)
        endpoints.ip_addresses.filter.return_value = [mock_ip]
        mock_mac = MagicMock(mac_address="FF:EE:DD")
        endpoints.mac_addresses.filter.return_value = [mock_mac]

        metrics = _prune_network_orphans(
            endpoints=endpoints,
            node_type=NodeType.DEVICE,
            ifaces_cache=ifaces_cache,
            csv_iface_names=csv_iface_names,
            csv_ips=set(),
            csv_macs=set(),
            dry_run=True,
        )

        assert metrics.pruned_interfaces == 1
        assert metrics.pruned_ips == 0  # No se evalúan IPs/MACs si la interfaz se poda
        assert metrics.pruned_macs == 0
        assert metrics.errors == 0

        mock_iface.delete.assert_not_called()
        mock_ip.update.assert_not_called()
        mock_mac.delete.assert_not_called()


class TestSyncSkips:
    def test_sync_device_skips_missing_manufacturer_or_model(self) -> None:
        row = {"machine_name": "srv1", "machine_type": "server"}

        mock_config = MagicMock()
        mock_config.is_empty.return_value = False
        mock_config.csv_columns = {
            "rack_location": MagicMock(source="rack_location"),
            "manufacturer": MagicMock(source="manufacturer"),
            "model": MagicMock(source="model"),
            "machine_name": MagicMock(source="machine_name"),
            "machine_type": MagicMock(source="machine_type"),
        }

        # Fallará porque falta manufacturer o model
        with pytest.raises(RowSkipCondition, match="Falta 'manufacturer' o 'model'"):
            sync_device(
                endpoints=MagicMock(),
                row=row,
                config=mock_config,
                site=MagicMock(),
                cluster_type_map={},
                device_type_attrs={},
                fallback_cluster_type=MagicMock(),
                caches=MagicMock(),
                csv_counts=_counts(),
                dry_run=False,
            )

    def test_sync_vm_skips_missing_cluster(self) -> None:
        row = {"machine_name": "vm1"}

        mock_config = MagicMock()
        mock_config.is_empty.return_value = False
        mock_config.csv_columns = {
            "rack_location": MagicMock(source="rack_location"),
            "cluster_name": MagicMock(source="cluster_name"),
            "machine_name": MagicMock(source="machine_name"),
            "inventory_uuid": MagicMock(source="inventory_uuid"),
        }

        # Fallará porque falta cluster_name
        with pytest.raises(RowSkipCondition, match="Falta 'cluster_name'"):
            sync_vm(
                endpoints=MagicMock(),
                row=row,
                config=mock_config,
                site=MagicMock(),
                cluster_type_map={},
                fallback_cluster_type=MagicMock(),
                caches=MagicMock(),
                csv_counts=_counts(),
                dry_run=False,
            )


def _keep_payload(*args: Any) -> tuple[Any, Any]:
    """Doble de `_resolve_common_dependencies`: devuelve el payload y la caché sin tocar."""
    return args[2], args[3]


class TestSyncDevice:
    @patch("export_to_netbox._prepare_base_node")
    @patch("export_to_netbox._resolve_cluster")
    @patch("export_to_netbox.ensure_rack")
    @patch("export_to_netbox._resolve_device_type")
    @patch("export_to_netbox._execute_sync")
    @patch("export_to_netbox._resolve_existing_object", return_value=[])
    @patch("export_to_netbox._resolve_common_dependencies", side_effect=_keep_payload)
    @patch(
        "export_to_netbox.select_device_type_attributes",
        return_value=DeviceTypeAttributes(None, None),
    )
    def test_sync_device_assigns_front_face(
        self,
        mock_select_attrs: MagicMock,
        mock_common_deps: MagicMock,
        mock_resolve_existing: MagicMock,
        mock_execute_sync: MagicMock,
        mock_device_type: MagicMock,
        mock_ensure_rack: MagicMock,
        mock_cluster: MagicMock,
        mock_base_node: MagicMock,
    ) -> None:
        row = {"manufacturer": "Dell", "model": "R740", "machine_name": "srv1"}

        mock_config = MagicMock()
        mock_config.is_empty.return_value = False
        mock_config.is_cluster_host.return_value = False
        mock_config.csv_columns = {
            "rack_location": MagicMock(source="rack_location"),
            "manufacturer": MagicMock(source="manufacturer"),
            "model": MagicMock(source="model"),
            "rack": MagicMock(source="rack"),
            "machine_type": MagicMock(source="machine_type"),
        }

        mock_caches = MagicMock()
        mock_base_node.return_value = {
            "machine_name": "srv1",
            "inventory_uuid": "1234",
            "machine_type": "server",
            "payload": {"position": 10},  # NetBox API exige face si hay position
        }
        mock_device_type.return_value = (1, mock_caches)
        mock_execute_sync.return_value = SyncResult(SyncStatus.CREATED, 1, MagicMock())

        _, returned_caches = sync_device(
            endpoints=MagicMock(),
            row=row,
            config=mock_config,
            site=MagicMock(),
            cluster_type_map={},
            device_type_attrs={},
            fallback_cluster_type=MagicMock(),
            caches=mock_caches,
            csv_counts=_counts(),
            dry_run=False,
        )

        # Validar que payload haya sido inyectado con 'face' = 'front'
        payload_passed = mock_execute_sync.call_args[0][1]
        assert payload_passed["face"] == "front"
        assert payload_passed["position"] == 10
        assert returned_caches is mock_caches


class TestNodeLogFormat:
    """Los logs no deben repetir el nivel (el formateador del logger ya lo imprime)."""

    def test_hypervisor_without_cluster_message(
        self, config: NetBoxMappingConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        cols = {k: v.source for k, v in config.csv_columns.items()}
        row = {cols["manufacturer"]: "Dell", cols["model"]: "R740"}
        mock_caches = MagicMock()

        with (
            patch(
                "export_to_netbox._prepare_base_node",
                return_value={
                    "machine_name": "hv-01",
                    "inventory_uuid": "u1",
                    "machine_type": "hypervisor",
                    "payload": {},
                },
            ),
            patch("export_to_netbox._resolve_existing_object", return_value=[]),
            patch(
                "export_to_netbox._resolve_common_dependencies",
                side_effect=_keep_payload,
            ),
            patch(
                "export_to_netbox._resolve_cluster", return_value=(None, mock_caches)
            ),
            patch(
                "export_to_netbox._resolve_device_type", return_value=(1, mock_caches)
            ),
            patch(
                "export_to_netbox._execute_sync",
                return_value=SyncResult(SyncStatus.CREATED, 1, MagicMock()),
            ),
            caplog.at_level(logging.INFO),
        ):
            sync_device(
                MagicMock(),
                row,
                config,
                MockNetBoxRecord(id=1),
                {},
                {},
                MagicMock(),
                mock_caches,
                _counts(),
                False,
            )

        assert (
            "Hipervisor 'hv-01' sin cluster asignado: la columna 'Cluster' está vacía."
            in caplog.text
        )
        assert "INFO (" not in caplog.text

    def test_missing_host_device_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        devices = MagicMock()
        devices.filter.return_value = []
        config = MagicMock()
        config.csv_columns = {"host_device": MagicMock(source="Host")}

        with (
            patch("export_to_netbox.extract_csv_value", return_value="hv-02"),
            caplog.at_level(logging.WARNING),
        ):
            dev_id, _ = _resolve_host_device(
                devices, {"Host": "hv-02"}, MockNetBoxRecord(id=1), "vm-01", {}, config
            )

        assert dev_id is None
        assert (
            "El dispositivo host 'hv-02' de la VM 'vm-01' no se encontró en el site"
            in caplog.text
        )
        assert "ADVERTENCIA" not in caplog.text


class TestGetOrCreateCached:
    def test_returns_from_cache(self) -> None:
        endpoint = MagicMock()
        cache: dict[Any, Any] = {"key1": "mock_obj"}
        result, returned_cache = get_or_create_cached(
            endpoint, cache, "key1", {}, {}, "Test", False
        )
        assert result == "mock_obj"
        assert returned_cache is cache
        assert returned_cache["key1"] == "mock_obj"
        endpoint.filter.assert_not_called()
        endpoint.create.assert_not_called()

    def test_skip_filter_creates_directly(self) -> None:
        endpoint = MagicMock()
        cache: dict[Any, Any] = {}
        endpoint.name = "test_endpoints"
        endpoint.create.return_value = "created_obj"
        result, returned_cache = get_or_create_cached(
            endpoint,
            cache,
            "key1",
            {},
            {"name": "Test"},
            "Test",
            False,
            skip_filter=True,
        )
        assert result == "created_obj"
        assert "key1" in returned_cache
        assert returned_cache["key1"] == "created_obj"
        endpoint.filter.assert_not_called()
        endpoint.create.assert_called_once_with(name="Test")

    def test_dry_run_mock(self) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = []
        cache: dict[Any, Any] = {}
        endpoint.name = "test_endpoints"
        result, returned_cache = get_or_create_cached(
            endpoint, cache, "key1", {}, {"name": "Test"}, "Test", True
        )
        assert result.id == 0
        assert result.name == "Test"
        assert returned_cache["key1"] is result
        endpoint.create.assert_not_called()

    def test_create_error_raises_netbox_api_error(self) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = []
        endpoint.create.side_effect = RequestError(
            MagicMock(status_code=400, reason="Bad Request")
        )
        cache: dict[Any, Any] = {}
        endpoint.name = "test_endpoints"
        with pytest.raises(
            NetBoxApiError,
            match="No se pudo crear el objeto en 'test_endpoints' con nombre 'Test'",
        ):
            get_or_create_cached(
                endpoint, cache, "key1", {}, {"name": "Test"}, "Test", False
            )

    @patch("time.sleep", return_value=None)
    def test_multithread_duplication_race_condition(
        self, mock_sleep: MagicMock
    ) -> None:
        """
        Simula una colisión de unicidad donde otro hilo crea el objeto entre
        nuestro primer filter y el create.
        """
        endpoint = MagicMock()
        endpoint.name = "test_endpoints"

        # El primer filter no encuentra nada. El segundo filter lo encuentra.
        mock_obj = MockNetBoxRecord(id=99, name="Test")
        endpoint.filter.side_effect = [[], [mock_obj]]

        # Simular que el endpoint.create levanta un RequestError 400 por nombre duplicado.
        mock_req = MagicMock(status_code=400)
        mock_req.json.return_value = {
            "name": ["Manufacturer with this name already exists."]
        }
        endpoint.create.side_effect = RequestError(mock_req)

        cache: dict[Any, Any] = {}

        result, returned_cache = get_or_create_cached(
            endpoint, cache, "key1", {"name": "Test"}, {"name": "Test"}, "Test", False
        )

        # Verificamos que se manejó la carrera
        assert result == mock_obj
        assert returned_cache["key1"] == mock_obj
        assert endpoint.filter.call_count == 2
        endpoint.create.assert_called_once_with(name="Test")
        mock_sleep.assert_called_once()

    def test_collision_error_malformed_json_fallback(self) -> None:
        """
        Escenario destructivo: La API devuelve un error 400 pero el body no es un JSON válido.
        Se debe verificar que _is_collision_error captura el ValueError de json() y usa el fallback de texto.
        """
        mock_req = MagicMock(status_code=400)
        mock_req.json.side_effect = ValueError("Invalid JSON")
        # Simula el atributo error que usa pynetbox como fallback
        mock_error = RequestError(mock_req)
        mock_error.error = "The slug already exists."

        assert _is_collision_error(mock_error, ("slug",)) is True

    def test_collision_error_non_dict_json(self) -> None:
        """
        Escenario destructivo: La API devuelve un JSON que no es un diccionario (e.g. una lista).
        _is_collision_error debería manejarlo sin crashear y hacer fallback al texto de error.
        """
        mock_req = MagicMock(status_code=400)
        mock_req.json.return_value = ["An unexpected error array"]
        mock_error = RequestError(mock_req)
        mock_error.error = "Name already exists"

        assert _is_collision_error(mock_error, ("name",)) is True

    def test_create_with_fallback_slug_double_collision(self) -> None:
        """
        Escenario destructivo: Ocurre una colisión de slug, se genera un fallback slug,
        pero el fallback slug TAMBIÉN colisiona. Debe levantar RequestError.
        """
        endpoint = MagicMock()
        endpoint.name = "test_endpoint"

        # Simula RequestError 400 por slug colisionando SIEMPRE
        mock_req = MagicMock(status_code=400)
        mock_req.json.return_value = {"slug": ["Slug already exists."]}
        endpoint.create.side_effect = RequestError(mock_req)

        with pytest.raises(
            NetBoxApiError, match="colisión persistente de slug o rechazo"
        ):
            _create_with_fallback_slug(
                endpoint, "Test Node", name="Test Node", slug="test-node"
            )

        # Debería haber intentado crear 2 veces (original y fallback)
        assert endpoint.create.call_count == 2

    @patch("time.sleep", return_value=None)
    def test_get_or_create_cached_max_retries_exceeded(
        self, mock_sleep: MagicMock
    ) -> None:
        """
        Escenario destructivo: La creación colisiona por concurrencia y los retries fallan
        continuamente hasta exceder MAX_RETRIES. Debe lanzar NetBoxApiError.
        """
        endpoint = MagicMock()
        endpoint.name = "test_endpoint"
        endpoint.filter.return_value = []

        mock_req = MagicMock(status_code=400)
        mock_req.json.return_value = {"name": ["already exists"]}
        endpoint.create.side_effect = RequestError(mock_req)

        cache: dict[Any, Any] = {}

        with pytest.raises(
            NetBoxApiError,
            match="No se pudo crear el objeto en 'test_endpoint' con nombre 'Test Node'",
        ):
            get_or_create_cached(
                endpoint,
                cache,
                "key",
                {"name": "Test Node"},
                {"name": "Test Node"},
                "Test Node",
                False,
            )

        # El filter se llama 3 veces (1 por cada intento de retry)
        assert endpoint.filter.call_count == 3
        # El create se intenta 3 veces
        assert endpoint.create.call_count == 3
        # Hace sleep 2 veces (antes del intento 2 y 3)
        assert mock_sleep.call_count == 2

    def test_preventive_slug_search_hit(self) -> None:
        """
        Escenario destructivo/Edge case: El objeto no se encuentra por su filtro primario (ej. nombre),
        pero preventive_slug_search encuentra una colisión inminente de slug y reutiliza el objeto.
        """
        endpoint = MagicMock()
        mock_obj = MockNetBoxRecord(id=5, name="Mock Name", slug="mock-slug")

        # side_effect: Primera llamada (filter por name) -> vacío.
        # Segunda llamada (filter por slug) -> devuelve el mock_obj.
        def mock_filter(**kwargs: Any) -> list[MockNetBoxRecord]:
            if "slug" in kwargs:
                return [mock_obj]
            return []

        endpoint.filter.side_effect = mock_filter

        result = _search_in_netbox(
            endpoint=endpoint,
            filter_kwargs={"name": "Different Name"},
            create_kwargs={"name": "Different Name", "slug": "mock-slug"},
            name="Different Name",
            preventive_slug_search=True,
        )

        assert result == mock_obj
        assert endpoint.filter.call_count == 2


class TestInvalidMachineTypeDoesNotAbortPipeline:
    """
    Un 'machine_type' fuera del mapa debe contarse como ERROR de su fila
    (vía _classify_rows) y no abortar el pipeline desde el pre-escaneo global.
    """

    @staticmethod
    def _row(config: NetBoxMappingConfig, machine_type: str, **extra: str) -> CsvRow:
        src = {k: v.source for k, v in config.csv_columns.items()}
        row = {
            src["machine_name"]: extra.get("name", "srv-01"),
            src["machine_type"]: machine_type,
            src["cluster_name"]: extra.get("cluster", "Cluster-A"),
            src["hypervisor_os"]: extra.get("hypervisor_os", ""),
        }
        return row

    def test_precompute_ignores_unknown_machine_type(
        self, config: NetBoxMappingConfig
    ) -> None:
        rows = [
            self._row(config, "Contenedor", name="srv-x", hypervisor_os="ESXi"),
            self._row(config, "Hipervisor", name="hv-1", hypervisor_os="Proxmox VE"),
        ]
        assert precompute_cluster_type_map(rows, config) == {"Cluster-A": "Proxmox VE"}

    def test_main_counts_invalid_row_as_error_and_syncs_the_rest(
        self,
        config: NetBoxMappingConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        rows = [
            self._row(config, "Contenedor", name="srv-x"),
            self._row(config, "Dedicada", name="srv-ok"),
        ]
        synced: list[str] = []
        name_col = config.csv_columns["machine_name"].source

        def fake_sync_row(
            row_num: int, row: CsvRow, *args: Any
        ) -> tuple[SyncCounts, CacheStore]:
            counts: SyncCounts = args[-2]
            synced.append(row[name_col])
            counts[SyncStatus.UNCHANGED] += 1
            return counts, args[-4]

        monkeypatch.setattr(
            sys, "argv", ["export_to_netbox.py", str(tmp_path / "in.csv")]
        )
        with (
            patch("export_to_netbox.load_config", return_value=config),
            patch(
                "export_to_netbox.read_and_validate_csv",
                return_value=CsvData(rows, list(range(2, 2 + len(rows)))),
            ),
            patch("export_to_netbox.load_env", return_value=("u", "t", True)),
            patch("export_to_netbox.build_nb_client"),
            patch("export_to_netbox.build_netbox_endpoints"),
            patch("export_to_netbox.ensure_custom_fields"),
            patch("export_to_netbox.ensure_site"),
            patch(
                "export_to_netbox.ensure_dynamic_cluster_types",
                return_value=(MagicMock(), {}),
            ),
            patch("export_to_netbox.ensure_all_device_roles", return_value=({}, {})),
            patch("export_to_netbox._sync_row", side_effect=fake_sync_row),
            pytest.raises(SystemExit) as exit_info,
        ):
            main()

        assert synced == ["srv-ok"]
        assert exit_info.value.code == 1  # la fila inválida cuenta como ERROR

    def test_main_excludes_discarded_rows_from_identity_counts(
        self,
        config: NetBoxMappingConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Una fila descartada con el mismo nombre/UUID no debe volver ambigua a la válida."""
        uuid_col = config.csv_columns["inventory_uuid"].source
        rows = [
            self._row(config, "Contenedor", name="srv-a") | {uuid_col: "U-1"},
            self._row(config, "Dedicada", name="srv-a") | {uuid_col: "U-1"},
        ]
        seen: list[CsvIdentityCounts] = []

        def fake_sync_row(
            row_num: int, row: CsvRow, *args: Any
        ) -> tuple[SyncCounts, CacheStore]:
            seen.append(args[-3])
            return args[-2], args[-4]

        monkeypatch.setattr(
            sys, "argv", ["export_to_netbox.py", str(tmp_path / "in.csv")]
        )
        with (
            patch("export_to_netbox.load_config", return_value=config),
            patch(
                "export_to_netbox.read_and_validate_csv",
                return_value=CsvData(rows, [2, 3]),
            ),
            patch("export_to_netbox.load_env", return_value=("u", "t", True)),
            patch("export_to_netbox.build_nb_client"),
            patch("export_to_netbox.build_netbox_endpoints"),
            patch("export_to_netbox.ensure_custom_fields"),
            patch("export_to_netbox.ensure_site"),
            patch(
                "export_to_netbox.ensure_dynamic_cluster_types",
                return_value=(MagicMock(), {}),
            ),
            patch("export_to_netbox.ensure_all_device_roles", return_value=({}, {})),
            patch("export_to_netbox._sync_row", side_effect=fake_sync_row),
            pytest.raises(SystemExit),
        ):
            main()

        assert len(seen) == 1
        assert seen[0].names["srv-a"] == 1
        assert seen[0].uuids["u-1"] == 1

    def test_main_counts_rejected_csv_records_as_errors(
        self,
        config: NetBoxMappingConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        rows = [self._row(config, "Dedicada", name="srv-ok")]
        monkeypatch.setattr(
            sys, "argv", ["export_to_netbox.py", str(tmp_path / "in.csv")]
        )

        def fake_sync_row(
            row_num: int, row: CsvRow, *args: Any
        ) -> tuple[SyncCounts, CacheStore]:
            counts: SyncCounts = args[-2]
            counts[SyncStatus.UNCHANGED] += 1
            return counts, args[-4]

        with (
            patch("export_to_netbox.load_config", return_value=config),
            patch(
                "export_to_netbox.read_and_validate_csv",
                return_value=CsvData(rows, [2], rejected_rows=1),
            ),
            patch("export_to_netbox.load_env", return_value=("u", "t", True)),
            patch("export_to_netbox.build_nb_client"),
            patch("export_to_netbox.build_netbox_endpoints"),
            patch("export_to_netbox.ensure_custom_fields"),
            patch("export_to_netbox.ensure_site"),
            patch(
                "export_to_netbox.ensure_dynamic_cluster_types",
                return_value=(MagicMock(), {}),
            ),
            patch("export_to_netbox.ensure_all_device_roles", return_value=({}, {})),
            patch("export_to_netbox._sync_row", side_effect=fake_sync_row),
            pytest.raises(SystemExit) as exit_info,
        ):
            main()

        assert exit_info.value.code == 1


class TestReadCsvLineNumbers:
    """Las líneas reportadas deben ser las del archivo, aun con celdas multilínea."""

    @staticmethod
    def _write(path: Path, content: str, encoding: str = "utf-8") -> Path:
        path.write_bytes(content.encode(encoding))
        return path

    def test_simple_csv(self, tmp_path: Path) -> None:
        csv_path = self._write(tmp_path / "a.csv", "A,B\n1,2\n3,4\n")
        _, data = _read_csv(csv_path)
        assert data.line_numbers == [2, 3]
        assert len(data.rows) == 2

    def test_multiline_cell_and_blank_line(self, tmp_path: Path) -> None:
        content = 'A,B\n"x\ny\nz",1\n\nq,2\nr,3\n'
        _, data = _read_csv(self._write(tmp_path / "b.csv", content))
        # El 1.er registro ocupa las líneas 2-4; la línea 5 está en blanco.
        assert data.line_numbers == [2, 6, 7]

    def test_utf8_bom_does_not_shift_lines(self, tmp_path: Path) -> None:
        csv_path = self._write(tmp_path / "c.csv", "\ufeffA,B\n1,2\n", "utf-8")
        headers, data = _read_csv(csv_path)
        assert headers == ["A", "B"]
        assert data.line_numbers == [2]

    def test_short_record_is_padded_with_empty_strings(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Una fila corta no debe dejar celdas None (rompían .strip() aguas abajo)."""
        content = "A,B,C\n1,2,3\nsolo-a\n"
        with caplog.at_level(logging.WARNING):
            _, data = _read_csv(self._write(tmp_path / "e.csv", content))

        assert data.rows[1] == {"A": "solo-a", "B": "", "C": ""}
        assert all(isinstance(v, str) for r in data.rows for v in r.values())
        assert data.rejected_rows == 0
        assert "Línea 3: faltan 2 campo(s)" in caplog.text

    def test_long_record_is_rejected_and_counted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Campos sobrantes (coma sin comillas) desplazan columnas: se descarta la fila."""
        content = "A,B\n1,2\n3,4,5\n6,7\n"
        with caplog.at_level(logging.ERROR):
            _, data = _read_csv(self._write(tmp_path / "f.csv", content))

        assert data.rows == [{"A": "1", "B": "2"}, {"A": "6", "B": "7"}]
        assert data.line_numbers == [2, 4]  # paralelas a las filas conservadas
        assert data.rejected_rows == 1
        assert "línea 3" in caplog.text

    def test_short_record_after_multiline_cell_keeps_line_numbers(
        self, tmp_path: Path
    ) -> None:
        content = 'A,B\n"x\ny",1\nsolo\n'
        _, data = _read_csv(self._write(tmp_path / "g.csv", content))
        assert data.line_numbers == [2, 4]

    def test_empty_body(self, tmp_path: Path) -> None:
        _, data = _read_csv(self._write(tmp_path / "d.csv", "A,B\n"))
        assert data == CsvData([], [])


class TestEnsureTaxonomyQACases:
    """
    Pruebas QA destructivas y de edge cases para el código de taxonomía (ensure_*).
    """

    def test_precompute_cluster_type_map_conflict(self) -> None:
        """
        Escenario: Inconsistencia en el inventario maestro para SO de hipervisores.
        Dos nodos del mismo cluster reportan SO distintos. Debe explotar ruidosamente.
        """
        config = MagicMock()

        def mock_extract(
            row: dict[str, str], col: str, config_mock: Any, **kwargs: Any
        ) -> str:
            return row.get(col, "")

        config.extract_csv_value = mock_extract

        mock_machine_type_col = MagicMock()
        mock_machine_type_col.cluster_host_types = ["hypervisor"]
        config.csv_columns = {"machine_type": mock_machine_type_col}

        rows = [
            {
                "machine_type": "hypervisor",
                "cluster_name": "Cluster-X",
                "hypervisor_os": "ESXi 7",
            },
            {
                "machine_type": "hypervisor",
                "cluster_name": "Cluster-X",
                "hypervisor_os": "ESXi 8",
            },
        ]

        with (
            patch("export_to_netbox.extract_csv_value", side_effect=mock_extract),
            pytest.raises(
                ConfigValidationError, match="Conflicto de SO en el clúster 'Cluster-X'"
            ),
        ):
            precompute_cluster_type_map(cast(Any, rows), config)

    def test_sync_single_device_role_api_error_wrap(self) -> None:
        """
        Escenario: La creación de un DeviceRole falla a nivel API.
        _sync_single_device_role debe atrapar NetBoxApiError y envolverlo en ConfigValidationError.
        """
        endpoints = MagicMock()
        endpoints.device_roles.name = "device_roles"
        endpoints.device_roles.filter.return_value = []
        endpoints.device_roles.create.side_effect = RequestError(
            MagicMock(status_code=403, reason="Forbidden")
        )

        role_def = DeviceRoleConfig(
            name="CoreRouter", slug="core-router", color="0000ff"
        )

        with pytest.raises(
            ConfigValidationError, match="Error procesando DeviceRole 'CoreRouter'"
        ):
            _sync_single_device_role(endpoints, role_def, {}, dry_run=False)

    @patch("export_to_netbox.get_or_create_cached")
    def test_sync_single_device_role_cache_and_update(
        self, mock_get_or_create: MagicMock
    ) -> None:
        """
        Escenario: Un DeviceRole ya existe en caché, pero tiene vm_role distinto al YAML.
        _sync_single_device_role debe mutarlo in-place invocando .update(),
        y la caché debe retornar con el objeto correcto sin reasignaciones redundantes.
        """
        endpoints = MagicMock()
        # En el yaml, este rol dice vm_role=True
        role_def = DeviceRoleConfig(
            name="AccessSwitch", slug="access", color="000000", vm_role=True
        )

        mock_obj = MagicMock(id=10, vm_role=False)
        cache_initial = cast(NameCache, {"accessswitch": mock_obj})
        mock_get_or_create.return_value = (mock_obj, cache_initial)

        returned_obj, returned_cache = _sync_single_device_role(
            endpoints, role_def, cache_initial, dry_run=False
        )

        mock_obj.update.assert_called_once_with({"vm_role": True})
        assert returned_obj is mock_obj
        assert returned_cache["accessswitch"] is mock_obj

    @patch("export_to_netbox.get_or_create_cached")
    def test_sync_single_device_role_cache_and_update_to_false(
        self, mock_get_or_create: MagicMock
    ) -> None:
        """
        Escenario: Un DeviceRole tiene vm_role=False en el YAML, pero True en NetBox.
        Debe actualizarse hacia False.
        """
        endpoints = MagicMock()
        role_def = DeviceRoleConfig(
            name="Hypervisor", slug="hypervisor", color="000000", vm_role=False
        )

        mock_obj = MagicMock(id=10, vm_role=True)
        cache_initial = cast(NameCache, {"hypervisor": mock_obj})
        mock_get_or_create.return_value = (mock_obj, cache_initial)

        returned_obj, _ = _sync_single_device_role(
            endpoints, role_def, cache_initial, dry_run=False
        )

        mock_obj.update.assert_called_once_with({"vm_role": False})
        assert returned_obj is mock_obj

    def test_get_or_create_preventive_slug_without_slug_kwarg(self) -> None:
        """
        Escenario: Búsqueda preventiva por slug activa, pero omitiendo 'slug' en create_kwargs.
        Debe omitir la búsqueda por slug (sin KeyError) y crear el objeto de todos modos.
        """
        endpoint = MagicMock()
        endpoint.name = "generic"
        endpoint.filter.return_value = []
        mock_created = MockNetBoxRecord(id=99, name="NoSlug")
        endpoint.create.return_value = mock_created

        cache: NameCache = {}
        result, _ = get_or_create_cached(
            endpoint,
            cache=cache,
            cache_key="key",
            filter_kwargs={"name": "NoSlug"},
            create_kwargs={"name": "NoSlug"},  # Falta 'slug' intencionalmente
            name="NoSlug",
            dry_run=False,
            preventive_slug_search=True,
        )

        assert result.id == 99
        endpoint.filter.assert_called_once_with(name="NoSlug")
        endpoint.create.assert_called_once_with(name="NoSlug")

    def test_ensure_device_type_blade_0u_overwrite_bug(self) -> None:
        """
        Escenario Edge Case Lógico: NetBox permite U-Height de 0.0 para blades lógicos.
        Validamos que `ensure_device_type` pase correctamente `0.0` en la creación sin
        sobrescribirlo erróneamente por un `or 1.0`.
        """
        endpoint = MagicMock()
        endpoint.name = "device_types"
        endpoint.filter.return_value = []
        mock_dt = MagicMock()
        mock_dt.id = 5
        mock_dt.model = "Blade"
        mock_dt.u_height = 0.0
        mock_dt.update = MagicMock()
        endpoint.create.return_value = mock_dt

        manufacturer = MockNetBoxRecord(id=10, name="Dell")

        ensure_device_type(
            device_types_endpoint=endpoint,
            manufacturer=manufacturer,
            model="Blade",
            attributes=DeviceTypeAttributes(0.0, None),
            cache={},
            dry_run=False,
        )

        endpoint.create.assert_called_once()
        create_call_args = endpoint.create.call_args[1]
        assert create_call_args["u_height"] == 0.0

    def test_sync_device_type_attributes_update_failure(self) -> None:
        """
        Escenario: Actualización de u_height/part_number falla debido a un problema con NetBox.
        """
        mock_record = MagicMock()
        mock_record.u_height = 1.0
        mock_record.part_number = ""
        mock_record.update.side_effect = RequestError(MagicMock(status_code=500))

        with pytest.raises(
            NetBoxApiError, match="Error actualizando DeviceType 'R640'"
        ):
            _sync_device_type_attributes(
                existing_dt=mock_record,
                model="R640",
                attributes=DeviceTypeAttributes(2.0, "PN"),
                dry_run=False,
            )


class TestSyncVM:
    @patch("export_to_netbox._prepare_base_node")
    @patch("export_to_netbox._resolve_cluster")
    @patch("export_to_netbox._execute_sync")
    @patch("export_to_netbox._resolve_existing_object", return_value=[])
    @patch("export_to_netbox._resolve_common_dependencies", side_effect=_keep_payload)
    def test_sync_vm_cache_propagation(
        self,
        mock_common_deps: MagicMock,
        mock_resolve_existing: MagicMock,
        mock_execute_sync: MagicMock,
        mock_cluster: MagicMock,
        mock_base_node: MagicMock,
    ) -> None:
        row = {"machine_name": "vm1", "cluster_name": "Cluster-X"}

        mock_config = MagicMock()
        mock_config.is_empty.return_value = False
        mock_config.csv_columns = {
            "rack_location": MagicMock(source="rack_location"),
            "cluster_name": MagicMock(source="cluster_name"),
            "machine_name": MagicMock(source="machine_name"),
            "host_device": MagicMock(source="host_device"),
        }

        mock_caches = MagicMock()
        mock_base_node.return_value = {
            "machine_name": "vm1",
            "inventory_uuid": "1234",
            "machine_type": "vm",
            "payload": {},
        }
        mock_cluster.return_value = (1, mock_caches.clusters)
        mock_execute_sync.return_value = (SyncStatus.CREATED, 1, MagicMock())

        _, returned_caches = sync_vm(
            endpoints=MagicMock(),
            row=row,
            config=mock_config,
            site=MagicMock(),
            cluster_type_map={},
            fallback_cluster_type=MagicMock(),
            caches=mock_caches,
            csv_counts=_counts(),
            dry_run=False,
        )

        assert returned_caches is mock_caches


class TestProcessInterfacesAndIps:
    @patch("export_to_netbox._sync_interfaces_for_object")
    @patch("export_to_netbox._assign_primary_ipv4")
    @patch("export_to_netbox._assign_primary_mac")
    def test_pure_returns_correct_deltas(
        self,
        mock_assign_mac: MagicMock,
        mock_assign_ipv4: MagicMock,
        mock_sync_ifaces: MagicMock,
    ) -> None:
        # Configurar mock_sync_ifaces para retornar (errores=2, ipv4_candidates=[...], ifaces_changed=False)
        mock_iface = MagicMock()
        mock_mac = MagicMock()
        mock_sync_ifaces.return_value = (2, [(10, mock_iface, mock_mac)], False)

        # Simular que se cambió la IP pero no la MAC
        mock_assign_ipv4.return_value = True
        mock_assign_mac.return_value = False

        iface_errors, any_changes = process_interfaces_and_ips(
            endpoints=MagicMock(),
            obj_id=1,
            node_type=MagicMock(),
            interfaces=[],
            args=MagicMock(dry_run=False, prune_network_orphans=False),
            main_obj=MagicMock(),
            machine_name="test-machine",
        )

        assert iface_errors == 2
        assert any_changes is True
        mock_assign_ipv4.assert_called_once()
        mock_assign_mac.assert_called_once()


def _ip_record(
    assigned_object_id: int | None,
    iface_name: str = "eth0",
    parent_name: str = "SRV-A",
) -> MockNetBoxRecord:
    """Construye un registro IP simulado de NetBox, asignado o libre."""
    data: dict[str, Any] = {
        "id": 1,
        "address": "10.0.0.5/26",
        "assigned_object_id": assigned_object_id,
    }
    if assigned_object_id is not None:
        data["assigned_object_type"] = "dcim.interface"
        data["assigned_object"] = SimpleNamespace(
            id=assigned_object_id,
            name=iface_name,
            device=SimpleNamespace(id=7, name=parent_name),
        )
    return MockNetBoxRecord.model_validate(data)


class TestDescribeAssignment:
    """Verifica la descripción (para logs) del dueño de un recurso asignado."""

    def test_full_description(self) -> None:
        assert (
            _describe_assignment(_ip_record(99)) == "interfaz 'eth0' del nodo 'SRV-A'"
        )

    def test_without_parent(self) -> None:
        record = MockNetBoxRecord.model_validate(
            {
                "id": 1,
                "assigned_object_id": 5,
                "assigned_object": SimpleNamespace(name="eth0"),
            }
        )
        assert _describe_assignment(record) == "interfaz 'eth0'"

    def test_without_nested_object_falls_back_to_id(self) -> None:
        record = MockNetBoxRecord(id=1, assigned_object_id=5)
        assert _describe_assignment(record) == "objeto ID=5"


class TestAssignIpAssignedElsewhere:
    """Una IP ya asignada a otra interfaz se omite con WARNING en lugar de fallar con HTTP 400."""

    IFACE = MockNetBoxRecord(id=10, name="eth0")

    def test_ip_owned_by_other_interface_is_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = [_ip_record(99)]

        with caplog.at_level(logging.WARNING):
            result = _assign_ip(endpoint, "10.0.0.5/26", self.IFACE, dry_run=False)

        assert result == (None, False)
        endpoint.create.assert_not_called()
        assert "interfaz 'eth0' del nodo 'SRV-A'" in caplog.text
        assert "se omite" in caplog.text

    def test_dry_run_also_skips_without_writing(self) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = [_ip_record(99)]

        assert _assign_ip(endpoint, "10.0.0.5/26", self.IFACE, dry_run=True) == (
            None,
            False,
        )
        endpoint.create.assert_not_called()

    def test_ip_already_on_own_interface_is_unchanged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        endpoint = MagicMock()
        own = _ip_record(10)
        endpoint.filter.return_value = [own]

        with caplog.at_level(logging.WARNING):
            result = _assign_ip(endpoint, "10.0.0.5/26", self.IFACE, dry_run=False)

        assert result == (own, False)
        assert caplog.text == ""

    def test_free_record_takes_priority_over_foreign_one(self) -> None:
        endpoint = MagicMock()
        free = MagicMock(assigned_object_id=None)
        endpoint.filter.return_value = [_ip_record(99), free]

        obj, changed = _assign_ip(endpoint, "10.0.0.5/26", self.IFACE, dry_run=False)

        assert obj is free
        assert changed is True
        free.update.assert_called_once()
        endpoint.create.assert_not_called()

    def test_new_ip_is_created(self) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = []

        obj, changed = _assign_ip(endpoint, "10.0.0.5/26", self.IFACE, dry_run=False)

        assert obj is endpoint.create.return_value
        assert changed is True

    def test_mac_shared_between_interfaces_is_still_created(self) -> None:
        """Las MAC no son únicas en NetBox: no se aplica la regla de IPs."""
        endpoint = MagicMock()
        endpoint.filter.return_value = [_ip_record(99)]

        obj, changed = _assign_mac(endpoint, "AA:BB:CC:DD:EE:FF", self.IFACE, False)

        assert obj is endpoint.create.return_value
        assert changed is True


class TestSyncSingleInterface:
    @patch("export_to_netbox._upsert_interface_record")
    @patch("export_to_netbox._assign_ip")
    @patch("export_to_netbox._assign_mac")
    def test_sync_single_interface_returns_cache(
        self,
        mock_assign_mac: MagicMock,
        mock_assign_ip: MagicMock,
        mock_upsert: MagicMock,
    ) -> None:
        # Configurar mocks
        mock_iface = MagicMock()
        mock_upsert.return_value = (mock_iface, True)
        mock_assign_ip.return_value = (MagicMock(), False)
        mock_assign_mac.return_value = (MagicMock(), False)

        mock_endpoints = MagicMock(spec=NetBoxEndpoints)
        mock_endpoints.ip_addresses = MagicMock()
        mock_endpoints.mac_addresses = MagicMock()

        ifaces_cache: NameCache = {"eth0": MagicMock(spec=NetBoxObject)}
        iface_data = cast(
            NetworkInterfaceData,
            {
                "name": "eth1",
                "enabled": True,
                "cidr": "1.1.1.1/24",
                "mac": "AA:BB",
                "ip": "1.1.1.1",
                "prefix": "24",
            },
        )

        mock_endpoint = MagicMock(spec=Endpoint)
        mock_endpoint.url = "http://localhost/api/dcim/interfaces/"

        single_result, returned_cache = _sync_single_interface(
            iface_data=iface_data,
            obj_id=1,
            iface_endpoint=mock_endpoint,
            ifaces_cache=ifaces_cache,
            endpoints=mock_endpoints,
            dry_run=False,
        )
        _, _, _, any_changes = single_result

        # Assert: the cache returned is the same object
        assert returned_cache is ifaces_cache
        # Assert: eth1 was added to the cache
        assert "eth1" in returned_cache
        assert returned_cache["eth1"] == mock_iface
        assert any_changes is True


class TestSyncSingleInterfaceSkippedIp:
    @patch("export_to_netbox._upsert_interface_record")
    @patch("export_to_netbox._assign_ip", return_value=(None, False))
    @patch("export_to_netbox._assign_mac")
    def test_skipped_ip_does_not_block_mac_nor_count_as_error(
        self,
        mock_assign_mac: MagicMock,
        mock_assign_ip: MagicMock,
        mock_upsert: MagicMock,
    ) -> None:
        mock_upsert.return_value = (MagicMock(), False)
        mock_assign_mac.return_value = (MagicMock(), False)
        endpoint = MagicMock(spec=Endpoint)
        endpoint.url = "http://localhost/api/dcim/interfaces/"
        endpoints = MagicMock(spec=NetBoxEndpoints)
        endpoints.ip_addresses = MagicMock()
        endpoints.mac_addresses = MagicMock()
        iface_data = cast(
            NetworkInterfaceData,
            {
                "name": "eth0",
                "enabled": True,
                "cidr": "10.0.0.5/26",
                "mac": "aa:bb:cc:dd:ee:ff",
                "ip": "10.0.0.5",
                "prefix": "26",
                "network": None,
            },
        )

        (_, ip_obj, mac_obj, changed), _ = _sync_single_interface(
            iface_data, 1, endpoint, {}, endpoints, False
        )

        mock_assign_ip.assert_called_once()
        mock_assign_mac.assert_called_once()
        assert ip_obj is None
        assert mac_obj is not None
        assert changed is False


class TestSyncSingleInterfacePrefix:
    """Verifica que el Prefix se asegure antes de asignar la IP."""

    @staticmethod
    def _run(iface_data: dict[str, Any]) -> tuple[MagicMock, MagicMock, MagicMock]:
        mock_endpoints = MagicMock(spec=NetBoxEndpoints)
        mock_endpoints.ip_addresses = MagicMock()
        mock_endpoints.mac_addresses = MagicMock()
        mock_endpoints.prefixes = MagicMock()
        parent = MagicMock()
        with (
            patch(
                "export_to_netbox._upsert_interface_record",
                return_value=(MagicMock(), False),
            ),
            patch(
                "export_to_netbox.ensure_prefix", return_value=(MagicMock(), True)
            ) as ensure_prefix,
            patch(
                "export_to_netbox._assign_ip", return_value=(MagicMock(), False)
            ) as assign_ip,
        ):
            parent.attach_mock(ensure_prefix, "ensure_prefix")
            parent.attach_mock(assign_ip, "assign_ip")
            mock_endpoint = MagicMock(spec=Endpoint)
            mock_endpoint.url = "http://localhost/api/dcim/interfaces/"
            _sync_single_interface(
                iface_data=cast(NetworkInterfaceData, iface_data),
                obj_id=1,
                iface_endpoint=mock_endpoint,
                ifaces_cache={},
                endpoints=mock_endpoints,
                dry_run=False,
            )
        return parent, ensure_prefix, assign_ip

    def test_prefix_ensured_before_ip(self) -> None:
        parent, ensure_prefix, assign_ip = self._run(
            {
                "name": "eth0",
                "enabled": True,
                "cidr": "192.168.1.10/24",
                "network": "192.168.1.0/24",
                "mac": None,
                "ip": "192.168.1.10",
                "prefix": "192.168.1.0/24",
            }
        )
        ensure_prefix.assert_called_once()
        assert ensure_prefix.call_args.args[1] == "192.168.1.0/24"
        assign_ip.assert_called_once()
        assert [c[0] for c in parent.mock_calls[:2]] == ["ensure_prefix", "assign_ip"]

    def test_no_prefix_without_cidr(self) -> None:
        _, ensure_prefix, assign_ip = self._run(
            {
                "name": "eth0",
                "enabled": True,
                "cidr": None,
                "network": None,
                "mac": None,
                "ip": None,
                "prefix": None,
            }
        )
        ensure_prefix.assert_not_called()
        assign_ip.assert_not_called()


class TestClassifyRows:
    """QA Tester verification para _classify_rows."""

    def test_classify_rows_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verifica que las filas se dividan correctamente según su NodeType."""

        def mock_get_node_type(row: dict[str, str], config: Any) -> NodeType:
            if row.get("type") == "vm":
                return NodeType.VIRTUAL_MACHINE
            return NodeType.DEVICE

        monkeypatch.setattr(
            export_to_netbox, "get_node_type_from_row", mock_get_node_type
        )

        rows = [
            {"type": "device", "machine_name": "host1"},
            {"type": "vm", "machine_name": "vm1"},
            {"type": "device", "machine_name": "host2"},
        ]

        counts = {SyncStatus.ERROR: 0}

        result, new_counts = _classify_rows(rows, [2, 3, 4], MagicMock(), counts)  # type: ignore

        assert len(result.device_rows) == 2
        assert len(result.vm_rows) == 1
        assert new_counts[SyncStatus.ERROR] == 0
        # Se propagan las líneas reales recibidas, no el índice de la lista
        assert result.device_rows[0][0] == 2
        assert result.vm_rows[0][0] == 3
        assert result.device_rows[1][0] == 4

    def test_classify_rows_validation_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verifica el caso de borde destructivo: Una fila corrompida lanza RowValidationError."""

        def mock_get_node_type(row: dict[str, str], config: Any) -> NodeType:
            if row.get("type") == "invalid":
                raise RowValidationError("Invalid node type")
            return NodeType.DEVICE

        monkeypatch.setattr(
            export_to_netbox, "get_node_type_from_row", mock_get_node_type
        )
        monkeypatch.setattr(
            export_to_netbox,
            "extract_csv_value",
            lambda r, k, c, **kwargs: "broken_host",
        )

        rows = [
            {"type": "invalid", "machine_name": "broken_host"},
            {"type": "device", "machine_name": "host1"},
        ]

        counts = {SyncStatus.ERROR: 5}  # Empezamos con un estado previo

        result, new_counts = _classify_rows(
            rows,
            [2, 7],
            MagicMock(),
            counts,  # type: ignore
        )

        assert len(result.device_rows) == 1
        assert len(result.vm_rows) == 0
        assert new_counts[SyncStatus.ERROR] == 6  # Se sumó 1 error
        # La fila válida conserva su línea real (7), no su índice en la lista
        assert result.device_rows[0][0] == 7
        assert "línea 2 ('broken_host')" in caplog.text


class TestFormatDiffKeys:
    """Pruebas exhaustivas (qa-tester) para la utilidad format_diff_keys."""

    def test_format_diff_keys_simple(self) -> None:
        """Caso 1: Llaves planas sin anidación."""
        obj = MagicMock(status="active", serial="123")
        diff = {"status": "planned", "serial": "456"}
        keys = format_diff_keys(obj, diff)
        assert keys == ["status", "serial"]

    def test_format_diff_keys_custom_fields_partial(self) -> None:
        """Caso 2: Subdiccionario donde solo cambia una subllave específica."""
        # El record actual tiene un custom_field "ambiente" y "prioridad"
        obj = MagicMock()
        obj.custom_fields = {"ambiente": "PROD", "prioridad": 1}

        # El payload actualiza "ambiente" a "DEV", y vuelve a enviar "prioridad" igual, y añade "nuevo"
        diff = {"custom_fields": {"ambiente": "DEV", "prioridad": 1, "nuevo": "test"}}

        keys = format_diff_keys(obj, diff)
        # Solo deben reportarse los que realmente cambiaron respecto al original
        assert keys == ["custom_fields.ambiente", "custom_fields.nuevo"]

    def test_format_diff_keys_dict_attribute_missing(self) -> None:
        """Caso 3: El objeto no tiene el atributo anidado (fallback a key padre)."""
        obj = MagicMock()
        del obj.custom_fields  # Simulamos que no existe la propiedad

        diff = {"custom_fields": {"ambiente": "DEV"}}
        keys = format_diff_keys(obj, diff)

        assert keys == ["custom_fields"]

    def test_format_diff_keys_attribute_not_dict(self) -> None:
        """Caso 4: El objeto tiene el atributo pero NO es un diccionario (fallback)."""
        obj = MagicMock()
        obj.custom_fields = "esto es un string, no un dict"

        diff = {"custom_fields": {"ambiente": "DEV"}}
        keys = format_diff_keys(obj, diff)

        assert keys == ["custom_fields"]

    def test_format_diff_keys_empty_diff(self) -> None:
        """Caso 5: Diff completamente vacío."""
        obj = MagicMock()
        keys = format_diff_keys(obj, {})
        assert keys == []


class TestCheckRecordChanges:
    """QA Tester verification para check_record_changes (Pure Attribute Comparison)."""

    def test_primitive_changes(self) -> None:
        """Verifica que detecte cambios o igualdades en strings y enteros."""
        record = MagicMock()
        record.name = "Host 1"
        record.u_height = 2

        # Sin cambios
        diff1 = check_record_changes(record, {"name": "Host 1", "u_height": 2})
        assert diff1 == {}

        # Con cambios
        diff2 = check_record_changes(record, {"name": "Host 2", "u_height": 3})
        assert diff2 == {"name": "Host 2", "u_height": 3}

    def test_nested_object_changes(self) -> None:
        """Verifica la lógica de FK anidadas (ej. record.cluster.id vs payload['cluster'])."""
        record = MagicMock()
        record.cluster = MagicMock(id=5)
        record.status = MagicMock(value="active")

        # 1. Payload pasa ID int
        diff = check_record_changes(record, {"cluster": 5})
        assert diff == {}
        diff = check_record_changes(record, {"cluster": 6})
        assert diff == {"cluster": 6}

        # 2. Payload pasa dict {"id": X}
        diff = check_record_changes(record, {"cluster": {"id": 5}})
        assert diff == {}
        diff = check_record_changes(record, {"cluster": {"id": 6}})
        assert diff == {"cluster": {"id": 6}}

        # 3. Selectores por Value
        diff = check_record_changes(record, {"status": "active"})
        assert diff == {}
        diff = check_record_changes(record, {"status": "offline"})
        assert diff == {"status": "offline"}

    def test_custom_fields_changes(self) -> None:
        """Verifica la lógica de merge e igualdades en custom_fields."""
        record = MagicMock()
        record.custom_fields = {"env": "prod", "owner": "IT"}

        # Sin cambios (payload solo manda 1 campo, pero es igual)
        diff = check_record_changes(record, {"custom_fields": {"env": "prod"}})
        assert diff == {}

        # Con cambios (payload cambia 1 campo)
        diff = check_record_changes(record, {"custom_fields": {"env": "dev"}})
        assert diff == {"custom_fields": {"env": "dev"}}

        # Con cambios (añade 1 campo nuevo)
        diff = check_record_changes(record, {"custom_fields": {"new_tag": "test"}})
        assert diff == {"custom_fields": {"new_tag": "test"}}


class TestIsCustomFieldChanged:
    """QA Tester verification para _is_custom_field_changed."""

    def test_custom_field_changed(self) -> None:
        """Verifica la lógica de detección de cambios en custom_fields."""
        # 1. new_val no es dict
        assert _is_custom_field_changed("prod", "dev") is True
        assert _is_custom_field_changed("prod", "prod") is False

        # 2. curr_val no es dict
        assert _is_custom_field_changed(None, {"env": "prod"}) is True

        # 3. Ambos dicts, sin cambios en las llaves especificadas
        assert (
            _is_custom_field_changed({"env": "prod", "owner": "IT"}, {"env": "prod"})
            is False
        )

        # 4. Ambos dicts, con cambios en valor
        assert _is_custom_field_changed({"env": "prod"}, {"env": "dev"}) is True

        # 5. Ambos dicts, llave nueva en new_val
        assert _is_custom_field_changed({"env": "prod"}, {"new_tag": "test"}) is True


class TestIsRelationChanged:
    """QA Tester verification para _is_relation_changed."""

    def test_relation_changed(self) -> None:
        """Verifica la lógica de detección de cambios en FKs y Choices."""
        # 1. curr_val es None
        assert _is_relation_changed(None, 5) is True
        assert _is_relation_changed(None, None) is False

        # 2. Atributos primitivos (sin id ni value)
        curr_val_str = "simple_string"
        assert _is_relation_changed(curr_val_str, 5) is None

        # 3. ID de FK
        curr_val_fk = MagicMock(id=5)
        assert _is_relation_changed(curr_val_fk, 5) is False
        assert _is_relation_changed(curr_val_fk, 6) is True
        assert _is_relation_changed(curr_val_fk, None) is True

        # 4. ID dentro de un dict
        assert _is_relation_changed(curr_val_fk, {"id": 5}) is False
        assert _is_relation_changed(curr_val_fk, {"id": 6}) is True

        # 5. Selector por value
        curr_val_choice = MagicMock(value="active")
        assert _is_relation_changed(curr_val_choice, "active") is False
        assert _is_relation_changed(curr_val_choice, "offline") is True
        assert _is_relation_changed(curr_val_choice, None) is True

        # 6. Tipo no manejable (ej. new_val es lista, falla el isinstance)
        assert _is_relation_changed(curr_val_fk, [5]) is None


class TestSyncRowEdgeCases:
    """
    Casos destructivos para _sync_row, validando especialmente la lógica
    de escalación de contadores y el manejo de excepciones.
    """

    @patch("export_to_netbox.extract_csv_value")
    @patch("export_to_netbox._process_node_sync")
    @patch("export_to_netbox.parse_row_interfaces")
    @patch("export_to_netbox.process_interfaces_and_ips")
    def test_sync_row_no_escalation_on_error(
        self,
        mock_process_interfaces: MagicMock,
        mock_parse_interfaces: MagicMock,
        mock_process_node: MagicMock,
        mock_extract_csv_value: MagicMock,
    ) -> None:
        """
        Escenario destructivo: El nodo base no tuvo cambios (UNCHANGED).
        Las interfaces sí tuvieron cambios (any_changes=True), PERO hubieron errores (iface_errors=2).
        Verifica que el error prevenga que el nodo se escale engañosamente a UPDATED.
        """
        mock_process_node.return_value = (
            (SyncStatus.UNCHANGED, 123, MagicMock()),
            MagicMock(),
        )
        mock_parse_interfaces.return_value = [{"name": "eth0"}]
        # Devuelve iface_errors=2, any_changes=True
        mock_process_interfaces.return_value = (2, True)

        counts = {
            SyncStatus.CREATED: 0,
            SyncStatus.UPDATED: 0,
            SyncStatus.UNCHANGED: 0,
            SyncStatus.SKIPPED: 0,
            SyncStatus.ERROR: 0,
        }

        counts, _ = _sync_row(
            row_num=2,
            row={},
            node_type=NodeType.DEVICE,
            endpoints=MagicMock(),
            config=MagicMock(),
            site=MagicMock(),
            cluster_type_map={},
            device_type_attrs={},
            fallback_cluster_type=MagicMock(),
            caches=MagicMock(),
            csv_counts=_counts(),
            counts=counts,
            args=MagicMock(dry_run=False, prune_network_orphans=False),
        )

        # El status UNCHANGED debe incrementar (porque no se escaló a UPDATED)
        assert counts[SyncStatus.UNCHANGED] == 1
        assert counts[SyncStatus.UPDATED] == 0
        # Los errores de interfaz deben sumarse
        assert counts[SyncStatus.ERROR] == 2

    @patch("export_to_netbox.extract_csv_value")
    @patch("export_to_netbox._process_node_sync")
    @patch("export_to_netbox.parse_row_interfaces")
    @patch("export_to_netbox.process_interfaces_and_ips")
    def test_sync_row_escalation_success(
        self,
        mock_process_interfaces: MagicMock,
        mock_parse_interfaces: MagicMock,
        mock_process_node: MagicMock,
        mock_extract_csv_value: MagicMock,
    ) -> None:
        """
        Happy path de escalación: El nodo base es UNCHANGED, hay cambios en interfaces,
        y NO hay errores (iface_errors=0). Se debe descontar UNCHANGED y sumar UPDATED.
        """
        mock_process_node.return_value = (
            (SyncStatus.UNCHANGED, 123, MagicMock()),
            MagicMock(),
        )
        mock_parse_interfaces.return_value = [{"name": "eth0"}]
        # Devuelve iface_errors=0, any_changes=True
        mock_process_interfaces.return_value = (0, True)

        counts = {
            SyncStatus.CREATED: 0,
            SyncStatus.UPDATED: 0,
            SyncStatus.UNCHANGED: 0,
            SyncStatus.SKIPPED: 0,
            SyncStatus.ERROR: 0,
        }

        counts, _ = _sync_row(
            row_num=2,
            row={},
            node_type=NodeType.DEVICE,
            endpoints=MagicMock(),
            config=MagicMock(),
            site=MagicMock(),
            cluster_type_map={},
            device_type_attrs={},
            fallback_cluster_type=MagicMock(),
            caches=MagicMock(),
            csv_counts=_counts(),
            counts=counts,
            args=MagicMock(dry_run=False, prune_network_orphans=False),
        )

        assert counts[SyncStatus.UNCHANGED] == 0
        assert counts[SyncStatus.UPDATED] == 1
        assert counts[SyncStatus.ERROR] == 0


class TestNetBoxMappingConfig:
    """QA-Tester: Valida el comportamiento de mapeo normalizado y fall-through."""

    @pytest.fixture
    def config(self) -> NetBoxMappingConfig:
        return NetBoxMappingConfig(
            csv_columns={
                "status": CsvColumnDef(
                    source="Estado",
                    map={
                        "activo": "active",
                        "en baja": "decommissioning",
                    },
                ),
                "desc": CsvColumnDef(source="Descripcion"),
                "machine_type": CsvColumnDef(source="Tipo", map={"vm": "vm"}),
            },
            node_types=NodeTypesConfig(
                device=NodeMappingConfig(
                    status=StatusDefaultConfig(default="inventory"),
                    native_mappings=[],
                ),
                virtual_machine=NodeMappingConfig(
                    status=StatusDefaultConfig(default="inventory"),
                    native_mappings=[],
                ),
            ),
            custom_field_definitions=[],
            site=SiteConfig(name="Site", slug="site"),
            cluster_type=ClusterTypeConfig(
                default=ClusterTypeDefaultConfig(name="Cluster", slug="cluster")
            ),
            device_roles=[
                DeviceRoleConfig(name="Others", slug="others", color="123456")
            ],
        )

    def test_apply_map_exact_match(self, config: NetBoxMappingConfig) -> None:
        assert config.map_value("status", "activo") == "active"

    def test_apply_map_case_insensitive_and_spaces(
        self, config: NetBoxMappingConfig
    ) -> None:
        """Simula un edge case común en CSV: mayúsculas y espacios basura."""
        assert config.map_value("status", "  ACTIVO  ") == "active"
        assert config.map_value("status", "En Baja") == "decommissioning"

    def test_apply_map_fall_through(self, config: NetBoxMappingConfig) -> None:
        """Si el valor ingresado ya es un valor destino válido, debe pasar directo (fall-through)."""
        assert config.map_value("status", "active") == "active"
        assert config.map_value("status", "  DECOMMISSIONING  ") == "decommissioning"

    def test_apply_map_no_map_defined(self, config: NetBoxMappingConfig) -> None:
        """Si la columna no tiene map configurado, retorna el valor crudo."""
        assert config.map_value("desc", "Cualquier cosa") == "Cualquier cosa"

    def test_apply_map_strict_violation(self, config: NetBoxMappingConfig) -> None:
        """Si no coincide ni en llaves ni en valores destino, y strict=True, explota."""
        with pytest.raises(RowValidationError, match="no está definido en el mapa"):
            config.map_value("status", "desconocido")

    def test_apply_map_non_strict(self, config: NetBoxMappingConfig) -> None:
        """Si strict=False, valores desconocidos retornan None."""
        assert config.map_value("status", "desconocido", strict=False) is None


# ============================================================
# DETECCIÓN DE RENOMBRES DE INTERFACES
# ============================================================


def _fp(ips: tuple[str, ...] = (), macs: tuple[str, ...] = ()) -> InterfaceFingerprint:
    """Construye una huella de interfaz con IPs (host) y MACs."""
    return InterfaceFingerprint(frozenset(ips), frozenset(macs))


class TestDetectInterfaceRenames:
    """Núcleo puro: empareja interfaces huérfanas y nuevas por IP/MAC únicas y 1:1."""

    def test_rename_by_ip(self) -> None:
        renames = detect_interface_renames(
            {"ens18": _fp(ips=("10.0.0.5",))}, {"eth0": _fp(ips=("10.0.0.5",))}
        )
        assert renames == [InterfaceRename("ens18", "eth0", "IP 10.0.0.5")]

    def test_rename_by_mac(self) -> None:
        renames = detect_interface_renames(
            {"ens18": _fp(macs=("AA:BB:CC:DD:EE:FF",))},
            {"eth0": _fp(macs=("AA:BB:CC:DD:EE:FF",))},
        )
        assert renames == [InterfaceRename("ens18", "eth0", "MAC AA:BB:CC:DD:EE:FF")]

    def test_consistent_ip_and_mac_are_both_reported(self) -> None:
        signals = _fp(ips=("10.0.0.5",), macs=("AA:BB:CC:DD:EE:FF",))
        renames = detect_interface_renames({"ens18": signals}, {"eth0": signals})
        assert renames == [
            InterfaceRename("ens18", "eth0", "IP 10.0.0.5, MAC AA:BB:CC:DD:EE:FF")
        ]

    def test_contradicting_signals_do_not_rename(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """La IP apunta a una interfaz nueva y la MAC a otra: no se adivina."""
        with caplog.at_level(logging.WARNING):
            renames = detect_interface_renames(
                {"ens18": _fp(ips=("10.0.0.5",), macs=("AA:AA:AA:AA:AA:AA",))},
                {
                    "eth0": _fp(ips=("10.0.0.5",)),
                    "eth1": _fp(macs=("AA:AA:AA:AA:AA:AA",)),
                },
            )
        assert renames == []
        assert caplog.text.count("Renombre ambiguo") == 1  # una vez, no por pareja
        assert "'ens18' coincide con ['eth0', 'eth1']" in caplog.text

    def test_two_orphans_claiming_one_newcomer_do_not_rename(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            renames = detect_interface_renames(
                {
                    "ens18": _fp(ips=("10.0.0.5",)),
                    "ens19": _fp(macs=("AA:AA:AA:AA:AA:AA",)),
                },
                {"eth0": _fp(ips=("10.0.0.5",), macs=("AA:AA:AA:AA:AA:AA",))},
            )
        assert renames == []
        assert "la interfaz nueva 'eth0' coincide con ['ens18', 'ens19']" in caplog.text

    def test_mac_shared_by_bond_is_ignored_and_ip_decides(self) -> None:
        """bond0 y su miembro comparten MAC: la MAC no identifica; decide la IP."""
        shared_mac = "AA:BB:CC:DD:EE:FF"
        renames = detect_interface_renames(
            {"bond-old": _fp(ips=("10.0.0.5",), macs=(shared_mac,))},
            {
                "bond0": _fp(ips=("10.0.0.5",), macs=(shared_mac,)),
                "eth0": _fp(macs=(shared_mac,)),
            },
        )
        assert renames == [InterfaceRename("bond-old", "bond0", "IP 10.0.0.5")]

    def test_two_renames_in_the_same_node(self) -> None:
        renames = detect_interface_renames(
            {"ens18": _fp(ips=("10.0.0.5",)), "ens19": _fp(ips=("10.0.1.5",))},
            {"eth1": _fp(ips=("10.0.1.5",)), "eth0": _fp(ips=("10.0.0.5",))},
        )
        assert renames == [
            InterfaceRename("ens18", "eth0", "IP 10.0.0.5"),
            InterfaceRename("ens19", "eth1", "IP 10.0.1.5"),
        ]

    def test_interfaces_without_signals_are_not_renamed(self) -> None:
        assert detect_interface_renames({"ens18": _fp()}, {"eth0": _fp()}) == []


class TestCsvInterfaceFingerprint:
    """Identidad de red de una interfaz del CSV (IP host y MAC normalizada)."""

    @staticmethod
    def _iface(cidr: str | None, mac: str | None) -> NetworkInterfaceData:
        return cast(
            NetworkInterfaceData,
            {
                "name": "eth0",
                "enabled": True,
                "mac": mac,
                "ip": None,
                "prefix": None,
                "cidr": cidr,
                "network": None,
            },
        )

    def test_ip_without_mask_and_uppercase_mac(self) -> None:
        fingerprint = _csv_interface_fingerprint(
            self._iface("10.0.0.5/24", "aa:bb:cc:dd:ee:ff")
        )
        assert fingerprint == _fp(ips=("10.0.0.5",), macs=("AA:BB:CC:DD:EE:FF",))

    @pytest.mark.parametrize("cidr", ["10.0.0.5/24", "10.0.0.5/26", "10.0.0.5/32"])
    def test_mask_change_keeps_identity(self, cidr: str) -> None:
        assert _csv_interface_fingerprint(self._iface(cidr, None)).ip_hosts == {
            "10.0.0.5"
        }

    def test_missing_ip_and_mac_give_empty_fingerprint(self) -> None:
        assert _csv_interface_fingerprint(self._iface(None, None)) == _fp()


class TestRekeyInterfacesCache:
    def test_moves_only_renamed_keys_without_mutating_input(self) -> None:
        old, kept = MockNetBoxRecord(id=1), MockNetBoxRecord(id=2)
        cache: NameCache = {"ens18": old, "lo": kept}

        result = rekey_interfaces_cache(
            cache, [InterfaceRename("ens18", "eth0", "IP 10.0.0.5")]
        )

        assert result == {"eth0": old, "lo": kept}
        assert cache == {"ens18": old, "lo": kept}


class TestResolveInterfaceRenamesQueries:
    """Las huellas de NetBox solo se consultan cuando puede haber un renombre."""

    @staticmethod
    def _iface(name: str, cidr: str | None = None) -> NetworkInterfaceData:
        network = None
        return cast(
            NetworkInterfaceData,
            {
                "name": name,
                "enabled": True,
                "mac": None,
                "ip": None,
                "prefix": None,
                "cidr": cidr,
                "network": network,
            },
        )

    @pytest.mark.parametrize(
        ("obj_id", "csv_names", "cache_names"),
        [
            (0, ["eth0"], ["ens18"]),  # nodo nuevo: no hay interfaces en NetBox
            (7, ["eth0"], ["eth0"]),  # sin huérfanas ni nuevas
            (7, ["eth0", "eth1"], ["eth0"]),  # solo nuevas
            (7, ["eth0"], ["eth0", "ens18"]),  # solo huérfanas
        ],
    )
    def test_no_queries_without_orphans_and_newcomers(
        self, obj_id: int, csv_names: list[str], cache_names: list[str]
    ) -> None:
        endpoints = MagicMock()
        cache: NameCache = {
            n: MockNetBoxRecord(id=i + 1) for i, n in enumerate(cache_names)
        }

        renames = _resolve_interface_renames(
            endpoints,
            NodeType.DEVICE,
            obj_id,
            [self._iface(n, "10.0.0.5/24") for n in csv_names],
            cache,
        )

        assert renames == []
        endpoints.ip_addresses.filter.assert_not_called()
        endpoints.mac_addresses.filter.assert_not_called()


class _FakeRecord:
    """Objeto NetBox en memoria: update()/delete() modifican el almacén compartido."""

    def __init__(self, store: "_FakeNetBox", kind: str, **attrs: Any) -> None:
        self._store = store
        self._kind = kind
        for key, value in attrs.items():
            setattr(self, key, value)

    def __getattr__(self, name: str) -> Any:
        """Atributos dinámicos (como en pynetbox); los inexistentes siguen fallando."""
        raise AttributeError(name)

    def update(self, data: dict[str, Any]) -> bool:
        self._store.writes.append(("update", self._kind, dict(data)))
        for key, value in data.items():
            setattr(self, key, value)
        return True

    def delete(self) -> bool:
        self._store.writes.append(("delete", self._kind, {"id": self.id}))
        self._store.records[self._kind].remove(self)
        return True


class _FakeEndpoint:
    """Endpoint en memoria con los filtros que usa la sincronización de interfaces."""

    def __init__(self, store: "_FakeNetBox", kind: str, url: str) -> None:
        self._store = store
        self._kind = kind
        self.name = kind
        self.url = url

    def _matches(self, record: _FakeRecord, key: str, value: Any) -> bool:
        if key == "device_id" and self._kind == "interfaces":
            return record.device.id == value
        if key == "device_id":  # IPs/MACs: asignadas a interfaces del Device
            return self._store.interface_device_id(record) == value
        return getattr(record, key, None) == value

    def filter(self, **kwargs: Any) -> list[_FakeRecord]:
        return [
            r
            for r in self._store.records[self._kind]
            if all(self._matches(r, k, v) for k, v in kwargs.items())
        ]

    def create(self, **payload: Any) -> _FakeRecord:
        self._store.writes.append(("create", self._kind, dict(payload)))
        if self._kind == "interfaces":
            payload["device"] = SimpleNamespace(id=payload["device"])
        record = _FakeRecord(
            self._store, self._kind, id=self._store.next_id(), **payload
        )
        self._store.records[self._kind].append(record)
        return record


class _FakeNetBox:
    """NetBox mínimo en memoria: interfaces, IPs, MACs y prefixes de un Device."""

    def __init__(self) -> None:
        self.records: dict[str, list[_FakeRecord]] = {
            k: [] for k in ("interfaces", "ips", "macs", "prefixes")
        }
        self.writes: list[tuple[str, str, dict[str, Any]]] = []
        self._ids = 100
        self.endpoints = cast(
            NetBoxEndpoints,
            SimpleNamespace(
                device_interfaces=_FakeEndpoint(
                    self, "interfaces", "http://nb/api/dcim/interfaces/"
                ),
                ip_addresses=_FakeEndpoint(
                    self, "ips", "http://nb/api/ipam/ip-addresses/"
                ),
                mac_addresses=_FakeEndpoint(
                    self, "macs", "http://nb/api/dcim/mac-addresses/"
                ),
                prefixes=_FakeEndpoint(
                    self, "prefixes", "http://nb/api/ipam/prefixes/"
                ),
            ),
        )

    def next_id(self) -> int:
        self._ids += 1
        return self._ids

    def interface_device_id(self, record: _FakeRecord) -> int | None:
        iface_id = getattr(record, "assigned_object_id", None)
        for iface in self.records["interfaces"]:
            if iface.id == iface_id:
                return iface.device.id
        return None

    def add_interface(
        self, device_id: int, name: str, cidr: str | None, mac: str | None
    ) -> _FakeRecord:
        iface = _FakeRecord(
            self,
            "interfaces",
            id=self.next_id(),
            name=name,
            enabled=True,
            type="other",
            device=SimpleNamespace(id=device_id),
        )
        self.records["interfaces"].append(iface)
        assignment = {
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": iface.id,
        }
        if cidr:
            self.records["ips"].append(
                _FakeRecord(self, "ips", id=self.next_id(), address=cidr, **assignment)
            )
        if mac:
            self.records["macs"].append(
                _FakeRecord(
                    self, "macs", id=self.next_id(), mac_address=mac, **assignment
                )
            )
        return iface


class TestInterfaceRenameEndToEnd:
    """Escenario completo: el maestro renombra ens18 → eth0 conservando IP y MAC."""

    DEVICE_ID = 7
    CIDR = "10.0.0.5/24"
    MAC = "AA:BB:CC:DD:EE:FF"

    def _csv_iface(
        self, name: str, cidr: str | None = None, mac: str | None = None
    ) -> NetworkInterfaceData:
        return cast(
            NetworkInterfaceData,
            {
                "name": name,
                "enabled": True,
                "mac": mac,
                "ip": cidr.split("/")[0] if cidr else None,
                "prefix": None,
                "cidr": cidr,
                "network": "10.0.0.0/24" if cidr else None,
            },
        )

    @staticmethod
    def _args(dry_run: bool = False, prune: bool = True) -> argparse.Namespace:
        return argparse.Namespace(dry_run=dry_run, prune_network_orphans=prune)

    def _sync(
        self, nb: _FakeNetBox, interfaces: list[NetworkInterfaceData], **flags: bool
    ) -> Any:
        return _sync_interfaces_for_object(
            nb.endpoints,
            self.DEVICE_ID,
            NodeType.DEVICE,
            interfaces,
            self._args(**flags),
        )

    def test_rename_keeps_object_ip_and_mac_and_is_idempotent(self) -> None:
        nb = _FakeNetBox()
        iface = nb.add_interface(self.DEVICE_ID, "ens18", self.CIDR, self.MAC)
        nb.records["prefixes"].append(
            _FakeRecord(nb, "prefixes", id=nb.next_id(), prefix="10.0.0.0/24")
        )
        csv = [self._csv_iface("eth0", self.CIDR, self.MAC)]

        result = self._sync(nb, csv)

        assert result.errors == 0 and result.any_changes is True
        assert iface.name == "eth0"  # mismo objeto, renombrado
        assert len(nb.records["interfaces"]) == 1  # no se creó ni se podó nada
        assert [w for w in nb.writes if w[0] != "update"] == []
        assert len(nb.records["ips"]) == 1 and len(nb.records["macs"]) == 1
        ip_record = nb.records["ips"][0]
        assert ip_record.assigned_object_id == iface.id

        nb.writes.clear()
        second = self._sync(nb, csv)
        assert second.errors == 0 and second.any_changes is False
        assert nb.writes == []

    def test_dry_run_reports_without_writing(self) -> None:
        nb = _FakeNetBox()
        iface = nb.add_interface(self.DEVICE_ID, "ens18", self.CIDR, self.MAC)
        nb.records["prefixes"].append(
            _FakeRecord(nb, "prefixes", id=nb.next_id(), prefix="10.0.0.0/24")
        )

        result = self._sync(
            nb, [self._csv_iface("eth0", self.CIDR, self.MAC)], dry_run=True
        )

        assert result.any_changes is True
        assert nb.writes == []
        assert iface.name == "ens18"

    def test_unrelated_new_interface_keeps_previous_behaviour(self) -> None:
        """Sin señales compartidas no hay renombre: se crea la nueva y se poda la vieja."""
        nb = _FakeNetBox()
        nb.add_interface(self.DEVICE_ID, "ens18", "10.0.9.9/24", "11:11:11:11:11:11")

        self._sync(nb, [self._csv_iface("eth0")])

        assert [i.name for i in nb.records["interfaces"]] == ["eth0"]
        assert ("delete", "interfaces") in [(w[0], w[1]) for w in nb.writes]


# ============================================================
# PODA: IPs CON ROL ASIGNADO (curadas a mano en NetBox)
# ============================================================


class TestIpRoleProtection:
    """Las IPs con rol nunca se desasignan; sin rol, la poda sigue igual."""

    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            (None, None),
            (SimpleNamespace(value="secondary", label="Secondary"), "secondary"),
            ("vip", "vip"),  # string plano
            (SimpleNamespace(value=None), None),
        ],
    )
    def test_get_ip_role(self, role: Any, expected: str | None) -> None:
        assert _get_ip_role(MockNetBoxRecord.model_validate({"role": role})) == expected

    def test_ip_with_role_is_kept_and_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        secondary = MagicMock(
            address="10.0.0.6/24", role=SimpleNamespace(value="secondary")
        )
        plain_orphan = MagicMock(address="10.0.0.7/24", role=None)
        endpoint = MagicMock()
        endpoint.filter.return_value = [secondary, plain_orphan]

        with caplog.at_level(logging.INFO):
            pruned, errors = _prune_orphan_ips(
                endpoint, "dcim.interface", 101, {"10.0.0.5/24"}, dry_run=False
            )

        assert (pruned, errors) == (1, 0)
        secondary.update.assert_not_called()
        plain_orphan.update.assert_called_once()
        assert "Se conserva la IP '10.0.0.6/24' (rol 'secondary')" in caplog.text

    def test_dry_run_also_respects_protection(self) -> None:
        vip = MagicMock(address="10.0.0.6/24", role=SimpleNamespace(value="vip"))
        endpoint = MagicMock()
        endpoint.filter.return_value = [vip]

        assert _prune_orphan_ips(endpoint, "dcim.interface", 101, set(), True) == (0, 0)
        vip.update.assert_not_called()


class TestIpRoleProtectionEndToEnd:
    """Con --prune-network-orphans, sobre un NetBox en memoria."""

    DEVICE_ID = 7

    @staticmethod
    def _add_ip(
        nb: _FakeNetBox, iface: _FakeRecord, cidr: str, role: str | None
    ) -> None:
        nb.records["ips"].append(
            _FakeRecord(
                nb,
                "ips",
                id=nb.next_id(),
                address=cidr,
                role=SimpleNamespace(value=role) if role else None,
                assigned_object_type="dcim.interface",
                assigned_object_id=iface.id,
            )
        )

    @staticmethod
    def _csv_iface(name: str, cidr: str) -> NetworkInterfaceData:
        return cast(
            NetworkInterfaceData,
            {
                "name": name,
                "enabled": True,
                "mac": None,
                "ip": cidr.split("/")[0],
                "prefix": None,
                "cidr": cidr,
                "network": None,
            },
        )

    def _sync(self, nb: _FakeNetBox, interfaces: list[NetworkInterfaceData]) -> Any:
        args = argparse.Namespace(dry_run=False, prune_network_orphans=True)
        return _sync_interfaces_for_object(
            nb.endpoints, self.DEVICE_ID, NodeType.DEVICE, interfaces, args
        )

    def test_secondary_ip_survives_while_plain_orphan_ip_is_unassigned(self) -> None:
        nb = _FakeNetBox()
        eth0 = nb.add_interface(self.DEVICE_ID, "eth0", "10.0.0.5/24", None)
        self._add_ip(nb, eth0, "10.0.0.6/24", "secondary")
        self._add_ip(nb, eth0, "10.0.0.7/24", None)

        result = self._sync(nb, [self._csv_iface("eth0", "10.0.0.5/24")])

        assigned = {ip.address: ip.assigned_object_id for ip in nb.records["ips"]}
        assert result.errors == 0
        assert assigned["10.0.0.6/24"] == eth0.id  # protegida
        assert assigned["10.0.0.7/24"] is None  # huérfana sin rol: desasignada

    def test_orphan_interface_hosting_a_protected_ip_is_not_deleted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        nb = _FakeNetBox()
        nb.add_interface(self.DEVICE_ID, "eth0", "10.0.0.5/24", None)
        old = nb.add_interface(self.DEVICE_ID, "eth9", None, None)
        self._add_ip(nb, old, "10.0.9.9/24", "vrrp")

        with caplog.at_level(logging.WARNING):
            self._sync(nb, [self._csv_iface("eth0", "10.0.0.5/24")])

        assert "eth9" in [iface.name for iface in nb.records["interfaces"]]
        assert ("delete", "interfaces") not in [(w[0], w[1]) for w in nb.writes]
        assert "Se conserva la interfaz huérfana 'eth9'" in caplog.text

    def test_orphan_interface_without_protected_ips_is_still_deleted(self) -> None:
        nb = _FakeNetBox()
        nb.add_interface(self.DEVICE_ID, "eth0", "10.0.0.5/24", None)
        nb.add_interface(self.DEVICE_ID, "eth9", "10.0.9.9/24", None)

        self._sync(nb, [self._csv_iface("eth0", "10.0.0.5/24")])

        assert [iface.name for iface in nb.records["interfaces"]] == ["eth0"]


class TestKeepOrphanInterfaceFailSafe:
    def test_unverifiable_interface_is_kept(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Si NetBox falla al consultar sus IPs, no se borra (borrar es irreversible)."""
        endpoints = MagicMock()
        endpoints.ip_addresses.filter.side_effect = RequestError(
            MagicMock(status_code=500)
        )
        iface = MagicMock(id=102)

        with caplog.at_level(logging.WARNING):
            metrics = _prune_network_orphans(
                endpoints, NodeType.DEVICE, {"eth9": iface}, set(), set(), set(), False
            )

        iface.delete.assert_not_called()
        assert metrics.pruned_interfaces == 0
        assert "no se pudo verificar" in caplog.text


# ============================================================
# SINCRONIZACIÓN EN TRES FASES: validar → identidad → escribir
# ============================================================


class TestNoWritesBeforeRowIsAccepted:
    """
    Una fila que termina en SKIP (identidad) o en ERROR (datos inválidos) no debe dejar
    dependencias escritas en NetBox: Platform, Location, Rack, Cluster, Manufacturer
    ni DeviceType.
    """

    DEPENDENCY_ENDPOINTS = (
        "platforms",
        "locations",
        "racks",
        "clusters",
        "manufacturers",
        "device_types",
    )

    @staticmethod
    def _row(config: NetBoxMappingConfig, **overrides: str) -> CsvRow:
        cols = {k: v.source for k, v in config.csv_columns.items()}
        values = {
            "machine_name": "srv-01",
            "machine_type": "Dedicada",
            "inventory_uuid": "",
            "os": "Ubuntu",
            "manufacturer": "Dell",
            "model": "R640",
            "rack": "Rack-A1",
            "rack_location": "Fila A",
            "cluster_name": "Cluster-1",
        } | overrides
        return {cols[alias]: value for alias, value in values.items()}

    @staticmethod
    def _endpoints(nodes_by_name: list[Any]) -> MagicMock:
        """Endpoints sin dependencias existentes; `filter` de nodos devuelve `nodes_by_name`."""
        endpoints = MagicMock()
        for endpoint in (endpoints.devices, endpoints.virtual_machines):
            endpoint.url = "http://nb/api/dcim/devices/"
            endpoint.filter.side_effect = lambda **kw: (
                [] if "cf_inventory_uuid" in kw else nodes_by_name
            )
        endpoints.virtual_machines.url = (
            "http://nb/api/virtualization/virtual-machines/"
        )
        for name in TestNoWritesBeforeRowIsAccepted.DEPENDENCY_ENDPOINTS:
            getattr(endpoints, name).filter.return_value = []
        return endpoints

    def _assert_no_dependency_writes(self, endpoints: MagicMock) -> None:
        for name in self.DEPENDENCY_ENDPOINTS:
            endpoint = getattr(endpoints, name)
            assert not endpoint.create.called, f"se creó un objeto en '{name}'"

    def _sync_device(
        self, endpoints: MagicMock, row: CsvRow, config: NetBoxMappingConfig
    ) -> SyncResult:
        caches = CacheStore(device_roles={"others": MockNetBoxRecord(id=9)})
        result, _ = sync_device(
            endpoints,
            row,
            config,
            MockNetBoxRecord(id=SITE_ID),
            {},
            {},
            MockNetBoxRecord(id=3),
            caches,
            _counts({"srv-01": 1}),
            False,
        )
        return result

    def test_device_skipped_by_ambiguous_name_writes_nothing(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoints = self._endpoints([_node(1, ""), _node(2, "")])  # nombre ambiguo

        with pytest.raises(RowSkipCondition):
            self._sync_device(endpoints, self._row(config), config)

        self._assert_no_dependency_writes(endpoints)
        endpoints.devices.create.assert_not_called()

    def test_vm_skipped_by_uuid_of_other_site_writes_nothing(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoints = self._endpoints([])
        foreign = MockNetBoxRecord.model_validate(
            {
                "id": 4,
                "custom_fields": {"inventory_uuid": "uuid-1"},
                "site": SimpleNamespace(id=SITE_ID + 1),
            }
        )
        endpoints.virtual_machines.filter.side_effect = lambda **kw: (
            [foreign] if "cf_inventory_uuid" in kw else []
        )
        caches = CacheStore(device_roles={"others": MockNetBoxRecord(id=9)})
        row = self._row(config, machine_type="VM", inventory_uuid="uuid-1")

        with pytest.raises(RowSkipCondition, match="otro Site"):
            sync_vm(
                endpoints,
                row,
                config,
                MockNetBoxRecord(id=SITE_ID),
                {},
                MockNetBoxRecord(id=3),
                caches,
                _counts({"srv-01": 1}, {"uuid-1": 1}),
                False,
            )

        self._assert_no_dependency_writes(endpoints)

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"status": "Inexistente"}, "Estado|status"),
            ({"height_u": "1.25"}, "height_u"),
        ],
    )
    def test_invalid_row_data_writes_nothing(
        self, config: NetBoxMappingConfig, overrides: dict[str, str], match: str
    ) -> None:
        endpoints = self._endpoints([])

        with pytest.raises(RowValidationError, match=match):
            self._sync_device(endpoints, self._row(config, **overrides), config)

        self._assert_no_dependency_writes(endpoints)
        endpoints.devices.create.assert_not_called()

    def test_valid_row_still_creates_dependencies_and_node(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoints = self._endpoints([])
        endpoints.devices.create.return_value = MockNetBoxRecord(id=50)
        for name in self.DEPENDENCY_ENDPOINTS:
            getattr(endpoints, name).create.return_value = MagicMock(id=60)

        result = self._sync_device(endpoints, self._row(config), config)

        assert result.status == SyncStatus.CREATED
        for name in (
            "platforms",
            "locations",
            "racks",
            "manufacturers",
            "device_types",
        ):
            assert getattr(endpoints, name).create.called, name
        payload = endpoints.devices.create.call_args.kwargs
        assert {"platform", "rack", "device_type", "role", "status", "site"} <= set(
            payload
        )
