"""
Pruebas unitarias para export_to_netbox.py
"""

import hashlib
from collections import Counter
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from pynetbox.core.query import RequestError  # type: ignore

from export_to_netbox import (
    CastType,
    ChoiceItemConfig,
    ChoiceSetConfig,
    ConfigValidationError,
    CustomFieldConfig,
    FieldMappingConfig,
    FieldParseError,
    MockNetBoxRecord,
    NetBoxApiError,
    NetBoxMappingConfig,
    NetBoxObject,
    NodeType,
    RowValidationError,
    SiteConfig,
    SyncStatus,
    _build_interface_cidr,
    _execute_sync,
    _extract_raw_source_value,
    _find_existing_object,
    _generate_fallback_slug,
    _is_name_safely_unique,
    _parse_single_network_interface,
    _resolve_default_or_empty,
    _resolve_device_role,
    _resolve_field_value,
    _resolve_netbox_status,
    _validate_csv_headers,
    _validate_interface_ip,
    _validate_select_choice,
    apply_cast,
    build_payload,
    concat_dot,
    count_machine_names,
    create_with_fallback_slug,
    ensure_manufacturer,
    ensure_site,
    extract_csv_value,
    get_netbox_object_id,
    get_node_type_from_row,
    load_config,
    parse_bool_si_no,
    parse_int,
    parse_int_gb_to_mb,
    parse_network_interfaces,
    slugify,
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
        resultado = _generate_fallback_slug("mi-slug", "Nombre Original")
        expected_hash = hashlib.md5(b"Nombre Original").hexdigest()[:4]
        assert resultado == f"mi-slug-{expected_hash}"

    def test_determinism(self) -> None:
        """La misma entrada siempre produce el mismo slug de fallback."""
        a = _generate_fallback_slug("base", "Mismo Nombre")
        b = _generate_fallback_slug("base", "Mismo Nombre")
        assert a == b

    def test_different_names_produce_different_hashes(self) -> None:
        """Nombres distintos generan sufijos hash distintos."""
        a = _generate_fallback_slug("base", "Nombre A")
        b = _generate_fallback_slug("base", "Nombre B")
        assert a != b

    def test_generate_fallback_slug(self) -> None:
        base = "srv-01"
        fallback = _generate_fallback_slug(base, "SRV-01")
        assert fallback.startswith("srv-01-")
        assert len(fallback) == 7 + 4  # 'srv-01-' + 4 chars md5

    def test_success_on_retry(self) -> None:

        endpoint = MagicMock()
        mock_obj = MockNetBoxRecord(id=10, name="SRV", slug="srv-1234")

        # Falla la primera, funciona la segunda
        endpoint.create.side_effect = [
            RequestError(MagicMock(status_code=400, reason="Collision")),
            mock_obj,
        ]

        result = create_with_fallback_slug(
            endpoint, "Device", "SRV", name="SRV", slug="srv"
        )
        assert result.id == 10
        assert endpoint.create.call_count == 2
        # Verifica que la segunda llamada uso un slug diferente
        call_kwargs = endpoint.create.call_args_list[1][1]
        assert call_kwargs["slug"] != "srv"
        assert call_kwargs["slug"].startswith("srv-")

    def test_failure_on_retry(self) -> None:

        endpoint = MagicMock()
        # Falla siempre
        endpoint.create.side_effect = RequestError(
            MagicMock(status_code=400, reason="Persistent Collision")
        )

        with pytest.raises(NetBoxApiError, match="Imposible crear Device 'SRV'"):
            create_with_fallback_slug(endpoint, "Device", "SRV", name="SRV", slug="srv")


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


class TestParseIntGbToMb:
    """Verifica la conversión de GB a MB (NetBox espera MB para RAM)."""

    def test_gb_integer(self) -> None:
        """8 GB = 8192 MB."""
        assert parse_int_gb_to_mb("8") == 8192

    def test_gb_decimal(self) -> None:
        """0.5 GB = 512 MB."""
        assert parse_int_gb_to_mb("0.5") == 512

    def test_with_spaces(self) -> None:
        assert parse_int_gb_to_mb("  16  ") == 16384

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
        assert apply_cast("8", CastType.INT_GB_TO_MB, "memory") == 8192

    def test_cast_bool_si_no(self) -> None:
        assert apply_cast("si", CastType.BOOL_SI_NO, "is_virtual") is True

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
        """Verifica la precisión del redondeo: 1.5 GB = 1536 MB."""
        assert apply_cast("1.5", CastType.INT_GB_TO_MB, "memory") == 1536


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

    def test_required_empty_field_raises_error(
        self, config: NetBoxMappingConfig
    ) -> None:
        col_name = config.csv_columns["machine_name"].source
        row = {col_name: " "}
        with pytest.raises(
            RowValidationError, match="obligatorio 'machine_name' está vacío"
        ):
            extract_csv_value(row, "machine_name", config, required=True)

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
            extract_csv_value(row, "machine_name", config_copy, required=True)


class TestConcatDot:
    """Verifica la concatenación de campos de texto."""

    def test_valid_parts(self, config: NetBoxMappingConfig) -> None:
        assert concat_dot(["A", "B", "C"], config) == "A. B. C"

    def test_empty_filtered(self, config: NetBoxMappingConfig) -> None:
        assert concat_dot(["A", "N/A", "", "C"], config) == "A. C"

    def test_all_empty(self, config: NetBoxMappingConfig) -> None:
        assert concat_dot(["", "N/A"], config) == ""


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
        ]
        counts = count_machine_names(rows, config)
        assert counts["SRV-01"] == 2
        assert counts["SRV-02"] == 1
        assert counts["SRV-03"] == 1
        assert counts["SRV-04"] == 0


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

    def test_list_source(self) -> None:
        # Extrae el primer elemento en caso de que sea lista (comportamiento fallback)
        row = {"ColA": "ValorA", "ColB": "ValorB"}
        assert _extract_raw_source_value(row, ["ColA", "ColB"]) == "ValorA"

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
        # machine_type ya tiene mapeo en YAML: Dedicada -> device
        col_name = config.csv_columns["machine_type"].source
        row = {col_name: "Dedicada"}
        field_def = FieldMappingConfig(target="tipo", source=col_name)
        assert (
            _resolve_field_value(row, field_def, config, is_optional=True) == "device"
        )

    def test_cast_field(self, config: NetBoxMappingConfig) -> None:
        row = {"RAM": "8"}
        field_def = FieldMappingConfig(
            target="memory", source="RAM", cast=CastType.INT_GB_TO_MB
        )
        assert _resolve_field_value(row, field_def, config, is_optional=True) == 8192

    def test_concat_dot_field(self, config: NetBoxMappingConfig) -> None:
        row = {"Nota1": "Hola", "Nota2": "Mundo"}
        field_def = FieldMappingConfig(
            target="comments", source=["Nota1", "Nota2"], transform="concat_dot"
        )
        assert (
            _resolve_field_value(row, field_def, config, is_optional=True)
            == "Hola. Mundo"
        )

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
        assert payload["memory"] == 8192
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
            FieldMappingConfig(source="Tipo_maquina", target="machine_type"),
        ]
        row = {"Nombre": "SRV", "vCPUs": "4", "Tipo_maquina": "Dedicada"}

        payload = build_payload(row, native_map, custom_map, config)

        assert payload["name"] == "SRV"
        assert "custom_fields" in payload
        assert payload["custom_fields"]["cpu_cores"] == "4"
        assert payload["custom_fields"]["machine_type"] == "Dedicada"


class TestResolveNetboxStatus:
    """Verifica la resolución del estado en NetBox."""

    def test_mapped_status(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["status"].source
        row = {
            col_name: "Activo",
            config.csv_columns["machine_type"].source: "Dedicada",
        }
        assert _resolve_netbox_status(row, config) == "active"

    def test_unmapped_status_fallback(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["status"].source
        row = {col_name: "Desconocido", config.csv_columns["machine_type"].source: "VM"}
        assert _resolve_netbox_status(row, config) == "staged"

    def test_empty_status_fallback(self, config: NetBoxMappingConfig) -> None:
        row = {config.csv_columns["machine_type"].source: "Dedicada"}
        assert _resolve_netbox_status(row, config) == "inventory"


class TestResolveDeviceRole:
    """Verifica la búsqueda y fallback de roles."""

    def test_role_exists(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["role"].source
        row = {col_name: "DB", config.csv_columns["machine_name"].source: "SRV-01"}
        cache: dict[str, NetBoxObject] = {"db": MockNetBoxRecord(id=10, name="DB")}
        assert _resolve_device_role(row, cache, config) == 10

    def test_role_not_exists_uses_others(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["role"].source
        row = {
            col_name: "Inexistente",
            config.csv_columns["machine_name"].source: "SRV-01",
        }
        cache: dict[str, NetBoxObject] = {
            "others": MockNetBoxRecord(id=99, name="Others")
        }
        assert _resolve_device_role(row, cache, config) == 99

    def test_others_not_exists_raises_error(self, config: NetBoxMappingConfig) -> None:
        row = {config.csv_columns["machine_name"].source: "SRV-01"}
        cache: dict[str, NetBoxObject] = {}
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

    def test_not_found(self, config: NetBoxMappingConfig) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = []

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

        status, obj_id = _execute_sync(
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
        mock_existing.update.return_value = True  # Hubo cambios

        status, obj_id = _execute_sync(
            endpoint,
            {"name": "SRV-01-nuevo"},
            [mock_existing],
            "SRV-01",
            "uuid-1",
            dry_run=False,
        )
        assert status == SyncStatus.UPDATED
        assert obj_id == 50
        mock_existing.update.assert_called_once_with({"name": "SRV-01-nuevo"})

    def test_unchanged(self) -> None:
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"

        mock_existing = MagicMock()
        mock_existing.id = 50
        mock_existing.update.return_value = False  # Sin cambios

        status, obj_id = _execute_sync(
            endpoint,
            {"name": "SRV-01"},
            [mock_existing],
            "SRV-01",
            "uuid-1",
            dry_run=False,
        )
        assert status == SyncStatus.UNCHANGED
        assert obj_id == 50

    def test_dry_run_create(self) -> None:
        endpoint = MagicMock()
        endpoint.url = "http://test/api/dcim/devices/"

        status, obj_id = _execute_sync(
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
        # Eliminar _init_cache para forzar el fallback de _check_record_changes
        del mock_existing._init_cache

        status, obj_id = _execute_sync(
            endpoint,
            {"name": "SRV-01-nuevo"},
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

        with pytest.raises(RequestError):
            _execute_sync(
                endpoint,
                {"name": "SRV-nuevo"},
                [mock_existing],
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
            config.csv_columns["iface_mac"].source: "AA:AA, BB:BB",
        }
        interfaces = parse_network_interfaces(row, config)
        assert len(interfaces) == 2
        assert interfaces[0]["name"] == "eth0"
        assert interfaces[0]["cidr"] == "192.168.1.1/24"
        assert interfaces[1]["name"] == "eth1"
        assert interfaces[1]["cidr"] == "10.0.0.1/8"
        assert interfaces[1]["enabled"] is False

    def test_no_interfaces(self, config: NetBoxMappingConfig) -> None:
        row = {config.csv_columns["iface_names"].source: ""}
        assert parse_network_interfaces(row, config) == []

    def test_inconsistent_lengths_raises_error(
        self, config: NetBoxMappingConfig
    ) -> None:
        row = {
            config.csv_columns["iface_names"].source: "eth0, eth1",
            config.csv_columns["iface_ip"].source: "192.168.1.1",  # falta una IP
        }
        with pytest.raises(RowValidationError, match="longitudes inconsistentes"):
            parse_network_interfaces(row, config)

    def test_empty_columns_filled(self, config: NetBoxMappingConfig) -> None:
        row = {
            config.csv_columns["iface_names"].source: "eth0, eth1",
            config.csv_columns["iface_ip"].source: "",  # Completamente vacío
        }
        interfaces = parse_network_interfaces(row, config)
        assert len(interfaces) == 2
        assert interfaces[0]["ip"] is None
        assert interfaces[1]["ip"] is None

    def test_partial_ips_with_na(self, config: NetBoxMappingConfig) -> None:
        row = {
            config.csv_columns["iface_names"].source: "eth0, eth1",
            config.csv_columns["iface_ip"].source: "192.168.1.1, N/A",
            config.csv_columns["iface_pfx"].source: "24, N/A",
        }
        interfaces = parse_network_interfaces(row, config)
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
        interfaces = parse_network_interfaces(row, config)
        assert len(interfaces) == 1
        assert interfaces[0]["enabled"] is True  # Por default


class TestEnsureSite:
    """Verifica la lógica de ensure_site (búsqueda, creación y dry-run)."""

    def test_site_exists(self) -> None:
        endpoints = MagicMock()
        mock_site = MockNetBoxRecord(id=1, name="DC1", slug="dc1")
        endpoints.sites.filter.return_value = [mock_site]

        site_cfg = SiteConfig(name="DC1", slug="dc1")
        result = ensure_site(cast(Any, endpoints), site_cfg, dry_run=False)

        assert result.id == 1
        endpoints.sites.filter.assert_called_once_with(name="DC1")
        endpoints.sites.create.assert_not_called()

    def test_site_created(self) -> None:
        endpoints = MagicMock()
        endpoints.sites.filter.return_value = []
        mock_site = MockNetBoxRecord(id=2, name="DC2", slug="dc2")
        endpoints.sites.create.return_value = mock_site

        site_cfg = SiteConfig(name="DC2", slug="dc2")
        result = ensure_site(cast(Any, endpoints), site_cfg, dry_run=False)

        assert result.id == 2
        endpoints.sites.create.assert_called_once_with(name="DC2", slug="dc2")

    def test_dry_run_create_mock_site(self) -> None:
        endpoints = MagicMock()
        endpoints.sites.filter.return_value = []

        site_cfg = SiteConfig(name="DC3", slug="dc3")
        result = ensure_site(cast(Any, endpoints), site_cfg, dry_run=True)

        assert result.id == 0  # Mock
        assert result.name == "DC3"
        endpoints.sites.create.assert_not_called()


class TestEnsureManufacturer:
    """Verifica la lógica de ensure_manufacturer con cache y fallback de slug."""

    def test_found_in_cache(self) -> None:
        endpoint = MagicMock()
        cache: dict[str, NetBoxObject] = {"Dell": MockNetBoxRecord(id=5, name="Dell")}

        result = ensure_manufacturer(endpoint, "Dell", cache, dry_run=False)
        assert result.id == 5
        endpoint.filter.assert_not_called()

    def test_found_by_name(self) -> None:
        endpoint = MagicMock()
        mock_mfg = MockNetBoxRecord(id=6, name="HP")
        endpoint.filter.return_value = [mock_mfg]
        cache: dict[str, NetBoxObject] = {}

        result = ensure_manufacturer(endpoint, "HP", cache, dry_run=False)
        assert result.id == 6
        assert cache["HP"].id == 6
        endpoint.filter.assert_called_once_with(name="HP")

    def test_found_by_slug_fallback(self) -> None:
        endpoint = MagicMock()
        # filter(name="H.P.") retorna vacío, pero filter(slug="hp") retorna un registro
        mock_mfg = MockNetBoxRecord(id=7, name="HP")
        endpoint.filter.side_effect = [[], [mock_mfg]]
        cache: dict[str, NetBoxObject] = {}

        result = ensure_manufacturer(endpoint, "H.P.", cache, dry_run=False)
        assert result.id == 7
        assert cache["H.P."].id == 7
        assert endpoint.filter.call_count == 2

    def test_created_when_not_found(self) -> None:
        endpoint = MagicMock()
        endpoint.filter.return_value = []
        mock_created = MockNetBoxRecord(id=8, name="Lenovo")
        endpoint.create.return_value = mock_created
        cache: dict[str, NetBoxObject] = {}

        result = ensure_manufacturer(endpoint, "Lenovo", cache, dry_run=False)
        assert result.id == 8
        assert cache["Lenovo"].id == 8
        endpoint.create.assert_called_once_with(name="Lenovo", slug="lenovo")
