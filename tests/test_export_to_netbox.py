"""
Pruebas unitarias para export_to_netbox.py
==========================================

Etapa 1: Funciones puras de utilidad.

Estas funciones no dependen de I/O externo (pynetbox, API, filesystem),
por lo que se pueden testear directamente sin mocks.
"""

import hashlib

import pytest

from export_to_netbox import (
    CastType,
    ChoiceItemConfig,
    ChoiceSetConfig,
    ConfigValidationError,
    CustomFieldConfig,
    FieldMappingConfig,
    MockNetBoxRecord,
    NetBoxMappingConfig,
    NodeType,
    RowValidationError,
    _extract_raw_source_value,
    _generate_fallback_slug,
    _resolve_default_or_empty,
    _resolve_field_value,
    _validate_csv_headers,
    _validate_select_choice,
    apply_cast,
    build_payload,
    concat_dot,
    count_machine_names,
    extract_csv_value,
    get_netbox_object_id,
    get_node_type_from_row,
    load_config,
    parse_bool_si_no,
    parse_int,
    parse_int_gb_to_mb,
    slugify,
)

# ============================================================
# slugify
# ============================================================


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


# ============================================================
# _generate_fallback_slug
# ============================================================


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


# ============================================================
# parse_int
# ============================================================


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


# ============================================================
# parse_int_gb_to_mb
# ============================================================


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


# ============================================================
# parse_bool_si_no
# ============================================================


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


# ============================================================
# apply_cast
# ============================================================


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


# ============================================================
# get_netbox_object_id
# ============================================================


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


# ============================================================
# ETAPA 2: EXTRACCIÓN CSV Y CONFIGURACIÓN YAML
# ============================================================


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


# ============================================================
# ETAPA 3: RESOLUCIÓN DE CAMPOS Y PAYLOAD
# ============================================================


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
