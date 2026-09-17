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
    ConfigValidationError,
    MockNetBoxRecord,
    NetBoxMappingConfig,
    NodeType,
    RowValidationError,
    _generate_fallback_slug,
    _validate_csv_headers,
    apply_cast,
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

    def test_nombre_simple(self) -> None:
        """Un nombre limpio se convierte a minúsculas sin alteraciones."""
        assert slugify("Produccion") == "produccion"

    def test_espacios_y_guiones(self) -> None:
        """Espacios, guiones bajos y guiones múltiples se unifican en '-'."""
        assert slugify("Data  Center__Principal--Rack") == "data-center-principal-rack"

    def test_caracteres_especiales(self) -> None:
        """Caracteres no alfanuméricos (excepto guiones) se eliminan."""
        assert slugify("Rack #3 (Piso 2)") == "rack-3-piso-2"

    def test_truncamiento_a_100_caracteres(self) -> None:
        """NetBox limita los slugs a 100 caracteres."""
        nombre_largo = "a" * 150
        resultado = slugify(nombre_largo)
        assert len(resultado) == 100
        assert resultado == "a" * 100

    def test_string_vacio(self) -> None:
        """Un string vacío produce un slug vacío."""
        assert slugify("") == ""

    def test_strip_guiones_extremos(self) -> None:
        """Guiones al inicio y final del slug se eliminan."""
        assert slugify("--nombre--") == "nombre"

    def test_acentos_preservados(self) -> None:
        """Caracteres acentuados (válidos en NetBox) se preservan."""
        assert slugify("Producción") == "producción"


# ============================================================
# _generate_fallback_slug
# ============================================================


class TestGenerateFallbackSlug:
    """Verifica la generación determinista de slugs de respaldo."""

    def test_formato_slug_con_hash(self) -> None:
        """El slug de fallback tiene el formato 'base-XXXX' (hash MD5 de 4 chars)."""
        resultado = _generate_fallback_slug("mi-slug", "Nombre Original")
        expected_hash = hashlib.md5(b"Nombre Original").hexdigest()[:4]
        assert resultado == f"mi-slug-{expected_hash}"

    def test_determinismo(self) -> None:
        """La misma entrada siempre produce el mismo slug de fallback."""
        a = _generate_fallback_slug("base", "Mismo Nombre")
        b = _generate_fallback_slug("base", "Mismo Nombre")
        assert a == b

    def test_diferentes_nombres_producen_diferentes_hashes(self) -> None:
        """Nombres distintos generan sufijos hash distintos."""
        a = _generate_fallback_slug("base", "Nombre A")
        b = _generate_fallback_slug("base", "Nombre B")
        assert a != b


# ============================================================
# parse_int
# ============================================================


class TestParseInt:
    """Verifica la conversión robusta de valores a enteros."""

    def test_entero_valido(self) -> None:
        assert parse_int("42") == 42

    def test_entero_con_espacios(self) -> None:
        """El strip() interno elimina espacios antes de parsear."""
        assert parse_int("  128  ") == 128

    def test_entero_negativo(self) -> None:
        assert parse_int("-7") == -7

    def test_valor_invalido_lanza_error(self) -> None:
        with pytest.raises(ValueError, match="No es un número entero válido"):
            parse_int("abc")

    def test_float_string_lanza_error(self) -> None:
        """Un float como '3.14' no es un entero válido."""
        with pytest.raises(ValueError, match="No es un número entero válido"):
            parse_int("3.14")


# ============================================================
# parse_int_gb_to_mb
# ============================================================


class TestParseIntGbToMb:
    """Verifica la conversión de GB a MB (NetBox espera MB para RAM)."""

    def test_entero_gb(self) -> None:
        """8 GB = 8192 MB."""
        assert parse_int_gb_to_mb("8") == 8192

    def test_decimal_gb(self) -> None:
        """0.5 GB = 512 MB."""
        assert parse_int_gb_to_mb("0.5") == 512

    def test_con_espacios(self) -> None:
        assert parse_int_gb_to_mb("  16  ") == 16384

    def test_valor_invalido_lanza_error(self) -> None:
        with pytest.raises(ValueError, match="No es un valor numérico válido"):
            parse_int_gb_to_mb("no-es-numero")

    def test_cero_gb(self) -> None:
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
    def test_valores_verdaderos(self, valor: str) -> None:
        assert parse_bool_si_no(valor) is True

    @pytest.mark.parametrize(
        "valor",
        ["no", "NO", "No", "false", "False", "0"],
    )
    def test_valores_falsos(self, valor: str) -> None:
        assert parse_bool_si_no(valor) is False

    def test_valor_invalido_lanza_error(self) -> None:
        with pytest.raises(ValueError, match="No es 'si' ni 'no'"):
            parse_bool_si_no("quizás")

    def test_con_espacios(self) -> None:
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

    def test_cast_invalido_lanza_row_validation_error(self) -> None:
        """apply_cast envuelve ValueError en RowValidationError con contexto."""
        with pytest.raises(RowValidationError, match="Valor inválido.*cpu_cores"):
            apply_cast("abc", CastType.INT, "cpu_cores")

    def test_none_con_cast_int_lanza_error(self) -> None:
        """None no es convertible a int; se espera RowValidationError."""
        with pytest.raises(RowValidationError, match="Valor inválido.*campo"):
            apply_cast(None, CastType.INT, "campo")

    def test_string_no_numerico_con_cast_int_lanza_error(self) -> None:
        """Un string no-numérico con cast INT produce RowValidationError."""
        with pytest.raises(RowValidationError, match="Valor inválido.*campo"):
            apply_cast("texto", CastType.INT, "campo")


class TestApplyCastEdgeCases:
    """Casos específicos del branch final de apply_cast."""

    def test_int_valido_retorna_int(self) -> None:
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

    def test_objeto_con_id(self, mock_record: MockNetBoxRecord) -> None:
        """Un objeto con id=42 retorna 42."""
        assert get_netbox_object_id(mock_record) == 42

    def test_objeto_sin_id_retorna_cero(self) -> None:
        """Un objeto con id=0 (mock de dry-run) retorna 0."""
        obj = MockNetBoxRecord(id=0, name="dry-run-mock")
        assert get_netbox_object_id(obj) == 0

    def test_objeto_con_id_string_se_convierte(self) -> None:
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

    def test_valor_normal(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["machine_name"].source
        row = {col_name: "SRV-01"}
        assert extract_csv_value(row, "machine_name", config) == "SRV-01"

    def test_valor_vacio(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["machine_name"].source
        row = {col_name: ""}
        assert extract_csv_value(row, "machine_name", config) == ""

    def test_valor_na_se_convierte_en_vacio(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["machine_name"].source
        row = {col_name: "N/A"}
        assert extract_csv_value(row, "machine_name", config) == ""

    def test_campo_requerido_vacio_lanza_error(
        self, config: NetBoxMappingConfig
    ) -> None:
        col_name = config.csv_columns["machine_name"].source
        row = {col_name: " "}
        with pytest.raises(
            RowValidationError, match="obligatorio 'machine_name' está vacío"
        ):
            extract_csv_value(row, "machine_name", config, required=True)

    def test_alias_inexistente_lanza_error_critico(
        self, config: NetBoxMappingConfig
    ) -> None:
        col_name = config.csv_columns["machine_name"].source
        row = {col_name: "SRV-01"}
        with pytest.raises(
            ConfigValidationError, match="El alias 'alias_falso' solicitado"
        ):
            extract_csv_value(row, "alias_falso", config)

    def test_columna_con_origen_nulo_retorna_vacio(
        self, config: NetBoxMappingConfig
    ) -> None:
        config_copy = config.model_copy(deep=True)
        col = config_copy.csv_columns["machine_name"].model_copy(
            update={"source": None}
        )
        config_copy.csv_columns["machine_name"] = col
        row = {"Cualquier_Columna": "SRV-01"}
        assert extract_csv_value(row, "machine_name", config_copy) == ""

    def test_origen_nulo_y_requerido_lanza_error(
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

    def test_partes_validas(self, config: NetBoxMappingConfig) -> None:
        assert concat_dot(["A", "B", "C"], config) == "A. B. C"

    def test_vacion_filtrados(self, config: NetBoxMappingConfig) -> None:
        assert concat_dot(["A", "N/A", "", "C"], config) == "A. C"

    def test_todas_vacias(self, config: NetBoxMappingConfig) -> None:
        assert concat_dot(["", "N/A"], config) == ""


class TestGetNodeTypeFromRow:
    """Verifica la resolución del NodeType desde una fila CSV."""

    def test_tipo_device(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["machine_type"].source
        row = {col_name: "Dedicada"}
        assert get_node_type_from_row(row, config) == NodeType.DEVICE

    def test_tipo_vm(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["machine_type"].source
        row = {col_name: "VM"}
        assert get_node_type_from_row(row, config) == NodeType.VIRTUAL_MACHINE

    def test_tipo_no_mapeado_lanza_error(self, config: NetBoxMappingConfig) -> None:
        col_name = config.csv_columns["machine_type"].source
        row = {col_name: "Desconocido"}
        with pytest.raises(
            RowValidationError, match="no está definido en el mapa configurado"
        ):
            get_node_type_from_row(row, config)


class TestCountMachineNames:
    """Verifica el contador de nombres de máquinas."""

    def test_conteo_correcto(self, config: NetBoxMappingConfig) -> None:
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

    def test_headers_validos(self, config: NetBoxMappingConfig) -> None:
        # Extraemos los requeridos de la configuración cargada
        headers = [
            col.source
            for col in config.csv_columns.values()
            if col.required and col.source
        ]
        # Debería pasar sin lanzar excepciones
        _validate_csv_headers(headers, config)

    def test_headers_con_extras_permitidos(self, config: NetBoxMappingConfig) -> None:
        headers = [
            col.source
            for col in config.csv_columns.values()
            if col.required and col.source
        ]
        headers.extend(["Columna Extra 1", "Columna Extra 2"])
        _validate_csv_headers(headers, config)

    def test_faltan_headers_requeridos_lanza_error(
        self, config: NetBoxMappingConfig
    ) -> None:
        headers = ["Solo una columna irrelevante"]
        with pytest.raises(
            ValueError, match="El CSV no contiene las siguientes columnas obligatorias"
        ):
            _validate_csv_headers(headers, config)


class TestLoadConfig:
    """Verifica la carga y validación del archivo YAML de configuración."""

    def test_carga_yaml_real_exitosa(self, yaml_path) -> None:
        """Verifica que el archivo yaml del proyecto se carga correctamente."""
        config = load_config(yaml_path)
        assert isinstance(config, NetBoxMappingConfig)
        assert len(config.csv_columns) > 0

    def test_yaml_inexistente_lanza_error(self, tmp_path) -> None:
        """Una ruta falsa lanza ConfigValidationError."""
        with pytest.raises(
            ConfigValidationError, match="No se encontró el archivo de mapping"
        ):
            load_config(tmp_path / "falso.yaml")

    def test_yaml_invalido_lanza_error(self, tmp_path) -> None:
        """Un YAML mal formado o que no cumple el esquema lanza ConfigValidationError."""
        yaml_invalido = tmp_path / "invalido.yaml"
        yaml_invalido.write_text("csv_columns: [lista_en_lugar_de_dict]")

        with pytest.raises(ConfigValidationError, match="Error de validación"):
            load_config(yaml_invalido)
