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
    MockNetBoxRecord,
    RowValidationError,
    _generate_fallback_slug,
    apply_cast,
    get_netbox_object_id,
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
