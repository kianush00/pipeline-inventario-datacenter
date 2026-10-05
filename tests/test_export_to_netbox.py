"""
Pruebas unitarias para export_to_netbox.py
"""

import hashlib
from collections import Counter
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from pynetbox.core.query import RequestError  # type: ignore

import export_to_netbox
from export_to_netbox import (
    CastType,
    ChoiceItemConfig,
    ChoiceSetConfig,
    ConfigValidationError,
    CustomFieldConfig,
    DeviceRoleConfig,
    Endpoint,
    FieldMappingConfig,
    FieldParseError,
    MockNetBoxRecord,
    NameCache,
    NetBoxApiError,
    NetBoxEndpoints,
    NetBoxMappingConfig,
    NetBoxObject,
    NetworkInterfaceData,
    NodeType,
    RowSkipCondition,
    RowValidationError,
    SiteConfig,
    SiteNameCache,
    SyncResult,
    SyncStatus,
    ValidationError,
    _assign_primary_ipv4,
    _build_interface_cidr,
    _classify_rows,
    _create_with_fallback_slug,
    _execute_sync,
    _extract_concat_dot_value,
    _extract_raw_source_value,
    _find_existing_object,
    _is_collision_error,
    _is_custom_field_changed,
    _is_name_safely_unique,
    _is_relation_changed,
    _parse_network_interfaces,
    _parse_single_network_interface,
    _prune_orphan_interfaces,
    _resolve_cluster,
    _resolve_default_or_empty,
    _resolve_device_role,
    _resolve_device_type,
    _resolve_field_value,
    _resolve_netbox_status,
    _sanitize_mac_address,
    _search_in_netbox,
    _sync_device_type_u_height,
    _sync_row,
    _sync_single_device_role,
    _sync_single_interface,
    _validate_csv_headers,
    _validate_interface_ip,
    _validate_select_choice,
    apply_cast,
    build_payload,
    check_record_changes,
    concat_dot,
    count_machine_names,
    ensure_cluster,
    ensure_device_type,
    ensure_dynamic_cluster_types,
    ensure_location,
    ensure_manufacturer,
    ensure_rack,
    ensure_site,
    extract_csv_value,
    generate_fallback_slug,
    get_netbox_object_id,
    get_node_type_from_row,
    get_or_create_cached,
    load_config,
    parse_bool_si_no,
    parse_float,
    parse_float_to_int,
    parse_int,
    parse_int_gb_to_mb,
    precompute_cluster_type_map,
    process_interfaces_and_ips,
    slugify,
    sync_device,
    sync_vm,
)


class TestSlugify:
    """Verifica la generación de slugs válidos para NetBox."""

    def test_simple_name(self) -> None:
        """Un nombre limpio se convierte a minúsculas sin alteraciones."""
        assert slugify("Produccion") == "produccion"

    def test_spaces_and_dashes(self) -> None:
        """Espacios, guiones bajos y guiones múltiples se unifican en '-'."""
        assert slugify("Data  Center__Principal--Rack") == "data-center-principal-rack"

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
        """Guiones al inicio y final del slug se eliminan."""
        assert slugify("--nombre--") == "nombre"

    def test_preserves_accents(self) -> None:
        """Caracteres acentuados (válidos en NetBox) se preservan."""
        assert slugify("Producción") == "producción"

    def test_slugify_accents_and_symbols(self) -> None:
        assert slugify("  --H.P.!!__  ") == "hp"


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
            FieldMappingConfig(source="Tipo de maquina", target="machine_type"),
        ]
        row = {"Nombre": "SRV", "vCPUs": "4", "Tipo de maquina": "Dedicada"}

        payload = build_payload(row, native_map, custom_map, config)

        assert payload["name"] == "SRV"
        assert "custom_fields" in payload
        assert payload["custom_fields"]["cpu_cores"] == "4"
        assert payload["custom_fields"]["machine_type"] == "dedicated"


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


class TestFindExistingObject:
    """Verifica la lógica de búsqueda por UUID y Nombre."""

    def test_found_by_uuid(self, config: NetBoxMappingConfig) -> None:
        endpoint = MagicMock()
        mock_record = MockNetBoxRecord(id=5)
        endpoint.filter.return_value = [mock_record]

        existing, by_uuid, by_name = _find_existing_object(
            "uuid-123", "SRV-01", endpoint, config
        )

        assert existing == [mock_record]
        assert by_uuid is True
        assert by_name is False
        endpoint.filter.assert_called_once_with(cf_inventory_uuid="uuid-123")

    def test_found_by_name_when_uuid_not_found(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoint = MagicMock()
        # Primer llamado (UUID) retorna vacío, segundo llamado (nombre) retorna registro
        endpoint.filter.side_effect = [[], [MockNetBoxRecord(id=6)]]

        existing, by_uuid, by_name = _find_existing_object(
            "uuid-123", "SRV-01", endpoint, config
        )

        assert len(existing) == 1
        assert existing[0].id == 6
        assert by_uuid is False
        assert by_name is True
        assert endpoint.filter.call_count == 2

    def test_found_by_name_but_uuid_matches_case_insensitive(
        self, config: NetBoxMappingConfig
    ) -> None:
        endpoint = MagicMock()
        mock_record = MockNetBoxRecord(
            id=7, custom_fields={"inventory_uuid": "UUID-123-ABC"}
        )

        # Primer llamado (UUID en minúsculas) retorna vacío
        # Segundo llamado (nombre) retorna el registro
        endpoint.filter.side_effect = [[], [mock_record]]

        # Buscamos con el UUID en minúsculas
        existing, by_uuid, by_name = _find_existing_object(
            "uuid-123-abc", "SRV-01", endpoint, config
        )

        assert len(existing) == 1
        assert existing[0].id == 7
        assert by_uuid is True
        assert by_name is False
        assert endpoint.filter.call_count == 2

    def test_not_found(self, config: NetBoxMappingConfig) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = []
        endpoint.name = "racks"

        existing, by_uuid, by_name = _find_existing_object(
            "uuid-123", "SRV-01", endpoint, config
        )

        assert existing == []
        assert by_uuid is False
        assert by_name is False

    def test_uuid_empty_searches_by_name(self, config: NetBoxMappingConfig) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = [MockNetBoxRecord(id=7)]

        existing, by_uuid, by_name = _find_existing_object(
            "", "SRV-01", endpoint, config
        )

        assert len(existing) == 1
        assert existing[0].id == 7
        assert by_uuid is False
        assert by_name is True
        endpoint.filter.assert_called_once_with(name="SRV-01")

    def test_multiple_matches_returns_first(self, config: NetBoxMappingConfig) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = [
            MockNetBoxRecord(id=1, name="SRV"),
            MockNetBoxRecord(id=2, name="SRV"),
        ]

        result, _, _ = _find_existing_object("", "SRV", endpoint, config)
        assert len(result) == 2
        assert result[0].id == 1


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
            NetBoxApiError, match="Error al actualizar NodeType.VIRTUAL_MACHINE 'SRV'"
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

    @patch("export_to_netbox.get_or_create_cached")
    def test_ensure_rack_updates_location_if_different(
        self, mock_get: MagicMock
    ) -> None:
        endpoint = MagicMock()
        site_mock = MockNetBoxRecord(id=1, name="Site1")
        cache: SiteNameCache = {}

        # Rack existente con location antigua (id=88)
        mock_old_loc = MockNetBoxRecord(id=88, name="Fila B")
        mock_rack = MagicMock(id=5, name="Rack1", location=mock_old_loc)

        mock_get.return_value = (mock_rack, cache)

        _, _ = ensure_rack(
            endpoint, "Rack1", site_mock, cache, dry_run=False, location_id=99
        )

        # Debe haber llamado update en el mock_rack para asignarle el 99
        mock_rack.update.assert_called_once_with({"location": 99})

    @patch("export_to_netbox.get_or_create_cached")
    def test_ensure_rack_does_not_update_if_location_matches(
        self, mock_get: MagicMock
    ) -> None:
        endpoint = MagicMock()
        site_mock = MockNetBoxRecord(id=1, name="Site1")
        cache: SiteNameCache = {}

        mock_loc = MockNetBoxRecord(id=99, name="Fila A")
        mock_rack = MagicMock(id=5, name="Rack1", location=mock_loc)

        mock_get.return_value = (mock_rack, cache)

        _, _ = ensure_rack(
            endpoint, "Rack1", site_mock, cache, dry_run=False, location_id=99
        )

        mock_rack.update.assert_not_called()


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


class TestSyncDeviceTypeUHeight:
    """Verifica la sincronización de u_height en DeviceTypes."""

    def test_sync_no_update_needed(self) -> None:
        existing = MagicMock()
        existing.u_height = 1.5
        # fractional target height matches
        result = _sync_device_type_u_height(existing, "Model A", 1.5, False)
        assert result is False
        existing.update.assert_not_called()

    def test_sync_update_needed(self) -> None:
        existing = MagicMock()
        existing.u_height = 1.0
        # update to fractional
        result = _sync_device_type_u_height(existing, "Model B", 1.5, False)
        assert result is True
        existing.update.assert_called_once_with({"u_height": 1.5})


class TestResolveDeviceTypeUHeight:
    """QA Tester verification para extracción de alturas fraccionarias."""

    def test_fractional_u_height(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Mock dependencias para aislar _resolve_device_type
        monkeypatch.setattr(
            export_to_netbox,
            "ensure_manufacturer",
            lambda *args, **kwargs: (MagicMock(), MagicMock()),
        )

        mock_ensure_device_type = MagicMock(return_value=(MagicMock(), MagicMock()))
        monkeypatch.setattr(
            export_to_netbox, "ensure_device_type", mock_ensure_device_type
        )

        def mock_extract(
            row: dict[str, Any], key: str, config: Any, *args: Any, **kwargs: Any
        ) -> Any:
            if key == "hei_u":
                return row.get(key)
            return "mocked"

        monkeypatch.setattr(export_to_netbox, "extract_csv_value", mock_extract)
        monkeypatch.setattr(export_to_netbox, "get_netbox_object_id", lambda x: 1)

        row = {"manufacturer": "Dell", "model": "R740", "hei_u": "1.5"}
        _resolve_device_type(
            endpoints=MagicMock(),
            row=row,
            manufacturer="Dell",
            model="R740",
            caches=MagicMock(),
            config=MagicMock(),
            dry_run=False,
        )

        # Verificar que ensure_device_type recibe el argumento u_height como un float=1.5
        mock_ensure_device_type.assert_called_once()
        args, _ = mock_ensure_device_type.call_args
        # ensure_device_type(device_types_endpoint, manufacturer, model, u_height, cache, dry_run)
        assert args[3] == 1.5
        assert isinstance(args[3], float)

    def test_empty_u_height_defaults_to_1_0(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            export_to_netbox,
            "ensure_manufacturer",
            lambda *args, **kwargs: (MagicMock(), MagicMock()),
        )
        mock_ensure_device_type = MagicMock(return_value=(MagicMock(), MagicMock()))
        monkeypatch.setattr(
            export_to_netbox, "ensure_device_type", mock_ensure_device_type
        )

        def mock_extract(
            row: dict[str, Any], key: str, config: Any, *args: Any, **kwargs: Any
        ) -> Any:
            if key == "hei_u":
                return ""  # Simulamos celda vacía
            return "mocked"

        monkeypatch.setattr(export_to_netbox, "extract_csv_value", mock_extract)
        monkeypatch.setattr(export_to_netbox, "get_netbox_object_id", lambda x: 1)

        row = {"hei_u": ""}
        _resolve_device_type(
            endpoints=MagicMock(),
            row=row,
            manufacturer="Dell",
            model="R740",
            caches=MagicMock(),
            config=MagicMock(),
            dry_run=False,
        )

        mock_ensure_device_type.assert_called_once()
        args, _ = mock_ensure_device_type.call_args
        assert args[3] == 1.0
        assert isinstance(args[3], float)


class TestPruneInterfaces:
    """Verifica la poda de interfaces huérfanas."""

    def test_pruning_removes_orphan(self) -> None:
        mock_iface1 = MagicMock()
        mock_iface1.name = "eth0"

        mock_iface2 = MagicMock()
        mock_iface2.name = "eth1"

        ifaces_cache: dict[str, Any] = {"eth0": mock_iface1, "eth1": mock_iface2}

        # El CSV solo reporta "eth0"
        csv_iface_names = {"eth0"}

        deleted, errors = _prune_orphan_interfaces(
            ifaces_cache=ifaces_cache,
            csv_iface_names=csv_iface_names,
            obj_id=10,
            dry_run=False,
        )

        assert deleted == 1
        assert errors == 0
        mock_iface1.delete.assert_not_called()
        mock_iface2.delete.assert_called_once()

    def test_pruning_dry_run_skips_delete(self) -> None:
        mock_iface = MagicMock()
        mock_iface.name = "eth1"
        ifaces_cache: dict[str, Any] = {"eth1": mock_iface}
        csv_iface_names = set()

        deleted, errors = _prune_orphan_interfaces(
            ifaces_cache=ifaces_cache,
            csv_iface_names=csv_iface_names,
            obj_id=10,
            dry_run=True,
        )

        assert deleted == 1
        assert errors == 0
        mock_iface.delete.assert_not_called()


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
                fallback_cluster_type=MagicMock(),
                caches=MagicMock(),
                csv_name_counts=Counter(),
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
                csv_name_counts=Counter(),
                dry_run=False,
            )

    @patch("export_to_netbox._resolve_cluster", return_value=(None, MagicMock()))
    @patch("export_to_netbox._resolve_base_node")
    def test_sync_vm_skips_unresolvable_cluster(
        self, mock_base_node: MagicMock, mock_resolve_cluster: MagicMock
    ) -> None:
        row = {"machine_name": "vm1", "cluster_name": "Cluster-X"}

        mock_config = MagicMock()
        mock_config.is_empty.return_value = False
        mock_config.csv_columns = {
            "rack_location": MagicMock(source="rack_location"),
            "cluster_name": MagicMock(source="cluster_name", map=None),
            "machine_name": MagicMock(source="machine_name", map=None),
            "inventory_uuid": MagicMock(source="inventory_uuid", map=None),
        }

        mock_base_node.return_value = (
            {
                "machine_name": "vm1",
                "inventory_uuid": "1234",
                "machine_type": "vm",
                "payload": {},
            },
            MagicMock(),
        )

        with pytest.raises(
            RowSkipCondition, match="Falló la resolución del cluster 'Cluster-X'"
        ):
            sync_vm(
                endpoints=MagicMock(),
                row=row,
                config=mock_config,
                site=MagicMock(),
                cluster_type_map={},
                fallback_cluster_type=MagicMock(),
                caches=MagicMock(),
                csv_name_counts=Counter(),
                dry_run=False,
            )


class TestSyncDevice:
    @patch("export_to_netbox._resolve_base_node")
    @patch("export_to_netbox._resolve_cluster")
    @patch("export_to_netbox.ensure_rack")
    @patch("export_to_netbox._resolve_device_type")
    @patch("export_to_netbox._validate_sync")
    def test_sync_device_assigns_front_face(
        self,
        mock_validate_sync: MagicMock,
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
        mock_base_node.return_value = (
            {
                "machine_name": "srv1",
                "inventory_uuid": "1234",
                "machine_type": "server",
                "payload": {"position": 10},  # NetBox API exige face si hay position
            },
            mock_caches,
        )
        mock_device_type.return_value = (1, mock_caches)
        mock_validate_sync.return_value = SyncResult(SyncStatus.CREATED, 1, MagicMock())

        _, returned_caches = sync_device(
            endpoints=MagicMock(),
            row=row,
            config=mock_config,
            site=MagicMock(),
            cluster_type_map={},
            fallback_cluster_type=MagicMock(),
            caches=mock_caches,
            csv_name_counts=Counter(),
            dry_run=False,
        )

        # Validar que payload haya sido inyectado con 'face' = 'front'
        payload_passed = mock_validate_sync.call_args[0][1]
        assert payload_passed["face"] == "front"
        assert payload_passed["position"] == 10
        assert returned_caches is mock_caches


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
        Escenario: Un DeviceRole ya existe en caché, pero tiene vm_role=False.
        _sync_single_device_role debe mutarlo in-place invocando .update(),
        y la caché debe retornar con el objeto correcto sin reasignaciones redundantes.
        """
        endpoints = MagicMock()
        role_def = DeviceRoleConfig(name="AccessSwitch", slug="access", color="000000")

        mock_obj = MagicMock(id=10, vm_role=False)
        cache_initial = cast(NameCache, {"accessswitch": mock_obj})
        mock_get_or_create.return_value = (mock_obj, cache_initial)

        returned_obj, returned_cache = _sync_single_device_role(
            endpoints, role_def, cache_initial, dry_run=False
        )

        mock_obj.update.assert_called_once_with({"vm_role": True})
        assert returned_obj is mock_obj
        assert returned_cache["accessswitch"] is mock_obj

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
            u_height=0.0,
            cache={},
            dry_run=False,
        )

        endpoint.create.assert_called_once()
        create_call_args = endpoint.create.call_args[1]
        assert create_call_args["u_height"] == 0.0

    def test_sync_device_type_u_height_update_failure(self) -> None:
        """
        Escenario: Actualización de u_height falla debido a un problema con NetBox.
        """
        mock_record = MagicMock()
        mock_record.u_height = 1.0
        mock_record.update.side_effect = RequestError(MagicMock(status_code=500))

        with pytest.raises(
            NetBoxApiError, match="Error actualizando u_height de DeviceType 'R640'"
        ):
            _sync_device_type_u_height(
                existing_dt=mock_record, model="R640", target_height=2.0, dry_run=False
            )


class TestSyncVM:
    @patch("export_to_netbox._resolve_base_node")
    @patch("export_to_netbox._resolve_cluster")
    @patch("export_to_netbox._validate_sync")
    def test_sync_vm_cache_propagation(
        self,
        mock_validate_sync: MagicMock,
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
        mock_base_node.return_value = (
            {
                "machine_name": "vm1",
                "inventory_uuid": "1234",
                "machine_type": "vm",
                "payload": {},
            },
            mock_caches,
        )
        mock_cluster.return_value = (1, mock_caches.clusters)
        mock_validate_sync.return_value = (SyncStatus.CREATED, 1, MagicMock())

        _, returned_caches = sync_vm(
            endpoints=MagicMock(),
            row=row,
            config=mock_config,
            site=MagicMock(),
            cluster_type_map={},
            fallback_cluster_type=MagicMock(),
            caches=mock_caches,
            csv_name_counts=Counter(),
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
            dry_run=False,
            prune_interfaces=False,
            main_obj=MagicMock(),
            machine_name="test-machine",
        )

        assert iface_errors == 2
        assert any_changes is True
        mock_assign_ipv4.assert_called_once()
        mock_assign_mac.assert_called_once()


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

        result, new_counts = _classify_rows(rows, MagicMock(), counts)  # type: ignore

        assert len(result.device_rows) == 2
        assert len(result.vm_rows) == 1
        assert new_counts[SyncStatus.ERROR] == 0
        # Validar índices reales devueltos (start=2)
        assert result.device_rows[0][0] == 2
        assert result.vm_rows[0][0] == 3
        assert result.device_rows[1][0] == 4

    def test_classify_rows_validation_error(
        self, monkeypatch: pytest.MonkeyPatch
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

        result, new_counts = _classify_rows(rows, MagicMock(), counts)  # type: ignore

        assert len(result.device_rows) == 1
        assert len(result.vm_rows) == 0
        assert new_counts[SyncStatus.ERROR] == 6  # Se sumó 1 error


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
            fallback_cluster_type=MagicMock(),
            caches=MagicMock(),
            csv_name_counts=Counter(),
            counts=counts,
            dry_run=False,
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
            fallback_cluster_type=MagicMock(),
            caches=MagicMock(),
            csv_name_counts=Counter(),
            counts=counts,
            dry_run=False,
        )

        assert counts[SyncStatus.UNCHANGED] == 0
        assert counts[SyncStatus.UPDATED] == 1
        assert counts[SyncStatus.ERROR] == 0
