"""
export_to_netbox.py
===================
Exporta el inventario fusionado (merged_inventory.csv) a NetBox 4.x.

Variables de entorno requeridas:
    NETBOX_URL        → https://netbox.miempresa.com
    NETBOX_TOKEN      → token con permisos write sobre
                        dcim, virtualization, ipam, extras, core
    NETBOX_VERIFY_SSL → "true" / "false"  (por defecto: true)

Uso:
    python3 export_to_netbox.py merged_inventory.csv [netbox_mapping.yaml] [--dry-run]

Opciones:
    --dry-run   Muestra las operaciones que se ejecutarían sin
                modificar NetBox. Útil para validar antes del
                primer sync real.

Estrategia de idempotencia:
    El lookup de Device y VirtualMachine se hace primero por el
    custom field 'inventory_uuid', usando el nombre como fallback.
    Si la coincidencia es por nombre y este es único tanto en el
    CSV como en NetBox, se permite la actualización segura.
    Esto garantiza que un cambio de nombre en el CSV actualiza
    el objeto existente en NetBox en lugar de crear un duplicado.

Formato de entrada del CSV:
    - Campos simples: se procesan como strings directos (con .strip()).
    - Valores vacíos: se ignoran aquellos que coincidan exactamente con
      los valores definidos en `empty_values` del YAML (ej. "N/A", "None").
    - Interfaces de red: Las 5 columnas de red (Interfaces, estado, IP,
      Red IP, MAC) admiten múltiples valores separados por comas (`,`).
      Todos deben tener la misma cantidad de elementos por celda para
      sincronizarse correctamente.

Códigos de salida:
    0 → sin errores en filas individuales
    1 → al menos una fila produjo ERROR
"""

import argparse
import csv
import hashlib
import ipaddress
import logging
import os
import re
import sys
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import Any, Literal, NoReturn, TypeAlias, TypedDict, Union, cast

import requests
import urllib3
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)
from pynetbox.core.api import Api
from pynetbox.core.endpoint import Endpoint
from pynetbox.core.query import RequestError
from pynetbox.core.response import Record

# ============================================================
# LOGGER
# ============================================================

logging.basicConfig(
    format="%(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stderr,
)
log = logging.getLogger("export_to_netbox")


# ============================================================
# ERRORES PERSONALIZADOS
# ============================================================


class RowValidationError(ValueError):
    """Excepción lanzada cuando los datos de una fila son explícitamente inválidos."""


class ConfigValidationError(ValueError):
    """Excepción lanzada cuando hay errores críticos de configuración en el script o YAML."""


class RowSkipCondition(Exception):
    """Excepción lanzada para abortar tempranamente el procesamiento
    de una fila de forma silenciosa (SKIP)."""


class NetBoxApiError(Exception):
    """Excepción lanzada cuando ocurre un error de API persistente
    al interactuar con Pynetbox (RequestError)."""


class FieldParseError(ValueError):
    """Excepción lanzada cuando un campo opcional contiene datos mal formados,
    permitiendo a la capa superior decidir si ignorarlo o abortar la fila."""


# ============================================================
# TYPE ALIASES Y ESTRUCTURAS DE TIPOS
# ============================================================


class SyncStatus(str, Enum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


class NodeType(str, Enum):
    DEVICE = "device"
    VIRTUAL_MACHINE = "virtual_machine"


class CastType(str, Enum):
    INT = "int"
    FLOAT = "float"
    FLOAT_TO_INT = "float_to_int"
    INT_GB_TO_MB = "int_gb_to_mb"
    BOOL_SI_NO = "bool_si_no"
    LOWER = "lower"


NetBoxObject: TypeAlias = Union[Record, "MockNetBoxRecord"]
SyncResult: TypeAlias = tuple[SyncStatus, int, NetBoxObject | None]
CsvRow: TypeAlias = dict[str, str]
FieldValue: TypeAlias = str | int | float | bool | list[str] | None
CustomFieldsPayload: TypeAlias = dict[str, FieldValue]
SyncCounts: TypeAlias = dict[SyncStatus, int]
NetBoxPayload: TypeAlias = dict[str, Any]


class NetworkInterfaceData(TypedDict):
    """Representa la estructura de datos parseada de una interfaz de red."""

    name: str
    enabled: bool
    mac: str | None
    ip: str | None
    prefix: str | None
    cidr: str | None


class BaseNodeData(TypedDict):
    """Representa la estructura base resuelta de un nodo antes de sincronizar."""

    machine_name: str
    inventory_uuid: str
    machine_type: str
    payload: NetBoxPayload


# ============================================================
# ENDPOINTS DE NETBOX
# ============================================================


class NetBoxEndpoints(BaseModel):
    """
    Representa los endpoints de NetBox utilizados por el script.

    La clase se utiliza para centralizar el acceso a los endpoints
    de NetBox y asegurar que todos los endpoints requeridos estén
    disponibles antes de comenzar cualquier operación de
    sincronización.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    object_types: Endpoint
    custom_fields: Endpoint
    choice_sets: Endpoint
    sites: Endpoint
    cluster_types: Endpoint
    manufacturers: Endpoint
    device_types: Endpoint
    platforms: Endpoint
    racks: Endpoint
    clusters: Endpoint
    device_roles: Endpoint
    devices: Endpoint
    virtual_machines: Endpoint
    device_interfaces: Endpoint
    vm_interfaces: Endpoint
    ip_addresses: Endpoint
    mac_addresses: Endpoint


# ============================================================
# MODELOS DE CONFIGURACIÓN YAML
# ============================================================


class SiteConfig(BaseModel):
    """Configuración del Site en NetBox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    slug: str | None = None

    @model_validator(mode="before")
    @classmethod
    def generate_slug_if_missing(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_slug = data.get("slug") or data.get("name")
            if raw_slug:
                data["slug"] = slugify(str(raw_slug))
        return data

    @field_validator("name", "slug")
    @classmethod
    def validate_no_unresolved_vars(cls, v: str | None) -> str | None:
        if v is not None and "$" in v and re.search(r"\$\{?\w+\}?", v):
            raise ValueError(
                f"El valor contiene variables de entorno no resueltas: '{v}'"
            )
        return v


class ClusterTypeDefaultConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1)
    slug: str | None = None

    @model_validator(mode="before")
    @classmethod
    def generate_slug_if_missing(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_slug = data.get("slug") or data.get("name")
            if raw_slug:
                data["slug"] = slugify(str(raw_slug))
        return data


class ClusterTypeConfig(BaseModel):
    """Configuración del ClusterType en NetBox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default: ClusterTypeDefaultConfig


class DeviceRoleConfig(BaseModel):
    """Configuración de un DeviceRole."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    slug: str | None = None
    color: str = Field(default="9e9e9e", pattern=r"^[0-9a-fA-F]{6}$")

    @model_validator(mode="before")
    @classmethod
    def generate_slug_if_missing(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_slug = data.get("slug") or data.get("name")
            if raw_slug:
                data["slug"] = slugify(str(raw_slug))
        return data


class ChoiceItemConfig(BaseModel):
    """Elemento individual de un Choice Set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = Field(min_length=1)
    label: str = Field(min_length=1)


class ChoiceSetConfig(BaseModel):
    """Definición de un Choice Set para Custom Fields de tipo 'select'."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    choices: list[ChoiceItemConfig] = Field(default_factory=list)


OBJECT_TYPE_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


class CustomFieldConfig(BaseModel):
    """Definición unificada de Custom Field para campos personalizados."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    label: str = Field(min_length=1)
    type: Literal[
        "text",
        "longtext",
        "integer",
        "decimal",
        "boolean",
        "date",
        "datetime",
        "url",
        "json",
        "select",
        "multiselect",
        "object",
        "multiobject",
    ] = "text"
    required: bool = False
    object_types: list[str] = Field(default_factory=list)
    choice_set: ChoiceSetConfig | None = None
    default: FieldValue = None

    @field_validator("object_types")
    @classmethod
    def validate_object_types(cls, v: list[str]) -> list[str]:
        for ot in v:
            if not OBJECT_TYPE_PATTERN.match(ot):
                raise ValueError(
                    f"Formato de Object Type inválido: '{ot}'. "
                    "Se esperaba 'app_label.model' (ej. 'dcim.device')."
                )
        return v

    @model_validator(mode="after")
    def validate_choice_set_if_select(self) -> "CustomFieldConfig":
        if self.type in ("select", "multiselect") and not self.choice_set:
            raise ValueError(
                "Los campos de tipo 'select' o 'multiselect' deben definir un 'choice_set'."
            )
        return self


class FieldMappingConfig(BaseModel):
    """Definición de mapeo entre columnas CSV y atributo NetBox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str | list[str]
    target: str = Field(min_length=1)
    required: bool = False
    is_unique: bool = False
    cast: CastType | None = None
    transform: Literal["concat_dot", "coalesce"] | None = None

    @model_validator(mode="after")
    def validate_transform_and_source(self) -> "FieldMappingConfig":
        source_list = self.source if isinstance(self.source, list) else [self.source]

        if not source_list:
            raise ValueError(
                f"El campo 'source' para target '{self.target}' no puede estar vacío."
            )

        if any(not s.strip() for s in source_list):
            raise ValueError(
                f"En 'source' (target '{self.target}'), ningún elemento puede estar vacío."
            )

        if self.transform in ("concat_dot", "coalesce") and not isinstance(
            self.source, list
        ):
            raise ValueError(
                f"El transform '{self.transform}' para target '{self.target}' "
                "requiere que 'source' sea explícitamente una lista."
            )

        return self


class StatusDefaultConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    default: str = Field(min_length=1)


class NodeMappingConfig(BaseModel):
    """Configuración de mapeo y fallback para un tipo de nodo específico."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: StatusDefaultConfig
    native_mappings: list[FieldMappingConfig] = Field(default_factory=list)
    custom_mappings: list[FieldMappingConfig] = Field(default_factory=list)


class NodeTypesConfig(BaseModel):
    """Configuración agrupada por tipo de nodo (device vs virtual_machine)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device: NodeMappingConfig
    virtual_machine: NodeMappingConfig

    def get_config(self, node_type: NodeType) -> NodeMappingConfig:
        """Retorna la configuración de mapeo fuertemente tipada según el tipo de nodo."""
        if node_type == NodeType.DEVICE:
            return self.device
        return self.virtual_machine


class CsvColumnDef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: str = Field(min_length=1)
    required: bool = False
    map: dict[str, Any] | None = None
    cluster_host_types: list[str] | None = None
    virtual_machine_types: list[str] = Field(default_factory=list)


class NetBoxMappingConfig(BaseModel):
    """
    Contrato completo de configuración y mapeo cargado desde netbox_mapping.yaml.
    Valida tipos, restricciones de valor y consistencia referencial.
    Centraliza el acceso a columnas y métodos utilitarios de ejecución como is_empty().

    Las listas y diccionarios de este modelo (ej. custom_field_definitions) son
    poblados automáticamente por Pydantic en la función load_config() al deserializar el
    archivo YAML.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    csv_columns: dict[str, CsvColumnDef]
    site: SiteConfig
    cluster_type: ClusterTypeConfig
    device_roles: list[DeviceRoleConfig]
    custom_field_definitions: list[CustomFieldConfig] = Field(default_factory=list)
    node_types: NodeTypesConfig
    empty_values: list[str] = Field(
        default_factory=lambda: ["N/A", "", "None", "n/a", "none"]
    )

    _empty_values_set: frozenset[str] = PrivateAttr(default_factory=frozenset)
    _custom_field_defs_map: dict[str, CustomFieldConfig] = PrivateAttr(
        default_factory=dict
    )

    def model_post_init(self, __context: Any, /) -> None:
        """Inicializa los valores vacíos y el mapa indexado de Custom Fields O(1)."""
        empty_vals_lower = {v.lower() for v in self.empty_values}
        object.__setattr__(
            self,
            "_empty_values_set",
            frozenset(empty_vals_lower) | {""},
        )

        cf_map: dict[str, CustomFieldConfig] = {}
        for cf in self.custom_field_definitions:
            cf_map[cf.name] = cf
        object.__setattr__(self, "_custom_field_defs_map", cf_map)

    @model_validator(mode="after")
    def validate_config_cross_references(self) -> "NetBoxMappingConfig":
        # 1. Validar que exista el rol "Others" (insensible a mayúsculas) para fallback
        role_names_lower = {r.name.strip().lower() for r in self.device_roles}
        if "others" not in role_names_lower:
            raise ValueError(
                "La lista 'device_roles' debe incluir un rol 'Others' "
                "para fallback de roles no reconocidos."
            )

        # 2. Validar que el Custom Field 'machine_type' esté definido en
        # custom_field_definitions y tenga un mapa
        cf_names = {cf.name for cf in self.custom_field_definitions}
        if "machine_type" not in cf_names:
            raise ValueError(
                "El Custom Field 'machine_type' es obligatorio dentro de custom_field_definitions."
            )

        col_def = self.csv_columns.get("machine_type")
        if not col_def or not col_def.map:
            raise ValueError(
                "La columna 'machine_type' debe tener un 'map' configurado en csv_columns."
            )

        # 3. Validar custom_mappings vs custom_field_definitions
        for node_type, node_config in [
            (NodeType.DEVICE, self.node_types.device),
            (NodeType.VIRTUAL_MACHINE, self.node_types.virtual_machine),
        ]:
            for cmap in node_config.custom_mappings:
                if cmap.target not in cf_names:
                    raise ValueError(
                        f"El custom_mapping target '{cmap.target}' en '{node_type}' "
                        "no está definido en custom_field_definitions."
                    )

        return self

    def get_required_columns(self) -> set[str]:
        """
        Retorna el conjunto de columnas obligatorias configuradas en el YAML.
        Esta lista define las columnas que deben estar *presentes en los encabezados*
        del CSV (fila 1). No implica que cada fila deba tener obligatoriamente un
        *valor no vacío* en dicha columna.
        """
        return {v.source for v in self.csv_columns.values() if v.required}

    def get_all_expected_columns(self) -> set[str]:
        """
        Retorna el catálogo completo de nombres de columnas esperadas
        en el CSV.
        """
        return {v.source for v in self.csv_columns.values()}

    def get_column_def_by_source(self, source_name: str) -> CsvColumnDef | None:
        """Busca y retorna la definición de la columna a partir de su nombre exacto (source) en el CSV."""
        for col_def in self.csv_columns.values():
            if col_def.source == source_name:
                return col_def
        return None

    def _apply_map(
        self, col_def: CsvColumnDef | None, value: str, identifier: str, strict: bool
    ) -> Any:
        if not col_def or not col_def.map:
            return value

        mapped = col_def.map.get(value.strip())
        if mapped is not None:
            return mapped

        if not strict:
            return None

        raise RowValidationError(
            f"El valor '{value}' de la {identifier} no está definido en el mapa configurado."
        )

    def map_value(self, alias: str, value: str, strict: bool = True) -> Any:
        """
        Aplica el mapa de transformación de la columna identificada por su `alias`.
        Si la columna no tiene mapa, devuelve el `value` original.
        Si la columna tiene mapa y el valor no existe en él:
            - Si strict=True, levanta RowValidationError.
            - Si strict=False, devuelve None.
        """
        col_def = self.csv_columns.get(alias)
        return self._apply_map(col_def, value, f"columna '{alias}'", strict)

    def map_value_by_source(self, source: str, value: str, strict: bool = True) -> Any:
        """Igual que map_value, pero busca la columna por su nombre exacto (source) en el CSV."""
        col_def = self.get_column_def_by_source(source)
        return self._apply_map(col_def, value, f"columna de origen '{source}'", strict)

    def get_custom_field_def(self, target: str) -> CustomFieldConfig | None:
        """Retorna la definición de un Custom Field en O(1) por nombre de target."""
        return self._custom_field_defs_map.get(target)

    def get_all_custom_field_defs(self) -> list[CustomFieldConfig]:
        """Retorna la lista completa de definiciones de Custom Field."""
        return list(self._custom_field_defs_map.values())

    def is_empty(self, value: Any) -> bool:
        """Determina si un valor es considerado vacío según empty_values."""
        if value is None:
            return True
        return str(value).strip().lower() in self._empty_values_set

    def resolve_node_type(self, machine_type: str) -> NodeType:
        """
        Resuelve el NodeType ('device' o 'virtual_machine') basándose en las listas
        semánticas de virtual_machine_types.
        Lanza RowValidationError si el campo está vacío.
        """
        if not machine_type:
            raise RowValidationError("El campo 'machine_type' está vacío.")

        mapped_type = self.map_value("machine_type", machine_type, strict=True)

        col_def = self.csv_columns.get("machine_type")
        vm_types = col_def.virtual_machine_types if col_def else []

        if mapped_type in vm_types:
            return NodeType.VIRTUAL_MACHINE
        return NodeType.DEVICE


# ===========================================================
# CACHE DE DATOS
# ===========================================================


class MockNetBoxRecord(BaseModel):
    """Representa un objeto simulado de NetBox para ejecuciones en modo dry-run."""

    model_config = ConfigDict(frozen=True, extra="allow")

    id: int = 0
    name: str = ""
    slug: str = ""
    type: str = ""
    model: str = ""
    vm_role: bool = False
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    # Red y asignaciones (para simulaciones de IP/MAC)
    address: str | None = None
    mac_address: str | None = None
    assigned_object_id: int | None = None
    assigned_object_type: str | None = None


class CacheStore(BaseModel):
    """
    Representa el cache de objetos de NetBox que se mantiene
    durante toda la ejecución del script.

    Incluye:
        manufacturers: mapea nombre → Manufacturer
        device_types: mapea (fabricante_id, modelo) → DeviceType
        platforms: mapea nombre → Platform
        racks: mapea "site_name/rack_name" → Rack
        clusters: mapea nombre → Cluster
        device_roles: mapea nombre_lower → DeviceRole
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    manufacturers: dict[str, NetBoxObject] = Field(default_factory=dict)
    device_types: dict[tuple[str, str], NetBoxObject] = Field(default_factory=dict)
    platforms: dict[str, NetBoxObject] = Field(default_factory=dict)
    racks: dict[tuple[int, str], NetBoxObject] = Field(default_factory=dict)
    clusters: dict[tuple[int, str], NetBoxObject] = Field(default_factory=dict)
    cluster_types: dict[str, NetBoxObject] = Field(default_factory=dict)
    device_roles: dict[str, NetBoxObject] = Field(default_factory=dict)
    host_devices: dict[tuple[int, str], int | None] = Field(default_factory=dict)


# ============================================================
# UTILIDADES
# ============================================================


def slugify(name: str) -> str:
    """Genera un slug válido para NetBox desde un nombre."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "-", slug)
    slug = slug.strip("-")
    # NetBox limita los slugs a 100 caracteres.
    return slug[:100]


def _generate_fallback_slug(base_slug: str, original_name: str) -> str:
    """Genera un slug de respaldo determinista usando un hash md5 corto."""
    hash_suffix = hashlib.md5(original_name.encode("utf-8")).hexdigest()[:4]
    return f"{base_slug}-{hash_suffix}"


def create_with_fallback_slug(
    endpoint: Endpoint,
    object_type_name: str,
    original_name: str,
    **kwargs: Any,
) -> Record:
    """
    Intenta crear un objeto en NetBox. Si ocurre colisión de slug (RequestError),
    genera un slug determinista de respaldo y reintenta la creación.
    """
    slug: str = kwargs.get("slug", "")
    try:
        return cast(Record, endpoint.create(**kwargs))
    except RequestError:
        slug_fallback = _generate_fallback_slug(slug, original_name)
        log.warning(
            "Slug '%s' colisionó al crear %s '%s'; reintentando con '%s'.",
            slug,
            object_type_name,
            original_name,
            slug_fallback,
        )
        kwargs["slug"] = slug_fallback
        try:
            return cast(Record, endpoint.create(**kwargs))
        except RequestError as e:
            raise NetBoxApiError(
                f"Imposible crear {object_type_name} '{original_name}' debido a colisión persistente "
                f"de slug o rechazo de NetBox: {e}"
            ) from e


def check_record_changes(
    record: Record,
    payload: NetBoxPayload,
) -> NetBoxPayload:
    """
    Determina qué campos cambiarían en el Record al aplicar el payload,
    sin persistir cambios ni realizar llamadas HTTP.
    Retorna un diccionario con las modificaciones detectadas.

    Nota: Utiliza Record._init_cache (API interna de pynetbox, verificado
    en v7.8.0) para clonar el estado original. Si la API interna cambia
    en una versión futura, el fallback asume conservadoramente que todos
    los campos del payload han cambiado.
    """
    try:
        temp = Record(dict(record._init_cache), record.api, record.endpoint)
    except (AttributeError, TypeError):
        log.debug(
            "Fallback en check_record_changes: _init_cache no disponible; "
            "se asume que todos los campos del payload han cambiado."
        )
        return dict(payload)

    for k, v in payload.items():
        setattr(temp, k, v)
    return temp.updates()


def parse_int(value: Any) -> int:
    """Convierte un valor a int, o lanza ValueError si no es convertible."""
    try:
        return int(str(value).strip())
    except (ValueError, TypeError) as e:
        raise ValueError("No es un número entero válido.") from e


def _to_float(value: Any) -> float:
    """Intenta parsear un valor a float, normalizando comas a puntos."""
    return float(str(value).strip().replace(",", "."))


def parse_float(value: Any) -> float:
    """Convierte un valor a float, o lanza ValueError si no es convertible."""
    try:
        return _to_float(value)
    except (ValueError, TypeError) as e:
        raise ValueError("No es un número decimal válido.") from e


def parse_float_to_int(value: Any) -> int:
    """Convierte un número (potencialmente float) a su entero más cercano."""
    try:
        val_float = _to_float(value)
        return round(val_float)
    except (ValueError, TypeError) as e:
        raise ValueError("No es un valor numérico válido.") from e


def parse_int_gb_to_mb(value: Any) -> int:
    """Convierte GB (string/float) a MB (entero). NetBox espera MB para memory."""
    try:
        gb = _to_float(value)
        return round(gb * 1024)
    except (ValueError, TypeError) as e:
        raise ValueError("No es un valor numérico válido.") from e


def parse_bool_si_no(value: Any) -> bool:
    """
    Convierte un valor a booleano según reglas específicas de 'si/no'.
    Lanza ValueError si no coincide.
    """
    v = str(value).strip().lower()
    if v in ("si", "sí", "yes", "true", "1"):
        return True
    if v in ("no", "false", "0"):
        return False
    raise ValueError("No es 'si' ni 'no'.")


def apply_cast(value: Any, cast_type: CastType, target: str) -> FieldValue:
    """Aplica un cast específico a un valor según la definición del campo."""
    try:
        match cast_type:
            case CastType.INT:
                return parse_int(value)
            case CastType.FLOAT:
                return parse_float(value)
            case CastType.FLOAT_TO_INT:
                return parse_float_to_int(value)
            case CastType.INT_GB_TO_MB:
                return parse_int_gb_to_mb(value)
            case CastType.BOOL_SI_NO:
                return parse_bool_si_no(value)
            case CastType.LOWER:
                return str(value).lower() if value is not None else value
    except ValueError as e:
        raise RowValidationError(
            f"Valor inválido '{value}' para el campo '{target}'. {e}"
        ) from e

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def concat_dot(parts: list[str], config: NetBoxMappingConfig) -> str:
    """Concatena partes no vacías con '. ' como separador."""
    clean = [p.strip() for p in parts if not config.is_empty(p)]
    return ". ".join(clean)


def extract_csv_value(
    row: CsvRow,
    col_alias: str,
    config: NetBoxMappingConfig,
    required: bool = False,
) -> str:
    """
    Extrae y sanitiza un valor del CSV usando su alias definido en el YAML.
    Si la celda está vacía (según config.is_empty) o la columna no existe o está
    mapeada a None, retorna un string vacío (""). Si 'required' es True y el
    valor resultante está vacío, levanta RowValidationError.
    Si el alias ni siquiera existe en la configuración, levanta ConfigValidationError.
    """
    if col_alias not in config.csv_columns:
        raise ConfigValidationError(
            f"Error crítico de configuración: El alias '{col_alias}' solicitado "
            "por el script no existe en `csv_columns` del archivo YAML."
        )

    col_name = config.csv_columns[col_alias].source
    if not col_name:
        if required:
            raise RowValidationError(
                f"El campo obligatorio '{col_alias}' no está mapeado en la configuración."
            )
        return ""

    val = row.get(col_name, "").strip()
    val = "" if config.is_empty(val) else val

    if required and not val:
        raise RowValidationError(
            f"El campo obligatorio '{col_alias}' está vacío en el CSV."
        )

    return val


def get_netbox_object_id(obj: NetBoxObject) -> int:
    """Retorna el ID de un objeto NetBox. Si el objeto es None, retorna 0."""
    obj_id = getattr(obj, "id", None)
    if obj_id is None:
        return 0
    return int(obj_id)


def get_node_type_from_object(obj: Endpoint | NetBoxObject) -> NodeType:
    """
    Retorna el tipo de nodo (virtual_machine o device) a partir de
    un Endpoint o NetBoxObject.
    """
    url = getattr(obj, "url", "")
    name = getattr(obj, "name", "")

    # Útil para Endpoints de pynetbox
    if "virtualization" in url or name == "virtual-machines":
        return NodeType.VIRTUAL_MACHINE

    # Si es un NetBoxObject tendrá el atributo del padre
    if hasattr(obj, "virtual_machine"):
        return NodeType.VIRTUAL_MACHINE

    return NodeType.DEVICE


def get_node_type_from_row(row: CsvRow, config: NetBoxMappingConfig) -> NodeType:
    """Extrae el tipo de máquina y lo mapea al tipo de nodo NetBox."""
    machine_type_val = extract_csv_value(row, "machine_type", config)
    return config.resolve_node_type(machine_type_val)


def resolve_mapping_path(args_mapping: str | None) -> Path:
    """Resuelve la ruta del archivo de configuración YAML."""
    if args_mapping:
        return Path(args_mapping)
    return Path(__file__).resolve().parent / "netbox_mapping.yaml"


def count_machine_names(
    rows: list[CsvRow], config: NetBoxMappingConfig
) -> Counter[str]:
    """Genera un conteo de las ocurrencias de nombres de máquinas en el CSV (ignorando vacíos)."""
    counts = Counter[str]()
    for row in rows:
        val = extract_csv_value(row, "machine_name", config)
        if isinstance(val, str):
            name = val.strip()
            if name:
                counts[name] += 1
    return counts


# ============================================================
# CARGA DE CONFIGURACIÓN
# ============================================================


def load_config(mapping_path: Path) -> NetBoxMappingConfig:
    """
    Carga y valida el archivo de mapping YAML utilizando Pydantic.
    Si hay errores de validación de sintaxis o de esquema, los reporta
    con detalle y termina la ejecución de manera controlada.
    """
    log.debug("Cargando configuración desde: %s", mapping_path)
    if not mapping_path.is_file():
        raise ConfigValidationError(
            f"No se encontró el archivo de mapping: {mapping_path}"
        )

    # Cargar el YAML
    try:
        with mapping_path.open("r", encoding="utf-8") as f:
            raw_yaml = f.read()
        # Expande sintaxis $VAR o ${VAR} usando variables de entorno
        expanded_yaml = os.path.expandvars(raw_yaml)
        raw = yaml.safe_load(expanded_yaml)
    except yaml.YAMLError:
        raise ConfigValidationError(f"Error sintáctico de YAML al leer {mapping_path}")

    if not isinstance(raw, dict):
        raise ConfigValidationError(
            f"El archivo de mapping {mapping_path} no contiene un diccionario YAML válido.",
        )

    # Validar el esquema Pydantic para el archivo de mapping
    try:
        return NetBoxMappingConfig.model_validate(raw)
    except ValidationError as exc:
        error_msgs = []
        for err in exc.errors():
            loc = " -> ".join(str(p) for p in err.get("loc", []))
            msg = err.get("msg", "")
            inp = err.get("input")
            error_msgs.append(f"[{loc}]: {msg} (valor recibido: {inp!r})")

        full_error = "\n".join(error_msgs)
        raise ConfigValidationError(
            f"Error de validación en el archivo de mapping YAML ({mapping_path}):\n{full_error}"
        )


def load_dotenv(env_path: Path | None = None) -> None:
    """
    Carga variables de entorno desde un archivo .env si existe.
    No sobrescribe variables que ya estén definidas en el entorno.
    """
    if env_path is None:
        env_path = Path(__file__).resolve().parent / ".env"

    if not env_path.is_file():
        return

    with env_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'\"")
                if key not in os.environ:
                    os.environ[key] = val


def load_env() -> tuple[str, str, bool]:
    """
    Carga las variables de entorno requeridas para la conexión con NetBox.

    - NETBOX_URL: URL de la API de NetBox (ej. http://netbox.empresa.com).
    - NETBOX_TOKEN: Token de autenticación con permisos write sobre dcim,
      virtualization, ipam, extras, core.
    - NETBOX_VERIFY_SSL: 'true' si se debe verificar el certificado SSL
      (por defecto), 'false' para saltar la verificación.
    """
    url = os.environ.get("NETBOX_URL", "").rstrip("/")
    token = os.environ.get("NETBOX_TOKEN", "")
    verify_ssl = os.environ.get("NETBOX_VERIFY_SSL", "true").lower() != "false"

    required_vars = {
        "NETBOX_URL": url,
        "NETBOX_TOKEN": token,
    }

    missing = [name for name, val in required_vars.items() if not val]
    if missing:
        missing_str = ", ".join(missing)
        raise ConfigValidationError(
            f"Variables de entorno requeridas no definidas: {missing_str}"
        )

    return url, token, verify_ssl


# ============================================================
# CLIENTE PYNETBOX
# ============================================================


def build_nb_client(url: str, token: str, verify_ssl: bool) -> Api:
    """
    Construye y retorna un cliente API de NetBox completamente configurado.

    La inicialización incluye: configuración de sesión HTTP (incluyendo
    deshabilitar SSL si se especifica), autenticación con el token, y una
    verificación inicial de conectividad contra el endpoint de sites.
    """
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    session = requests.Session()
    session.verify = verify_ssl

    nb = Api(url, token=token)
    nb.http_session = session

    # Verificar conectividad con una llamada liviana.
    try:
        nb.dcim.sites.filter(limit=1)
    except Exception as e:
        raise NetBoxApiError(f"No se pudo conectar con NetBox ({url})") from e

    log.info("Conectado a NetBox %s", url)
    return nb


def build_netbox_endpoints(nb: Api) -> NetBoxEndpoints:
    """
    Instancia y empaqueta de forma centralizada las referencias a los
    endpoints de pynetbox requeridos por el script en un contenedor
    inmutable (NetBoxEndpoints).

    La resolución de endpoints en pynetbox opera en memoria del lado
    del cliente (lazy). Falla con AttributeError si la versión de la
    librería pynetbox no expone alguna de las aplicaciones cliente
    principales (por ejemplo, 'core' en NetBox 4.x).
    """
    try:
        return NetBoxEndpoints(
            object_types=nb.core.object_types,
            custom_fields=nb.extras.custom_fields,
            choice_sets=nb.extras.custom_field_choice_sets,
            sites=nb.dcim.sites,
            cluster_types=nb.virtualization.cluster_types,
            manufacturers=nb.dcim.manufacturers,
            device_types=nb.dcim.device_types,
            platforms=nb.dcim.platforms,
            racks=nb.dcim.racks,
            clusters=nb.virtualization.clusters,
            device_roles=nb.dcim.device_roles,
            devices=nb.dcim.devices,
            virtual_machines=nb.virtualization.virtual_machines,
            device_interfaces=nb.dcim.interfaces,
            vm_interfaces=nb.virtualization.interfaces,
            ip_addresses=nb.ipam.ip_addresses,
            mac_addresses=nb.dcim.mac_addresses,
        )
    except AttributeError as e:
        raise NetBoxApiError(
            "La instancia de pynetbox no expone uno de los endpoints "
            "requeridos por el script"
        ) from e


# ============================================================
# LEER Y VALIDAR CSV
# ============================================================


def _read_csv(path: Path) -> tuple[list[str], list[CsvRow]]:
    """
    Lee merged_inventory.csv.
    Devuelve (headers, rows) donde cada row es {header: value}.
    """
    if not path.is_file():
        raise ConfigValidationError(f"No se encontró el CSV de entrada: {path}")

    rows: list[CsvRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        for row in reader:
            rows.append(dict(row))

    log.info("CSV leído: %d filas, %d columnas", len(rows), len(headers))
    return list(headers), rows


def _validate_csv_headers(
    headers: list[str],
    config: NetBoxMappingConfig,
) -> list[str]:
    """
    Valida que los encabezados del CSV incluyan todas las columnas obligatorias
    configuradas en 'csv_columns' con el flag 'required: true' dentro de netbox_mapping.yaml.
    Valida la *existencia de la columna en la cabecera*, no que cada fila
    deba tener un valor no vacío.
    Levanta ConfigValidationError si faltan columnas obligatorias.
    Retorna la lista de cabeceras intacta si la validación es exitosa.
    """
    header_set = set(headers)

    # 1. Validación de columnas críticas (ERROR bloqueante)
    missing_required = [
        col for col in sorted(config.get_required_columns()) if col not in header_set
    ]
    if missing_required:
        cols_str = ", ".join(repr(c) for c in missing_required)
        raise ConfigValidationError(
            f"El CSV no contiene las siguientes columnas obligatorias: {cols_str}"
        )

    # 2. Chequeo informativo de columnas esperadas (WARNING informativo)
    all_expected = config.get_all_expected_columns()
    missing_optional = [
        col
        for col in sorted(all_expected - config.get_required_columns())
        if col not in header_set
    ]
    if missing_optional:
        log.warning(
            "Columnas del mapping no encontradas en el CSV (se tratarán como vacías): %s",
            ", ".join(repr(c) for c in missing_optional),
        )

    return headers


def read_and_validate_csv(
    csv_path: Path,
    config: NetBoxMappingConfig,
) -> list[CsvRow]:
    """
    Orquesta la lectura del archivo CSV y la validación de sus cabeceras.
    Retorna únicamente las filas parseadas (diccionarios), listas para ser procesadas.
    """
    headers, rows = _read_csv(csv_path)
    _validate_csv_headers(headers, config)
    return rows


# ============================================================
# TAXONOMÍA: ensure_* (GET o CREATE)
# ============================================================


def ensure_site(
    endpoints: NetBoxEndpoints,
    site_cfg: SiteConfig,
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el Site definido en el YAML exista en NetBox."""
    name = site_cfg.name
    slug = cast(str, site_cfg.slug)

    results: list[Record] = list(endpoints.sites.filter(name=name))
    if results:
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía Site: %s", name)
        return MockNetBoxRecord(id=0, name=name, slug=slug)

    try:
        obj = cast(Record, endpoints.sites.create(name=name, slug=slug))
    except RequestError:
        raise NetBoxApiError(f"No se pudo crear el Site: {name}")

    log.info("Site creado: %s", name)
    return obj


def ensure_cluster_type(
    endpoints: NetBoxEndpoints,
    name: str,
    slug: str,
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el ClusterType exista en NetBox."""
    results: list[Record] = list(endpoints.cluster_types.filter(name=name))
    if results:
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía ClusterType: %s", name)
        return MockNetBoxRecord(id=0, name=name, slug=slug)

    try:
        obj = cast(Record, endpoints.cluster_types.create(name=name, slug=slug))
    except RequestError:
        raise NetBoxApiError(f"No se pudo crear el ClusterType: {name}")

    log.info("ClusterType creado: %s", name)
    return obj


def precompute_cluster_type_map(
    rows: list[CsvRow],
    config: NetBoxMappingConfig,
) -> dict[str, str]:
    """
    Pre-escaneo del CSV: asocia cada cluster_name con la tecnología
    del hipervisor (hypervisor_os) para determinar qué ClusterTypes
    crear dinámicamente.

    Solo procesa filas de tipo "Hipervisor". Si dos hipervisores del
    mismo clúster reportan distintos valores de SO, lanza un error
    explícito para forzar la corrección del maestro.

    Retorna un diccionario {cluster_name: hypervisor_os}.
    """
    cluster_type_map: dict[str, str] = {}

    for row in rows:
        machine_type = extract_csv_value(row, "machine_type", config)
        if machine_type != "Hipervisor":
            continue

        cluster_name = extract_csv_value(row, "cluster_name", config)
        hypervisor_os = extract_csv_value(row, "hypervisor_os", config)

        if not cluster_name or not hypervisor_os:
            continue

        existing_os = cluster_type_map.get(cluster_name)
        if existing_os is not None and existing_os != hypervisor_os:
            raise ConfigValidationError(
                f"Conflicto de SO en el clúster '{cluster_name}': "
                f"un hipervisor reporta '{existing_os}' y otro '{hypervisor_os}'. "
                "Corrija el inventario maestro."
            )

        cluster_type_map[cluster_name] = hypervisor_os

    if cluster_type_map:
        log.info(
            "Pre-escaneo: %d clúster(es) asociados a ClusterType dinámico.",
            len(cluster_type_map),
        )

    return cluster_type_map


def ensure_dynamic_cluster_types(
    endpoints: NetBoxEndpoints,
    cluster_type_map: dict[str, str],
    fallback_cfg: ClusterTypeConfig,
    cache: dict[str, NetBoxObject],
    dry_run: bool,
) -> NetBoxObject:
    """
    Crea los ClusterTypes dinámicos en NetBox a partir de los valores
    únicos de hypervisor_os y los almacena en el caché.

    Siempre garantiza el fallback estático del YAML como respaldo
    para clústeres sin información de SO.

    Retorna el ClusterType fallback.
    """
    # Garantizar el fallback estático del YAML.
    fallback_name = fallback_cfg.default.name
    fallback_slug = cast(str, fallback_cfg.default.slug)
    fallback = ensure_cluster_type(endpoints, fallback_name, fallback_slug, dry_run)
    cache[fallback_name] = fallback

    # Crear ClusterTypes dinámicos (uno por cada SO único).
    unique_os_names: set[str] = set(cluster_type_map.values())
    for os_name in sorted(unique_os_names):
        if os_name in cache:
            continue
        os_slug = slugify(os_name)
        ct = ensure_cluster_type(endpoints, os_name, os_slug, dry_run)
        cache[os_name] = ct

    return fallback


def ensure_manufacturer(
    manufacturers_endpoint: Endpoint,
    name: str,
    cache: dict[str, NetBoxObject],
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el Manufacturer exista en NetBox."""
    if name in cache:
        return cache[name]

    results: list[Record] = list(manufacturers_endpoint.filter(name=name))
    if results:
        cache[name] = results[0]
        return results[0]

    # Búsqueda preventiva por slug: si "H.P." genera slug "hp" y ya existe
    # un Manufacturer con ese slug (ej. "HP"), se reutiliza para deduplicar.
    slug = slugify(name)
    slug_results: list[Record] = list(manufacturers_endpoint.filter(slug=slug))
    if slug_results:
        log.warning(
            "Manufacturer '%s' no existe, pero su slug '%s' coincide con '%s'. "
            "Se reutiliza el objeto existente.",
            name,
            slug,
            getattr(slug_results[0], "name", "?"),
        )
        cache[name] = slug_results[0]
        return slug_results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía Manufacturer: %s", name)
        obj: NetBoxObject = MockNetBoxRecord(id=0, name=name)
        cache[name] = obj
        return obj

    obj = create_with_fallback_slug(
        manufacturers_endpoint,
        "Manufacturer",
        name,
        name=name,
        slug=slug,
    )
    log.info("Manufacturer creado: %s", name)
    cache[name] = obj
    return obj


def _sync_device_type_u_height(
    existing_dt: Record,
    model: str,
    target_height: float,
    dry_run: bool,
) -> Record:
    """
    Sincroniza la altura en U del modelo de servidor.
    Retorna el objeto DeviceType modificado (o intacto).
    """
    target_height_val = target_height or 1.0
    current_height = float(getattr(existing_dt, "u_height", 1) or 1)

    if current_height == float(target_height_val):
        return existing_dt

    if dry_run:
        log.info(
            "[DRY-RUN] Actualizaría u_height de DeviceType '%s' (de %g a %g)",
            model,
            current_height,
            target_height_val,
        )
        return existing_dt

    try:
        existing_dt.update({"u_height": target_height_val})
        log.info(
            "DeviceType '%s' u_height actualizado a %g",
            model,
            target_height_val,
        )
        return existing_dt
    except RequestError as e:
        raise NetBoxApiError(
            f"Error actualizando u_height de DeviceType '{model}': {e}"
        ) from e


def _create_device_type(
    endpoint: Endpoint,
    model: str,
    slug: str,
    manufacturer_id: int,
    u_height: float,
    manufacturer_name: str,
) -> Record:
    """Intenta crear el DeviceType, manejando colisiones de slug."""
    u_height_val = u_height or 1.0
    return create_with_fallback_slug(
        endpoint,
        "DeviceType",
        f"{manufacturer_name}/{model}",
        model=model,
        slug=slug,
        manufacturer=manufacturer_id,
        u_height=u_height_val,
    )


def ensure_device_type(
    device_types_endpoint: Endpoint,
    manufacturer: NetBoxObject,
    model: str,
    u_height: float,
    cache: dict[tuple[str, str], NetBoxObject],
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el DeviceType exista en NetBox."""
    manufacturer_id = get_netbox_object_id(manufacturer)

    # El ID numérico se retiene solo como fallback.
    manufacturer_name = str(getattr(manufacturer, "name", manufacturer_id))
    key = (manufacturer_name, model)
    if key in cache:
        return cache[key]

    if manufacturer_id == 0:
        results = []
    else:
        results = list(
            device_types_endpoint.filter(model=model, manufacturer_id=manufacturer_id)
        )
    if results:
        existing_dt = _sync_device_type_u_height(results[0], model, u_height, dry_run)
        cache[key] = existing_dt
        return existing_dt

    if dry_run:
        log.info("[DRY-RUN] Crearía DeviceType: %s / %s", manufacturer_name, model)
        obj = MockNetBoxRecord(id=0, model=model)
        cache[key] = obj
        return obj

    # Prefijamos el slug con el nombre del fabricante para evitar colisiones
    # entre modelos homónimos de distintas marcas (ej. "PowerEdge" de Dell vs HP).
    slug = slugify(f"{manufacturer_name} {model}")

    # Búsqueda preventiva por slug.
    slug_results: list[Record] = list(device_types_endpoint.filter(slug=slug))
    if slug_results:
        log.warning(
            "DeviceType '%s/%s' no existe por nombre, pero su slug '%s' coincide "
            "con un DeviceType existente. Se reutiliza.",
            manufacturer_name,
            model,
            slug,
        )
        cache[key] = slug_results[0]
        return slug_results[0]

    obj = _create_device_type(
        device_types_endpoint,
        model,
        slug,
        manufacturer_id,
        u_height,
        manufacturer_name,
    )
    log.info("DeviceType creado: %s / %s", manufacturer_name, model)
    cache[key] = obj
    return obj


def ensure_platform(
    platforms_endpoint: Endpoint,
    name: str,
    cache: dict[str, NetBoxObject],
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el Platform exista en NetBox."""
    if name in cache:
        return cache[name]

    results: list[Record] = list(platforms_endpoint.filter(name=name))
    if results:
        cache[name] = results[0]
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía Platform: %s", name)
        obj: NetBoxObject = MockNetBoxRecord(id=0, name=name)
        cache[name] = obj
        return obj

    # Búsqueda preventiva por slug para deduplicar variantes tipográficas.
    slug = slugify(name)
    slug_results: list[Record] = list(platforms_endpoint.filter(slug=slug))
    if slug_results:
        log.warning(
            "Platform '%s' no existe, pero su slug '%s' coincide con '%s'. "
            "Se reutiliza el objeto existente.",
            name,
            slug,
            getattr(slug_results[0], "name", "?"),
        )
        cache[name] = slug_results[0]
        return slug_results[0]

    obj = create_with_fallback_slug(
        platforms_endpoint,
        "Platform",
        name,
        name=name,
        slug=slug,
    )
    log.info("Platform creado: %s", name)
    cache[name] = obj
    return obj


def ensure_rack(
    racks_endpoint: Endpoint,
    name: str,
    site: NetBoxObject,
    cache: dict[tuple[int, str], NetBoxObject],
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el Rack exista en NetBox."""
    site_id = get_netbox_object_id(site)
    cache_key = (site_id, name)

    if cache_key in cache:
        return cache[cache_key]

    if site_id == 0:
        results = []
    else:
        results = list(racks_endpoint.filter(name=name, site_id=site_id))
    if results:
        cache[cache_key] = results[0]
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía Rack: %s", name)
        obj: NetBoxObject = MockNetBoxRecord(id=0, name=name)
        cache[cache_key] = obj
        return obj

    try:
        obj = cast(Record, racks_endpoint.create(name=name, site=site_id))
    except RequestError as e:
        raise NetBoxApiError(f"No se pudo crear el Rack '{name}': {e}") from e
    log.info("Rack creado: %s", name)
    cache[cache_key] = obj
    return obj


def ensure_cluster(
    clusters_endpoint: Endpoint,
    name: str,
    cluster_type: NetBoxObject,
    site: NetBoxObject,
    cache: dict[tuple[int, str], NetBoxObject],
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el Cluster exista en NetBox."""
    site_id = get_netbox_object_id(site)
    cache_key = (site_id, name)

    if cache_key in cache:
        return cache[cache_key]

    if site_id == 0:
        results = []
    else:
        results = list(clusters_endpoint.filter(name=name, site_id=site_id))
    if results:
        cache[cache_key] = results[0]
        return results[0]

    cluster_type_id = get_netbox_object_id(cluster_type)

    if dry_run:
        log.info("[DRY-RUN] Crearía Cluster: %s", name)
        obj: NetBoxObject = MockNetBoxRecord(id=0, name=name)
        cache[cache_key] = obj
        return obj

    try:
        obj = cast(
            Record,
            clusters_endpoint.create(
                name=name,
                type=cluster_type_id,
                site=site_id,
            ),
        )
    except RequestError as e:
        raise NetBoxApiError(f"No se pudo crear el Cluster '{name}': {e}") from e
    log.info("Cluster creado: %s", name)
    cache[cache_key] = obj
    return obj


def _sync_single_device_role(
    endpoints: NetBoxEndpoints,
    role_def: DeviceRoleConfig,
    device_roles_cache: dict[str, NetBoxObject],
    dry_run: bool,
) -> NetBoxObject:
    """
    Sincroniza un único DeviceRole y asegura que permita VMs.
    Retorna el objeto DeviceRole.
    """
    name = role_def.name
    key = name.lower()

    if key in device_roles_cache:
        return device_roles_cache[key]

    results: list[Record] = list(endpoints.device_roles.filter(name=name))
    if results:
        role_obj = results[0]
        if not getattr(role_obj, "vm_role", False):
            if dry_run:
                log.info("[DRY-RUN] Actualizaría DeviceRole para permitir VM: %s", name)
            else:
                try:
                    role_obj.update({"vm_role": True})
                    log.info("DeviceRole actualizado para permitir VM: %s", name)
                except RequestError as e:
                    raise NetBoxApiError(
                        f"Error actualizando DeviceRole '{name}': {e}"
                    ) from e

        device_roles_cache[key] = role_obj
        return role_obj

    if dry_run:
        log.info("[DRY-RUN] Crearía DeviceRole: %s", name)
        obj_mock = MockNetBoxRecord(id=0, name=name, vm_role=True)
        device_roles_cache[key] = obj_mock
        return obj_mock

    try:
        slug = cast(str, role_def.slug)
        obj = cast(
            Record,
            endpoints.device_roles.create(
                name=name,
                slug=slug,
                color=role_def.color,
                vm_role=True,
            ),
        )
    except RequestError as e:
        raise ConfigValidationError(f"Error creando DeviceRole '{name}': {e}") from e
    log.info("DeviceRole creado: %s", name)
    device_roles_cache[key] = obj
    return obj


def ensure_all_device_roles(
    endpoints: NetBoxEndpoints,
    device_roles: list[DeviceRoleConfig],
    device_roles_cache: dict[str, NetBoxObject],
    dry_run: bool,
) -> dict[str, NetBoxObject]:
    """
    Garantiza que todos los device roles definidos en el YAML
    existen en NetBox (/api/dcim/device-roles/).
    Todos los roles se habilitan para su uso en Virtual Machines.
    Puebla caches.device_roles con {nombre_lower: objeto}.
    Retorna un diccionario con los DeviceRoles sincronizados.
    """
    ensured_roles: dict[str, NetBoxObject] = {}
    for role_def in device_roles:
        key = role_def.name.lower()
        ensured_roles[key] = _sync_single_device_role(
            endpoints, role_def, device_roles_cache, dry_run
        )
    return ensured_roles


# ============================================================
# CUSTOM FIELDS: ensure_custom_fields
# ============================================================


def _get_choice_set_choices(choices: list[ChoiceItemConfig]) -> list[list[str]]:
    """Obtiene las opciones de un choice set, ya sea de tipo lista de listas o iterable."""
    return [[choice.value, choice.label] for choice in choices]


def _normalize_choices(extra_choices: Any) -> list[list[str]]:
    """Convierte las opciones de un choice set a una lista de listas de strings."""
    if not isinstance(extra_choices, (list, tuple)):
        return []

    return [
        [str(choice[0]), str(choice[1])]
        for choice in extra_choices
        if isinstance(choice, (list, tuple)) and len(choice) >= 2
    ]


def _ensure_choice_set(
    choice_sets_endpoint: Endpoint,
    existing_choice_sets: dict[str, Record],
    choice_set_cfg: ChoiceSetConfig,
    dry_run: bool,
) -> int:
    """Crea un choice set si no existe en NetBox."""
    choice_set_name: str = choice_set_cfg.name
    choices: list[list[str]] = _get_choice_set_choices(choice_set_cfg.choices)
    choice_set = existing_choice_sets.get(choice_set_name)

    def _get_choice_set_id(choice_set: Record) -> int:
        return cast(int, getattr(choice_set, "id", 0))

    if choice_set is None:
        if dry_run:
            log.info(
                "[DRY-RUN] Crearía Choice Set: %s",
                choice_set_name,
            )
            return 0

        try:
            choice_set = cast(
                Record,
                choice_sets_endpoint.create(
                    name=choice_set_name,
                    extra_choices=choices,
                    order_alphabetically=False,
                ),
            )
        except RequestError as e:
            raise ConfigValidationError(
                f"Error al crear Choice Set '{choice_set_name}': {e}"
            ) from e

        existing_choice_sets[choice_set_name] = choice_set
        log.info(
            "Choice Set creado: %s",
            choice_set_name,
        )
        return _get_choice_set_id(choice_set)

    if dry_run:
        log.info("[DRY-RUN] Actualizaría Choice Set: %s", choice_set_name)
        return _get_choice_set_id(choice_set)

    extra_choices: Any = getattr(choice_set, "extra_choices", None)
    current_choices: list[list[str]] = _normalize_choices(extra_choices)

    if current_choices == choices:
        return _get_choice_set_id(choice_set)

    try:
        choice_set.update(
            {
                "extra_choices": choices,
                "order_alphabetically": False,
            }
        )
        log.info("Choice Set actualizado: %s", choice_set_name)
    except RequestError as e:
        raise ConfigValidationError(
            f"Error al actualizar Choice Set '{choice_set_name}': {e}"
        ) from e

    return _get_choice_set_id(choice_set)


def _ensure_custom_field(
    custom_fields_endpoint: Endpoint,
    existing_cfs: dict[str, Record],
    cf_def: CustomFieldConfig,
    object_types: list[str],
    choice_set_id: int | None,
    dry_run: bool,
) -> NetBoxObject:
    """
    Crea un custom field si no existe en NetBox.
    Retorna el objeto Custom Field.
    """
    name: str = cf_def.name

    if name in existing_cfs:
        log.debug(
            "Custom field ya existe: %s",
            name,
        )
        return existing_cfs[name]

    if dry_run:
        log.info(
            "[DRY-RUN] Crearía custom field: %s (%s)",
            name,
            cf_def.type,
        )
        return MockNetBoxRecord(id=0, name=name, type=cf_def.type)

    create_kwargs: NetBoxPayload = {
        "name": name,
        "label": cf_def.label or name,
        "type": cf_def.type,
        "required": cf_def.required,
        "object_types": object_types,
    }

    if choice_set_id is not None:
        create_kwargs["choice_set"] = choice_set_id

    default_value: FieldValue = cf_def.default
    if default_value is not None:
        create_kwargs["default"] = default_value

    try:
        created_cf = cast(Record, custom_fields_endpoint.create(**create_kwargs))
    except RequestError as e:
        raise ConfigValidationError(f"Error al crear custom field '{name}': {e}") from e

    existing_cfs[name] = created_cf
    log.info("Custom field creado: %s", name)
    return created_cf


def ensure_custom_fields(
    endpoints: NetBoxEndpoints,
    cfg: NetBoxMappingConfig,
    dry_run: bool,
) -> dict[str, NetBoxObject]:
    """
    Garantiza que todos los custom fields definidos en el YAML
    existan en NetBox.

    Además de los custom fields definidos en 'custom_fields',
    procesa las definiciones especiales:
        - machine_type
        - environment

    Para los custom fields de tipo 'select', garantiza también
    la existencia del Choice Set asociado.

    NetBox 4.5+:
    - Los Object Types se consultan mediante /api/core/object-types/.
    - El endpoint /api/extras/object-types/ fue eliminado en NetBox 4.5.
    - Los Choice Sets se gestionan mediante
    /api/extras/custom-field-choice-sets/.

    Retorna un diccionario mapeando el nombre del Custom Field a su objeto en NetBox.
    """
    # Obtener Custom Fields y Choice Sets existentes.
    custom_fields = cast(list[Record], endpoints.custom_fields.all())
    choice_sets = cast(list[Record], endpoints.choice_sets.all())

    existing_cfs: dict[str, Record] = {str(cf.name): cf for cf in custom_fields}

    existing_choice_sets: dict[str, Record] = {
        str(ch_set.name): ch_set for ch_set in choice_sets
    }

    # Obtener lista unificada de definiciones de Custom Field O(1).
    cf_definitions: list[CustomFieldConfig] = cfg.get_all_custom_field_defs()

    ensured_cfs: dict[str, NetBoxObject] = {}

    for cf_def in cf_definitions:
        choice_set_id: int | None = None
        choice_set_cfg: ChoiceSetConfig | None = cf_def.choice_set

        if cf_def.type == "select" and choice_set_cfg:
            choice_set_id = _ensure_choice_set(
                endpoints.choice_sets,
                existing_choice_sets,
                choice_set_cfg,
                dry_run,
            )

        # Crear el Custom Field si no existe.
        cf_obj = _ensure_custom_field(
            endpoints.custom_fields,
            existing_cfs,
            cf_def,
            cf_def.object_types,
            choice_set_id,
            dry_run,
        )
        ensured_cfs[cf_def.name] = cf_obj

    return ensured_cfs


# ============================================================
# CONSTRUCCIÓN DE PAYLOAD
# ============================================================


def _resolve_default_or_empty(
    default: FieldValue,
    is_optional: bool,
    target: str,
) -> FieldValue:
    """Centraliza la política de fallback (default > None > error)."""
    if default is not None:
        return default
    if is_optional:
        return None
    raise RowValidationError(f"El campo obligatorio '{target}' está vacío en el CSV.")


def _extract_concat_dot_value(
    row: CsvRow,
    source: list[str],
    config: NetBoxMappingConfig,
) -> str:
    """Resuelve un campo multi-columna vía concatenación con punto."""
    parts = [row.get(s, "") for s in source]
    return concat_dot(parts, config)


def _extract_coalesce_value(
    row: CsvRow,
    source: list[str],
    config: NetBoxMappingConfig,
) -> str:
    """
    Resuelve un campo multi-columna mediante coalesce.
    Falla rápidamente si más de un campo contiene un valor.
    Devuelve el primer valor encontrado, o un string vacío si ninguno tiene valor.
    """
    values = []
    for s in source:
        val = row.get(s, "").strip()
        if not config.is_empty(val):
            values.append(val)

    if len(values) > 1:
        raise RowValidationError(
            f"Conflicto: Múltiples valores no nulos {values} para un campo coalesce "
            f"provenientes de las columnas {source}."
        )

    return values[0] if values else ""


def _extract_raw_source_value(row: CsvRow, source: str | list[str]) -> str:
    """Extrae el valor crudo de la(s) columna(s) origen, sin transformar."""
    if isinstance(source, str):
        return row.get(source, "")
    return row.get(source[0], "") if source else ""


def _validate_select_choice(
    value: FieldValue,
    custom_field_def: CustomFieldConfig | None,
    target: str,
    is_optional: bool,
) -> FieldValue:
    """
    Valida value contra choice_set cuando el Custom Field es de tipo
    'select' o 'multiselect'. No-op para cualquier otro tipo de campo.
    """
    if not (
        custom_field_def
        and custom_field_def.type in ("select", "multiselect")
        and custom_field_def.choice_set
    ):
        return value

    valid_choices = [c.value for c in custom_field_def.choice_set.choices]

    if custom_field_def.type == "multiselect":
        parts = [p.strip() for p in str(value).split(",") if p.strip()]
        invalid_parts = [p for p in parts if p not in valid_choices]

        if not invalid_parts:
            return parts

        log.warning(
            "Valores %s no son válidos para el Custom Field multiselect '%s'. Opciones válidas: %s.",
            invalid_parts,
            target,
            valid_choices,
        )
        if is_optional:
            return None
        raise RowValidationError(
            f"Valores inválidos '{invalid_parts}' para el campo requerido '{target}'."
        )

    if value in valid_choices:
        return value

    log.warning(
        "Valor '%s' no es válido para el Custom Field '%s'. Opciones válidas: %s.",
        value,
        target,
        valid_choices,
    )
    if is_optional:
        return None
    raise RowValidationError(
        f"Valor inválido '{value}' para el campo requerido '{target}'."
    )


def _resolve_field_value(
    row: CsvRow,
    field_def: FieldMappingConfig,
    config: NetBoxMappingConfig,
    is_optional: bool,
    default: FieldValue = None,
    custom_field_def: CustomFieldConfig | None = None,
) -> FieldValue:
    """
    Interpreta un `FieldMappingConfig` sobre una fila del CSV para devolver el valor final.
    Maneja concatenaciones y campos simples. Aplica el patrón "Pipe and Filter",
    delegando cada una a una función de responsabilidad única; corta
    temprano (fail-fast) en cuanto una etapa determina el valor final.
    """
    source = field_def.source
    target = field_def.target

    # Ruta independiente: multi-columna con transformador (concat_dot, coalesce)
    if isinstance(source, list):
        if field_def.transform == "concat_dot":
            value = _extract_concat_dot_value(row, source, config)
            if value:
                return value
            return _resolve_default_or_empty(default, is_optional, target)
        elif field_def.transform == "coalesce":
            value = _extract_coalesce_value(row, source, config)
            if value:
                return value
            return _resolve_default_or_empty(default, is_optional, target)

    raw_value = _extract_raw_source_value(row, source)
    if config.is_empty(raw_value):
        return _resolve_default_or_empty(default, is_optional, target)

    value: FieldValue = raw_value

    if isinstance(source, str):
        value = config.map_value_by_source(source, raw_value, strict=True)

    value = _validate_select_choice(value, custom_field_def, target, is_optional)

    if field_def.cast:
        value = apply_cast(value, field_def.cast, target)

    return value


def build_payload(
    row: CsvRow,
    native_maps: list[FieldMappingConfig],
    custom_maps: list[FieldMappingConfig],
    config: NetBoxMappingConfig,
) -> NetBoxPayload:
    """
    Construye de forma dinámica el payload para enviar a la API de NetBox,
    basándose estrictamente en las reglas de mapeo (DML) definidas en el archivo YAML.

    Esta función es el núcleo del dinamismo del script, ya que abstrae la lógica de
    extracción, mapeo de valores, casteos y validaciones, aislando el código Python
    de las reglas de negocio específicas del CSV.

    - Los `native_maps` se inyectan en el primer nivel (raíz) del diccionario payload.
    - Los `custom_maps` se agrupan y empaquetan dentro de la clave "custom_fields".
    - Los campos resultantes con valor `None` (ej. celdas vacías donde `is_optional=True`)
      se excluyen proactivamente del payload para evitar borrar datos preexistentes
      o violar constraints en NetBox.
    """
    payload: NetBoxPayload = {}
    cf_payload: CustomFieldsPayload = {}

    def _assign_if_valid(
        target_dict: dict[str, Any], fd: FieldMappingConfig, val: FieldValue
    ) -> None:
        """Sanitiza y asigna el valor al payload solo si es válido."""
        # Sanitización dinámica de constraints UNIQUE dictadas por el YAML.
        if fd.is_unique and val == "":
            val = None

        if val is not None:
            target_dict[fd.target] = val

    for fd in native_maps:
        value = _resolve_field_value(
            row,
            fd,
            config,
            is_optional=not fd.required,
            default=None,
            custom_field_def=None,
        )

        _assign_if_valid(payload, fd, value)

    for fd in custom_maps:
        cf_def = config.get_custom_field_def(fd.target)
        if cf_def is None:
            raise ConfigValidationError(
                f"Error crítico de configuración: El custom_mapping target "
                f"'{fd.target}' no está definido en custom_field_definitions."
            )

        value = _resolve_field_value(
            row,
            fd,
            config,
            is_optional=not cf_def.required,
            default=cf_def.default,
            custom_field_def=cf_def,
        )

        _assign_if_valid(cf_payload, fd, value)

    # Agregar los custom fields al payload
    if cf_payload:
        payload["custom_fields"] = cf_payload

    return payload


# ============================================================
# RESOLVERS GENERALES (PARA SINCRONIZACIÓN DE OBJETOS)
# ============================================================


def _resolve_netbox_status(
    row: CsvRow, config: NetBoxMappingConfig, node_type: NodeType
) -> str:
    """
    Resuelve el status NetBox a partir de la columna 'Estado'.

    Si el valor no existe en status_map, se aplica el fallback
    correspondiente al tipo de nodo.
    """
    status_csv = extract_csv_value(row, "status", config)

    status_mapped = config.map_value("status", status_csv, strict=False)
    if status_mapped is not None:
        return status_mapped

    node_cfg = config.node_types.get_config(node_type)
    return node_cfg.status.default


def _resolve_platform(
    plt_endpoint: Endpoint,
    row: CsvRow,
    plt_cache: dict[str, NetBoxObject],
    dry_run: bool,
    config: NetBoxMappingConfig,
) -> int | None:
    """Resuelve el Platform desde la columna OS y retorna su ID (si existe)."""
    plt_name = extract_csv_value(row, "os", config)
    if not plt_name:
        return None

    platform = ensure_platform(
        plt_endpoint,
        plt_name,
        plt_cache,
        dry_run,
    )
    return get_netbox_object_id(platform)


def _resolve_cluster(
    cluster_endpoint: Endpoint,
    row: CsvRow,
    cluster_type_map: dict[str, str],
    cluster_type_cache: dict[str, NetBoxObject],
    fallback_cluster_type: NetBoxObject,
    site: NetBoxObject,
    cluster_cache: dict[tuple[int, str], NetBoxObject],
    dry_run: bool,
    config: NetBoxMappingConfig,
) -> int | None:
    """
    Resuelve el Cluster desde la columna correspondiente y retorna su ID.

    Determina el ClusterType correcto usando el mapa pre-computado
    (cluster_name → hypervisor_os → ClusterType). Si el clúster no
    tiene una tecnología asociada, usa el fallback genérico del YAML.
    """
    cluster_name = extract_csv_value(row, "cluster_name", config)
    if not cluster_name:
        return None

    # Resolver el ClusterType correcto para este clúster.
    os_name = cluster_type_map.get(cluster_name)
    cluster_type = cluster_type_cache[os_name] if os_name else fallback_cluster_type

    cluster = ensure_cluster(
        cluster_endpoint,
        cluster_name,
        cluster_type,
        site,
        cluster_cache,
        dry_run,
    )
    return get_netbox_object_id(cluster)


def _resolve_device_type(
    endpoints: NetBoxEndpoints,
    row: CsvRow,
    caches: CacheStore,
    config: NetBoxMappingConfig,
    dry_run: bool,
) -> int:
    """
    Resuelve y retorna el ID del DeviceType utilizando el manufacturer y el modelo.
    Si la altura ('alt_u') no se proporciona o es inválida, asume 1 por defecto.
    """
    manufacturer = extract_csv_value(row, "manufacturer", config)
    model = extract_csv_value(row, "model", config)

    # Manufacturer.
    manufacturer_obj = ensure_manufacturer(
        endpoints.manufacturers,
        manufacturer,
        caches.manufacturers,
        dry_run,
    )

    raw_u_height = extract_csv_value(row, "alt_u", config)
    if not raw_u_height:
        u_height = 1.0
    else:
        try:
            u_height = parse_float(raw_u_height) or 1.0
        except ValueError:
            raise RowValidationError(
                f"Valor numérico inválido '{raw_u_height}' para 'alt_u'."
            )

    device_type = ensure_device_type(
        endpoints.device_types,
        manufacturer_obj,
        model,
        u_height,
        caches.device_types,
        dry_run,
    )
    return get_netbox_object_id(device_type)


def _resolve_host_device(
    devices_endpoint: Endpoint,
    row: CsvRow,
    site: NetBoxObject,
    machine_name: str,
    cache: dict[tuple[int, str], int | None],
    config: NetBoxMappingConfig,
) -> int | None:
    """
    Resuelve y cachea el ID del Device correspondiente al hipervisor host,
    acotado estrictamente al site configurado.
    """
    host_name_csv = extract_csv_value(row, "host_device", config)
    if not host_name_csv:
        return None

    site_id = get_netbox_object_id(site)

    cache_key = (site_id, host_name_csv)
    if cache_key in cache:
        dev_id = cache[cache_key]
        if dev_id is None:
            log.warning(
                "ADVERTENCIA (%s): El dispositivo host '%s' no se encontró en el site. "
                "La VM se creará sin asignación de host.",
                machine_name,
                host_name_csv,
            )
        return dev_id

    try:
        if site_id == 0:
            host_devices = []
        else:
            host_devices = list(
                devices_endpoint.filter(name=host_name_csv, site_id=site_id)
            )
        if not host_devices:
            log.warning(
                "ADVERTENCIA (%s): El dispositivo host '%s' no se encontró en el site. "
                "La VM se creará sin asignación de host.",
                machine_name,
                host_name_csv,
            )
            cache[cache_key] = None
            return None

        dev_id = get_netbox_object_id(host_devices[0])
        cache[cache_key] = dev_id
        return dev_id
    except Exception as e:
        raise NetBoxApiError(
            f"ERROR ({machine_name}): falló la consulta del host_device '{host_name_csv}' "
            f"en Site (ID: {site_id}): {e}"
        ) from e


def _resolve_device_role(
    row: CsvRow,
    roles_cache: dict[str, NetBoxObject],
    config: NetBoxMappingConfig,
) -> int:
    """
    Busca un DeviceRole por nombre (insensible a mayúsculas) y retorna su ID.
    Si el nombre está vacío o no existe, utiliza "Others" como fallback.
    Lanza RowValidationError si el fallback "Others" tampoco existe.
    """
    role_csv = extract_csv_value(row, "role", config)
    role_obj = None

    if role_csv:
        role_obj = roles_cache.get(role_csv.lower())

    if not role_obj:
        role_obj = roles_cache.get("others")

    if not role_obj:
        machine_name = extract_csv_value(row, "machine_name", config) or "?"
        raise RowValidationError(
            f"No existe el DeviceRole 'Others' en NetBox para asignar como "
            f"fallback a la máquina '{machine_name}'."
        )

    return get_netbox_object_id(role_obj)


# ============================================================
# SINCRONIZACIÓN DE OBJETOS (DEVICES y VMS)
# ============================================================


def _find_existing_object(
    uuid: str,
    machine_name: str,
    endpoint: Endpoint,
    config: NetBoxMappingConfig,
) -> tuple[list[Record], bool, bool]:
    """
    Busca un objeto primero por UUID y luego por nombre.

    Retorna:
        (objetos_encontrados, encontrado_por_uuid, encontrado_por_nombre)

    Política:
    - UUID presente + encontrado por UUID -> usar resultado UUID.
    - UUID presente + no encontrado por UUID -> buscar por nombre.
    - UUID vacío -> buscar directamente por nombre.
    """
    has_uuid = not config.is_empty(uuid)

    # 1. Búsqueda por API directa con UUID
    if has_uuid:
        existing_by_uuid: list[Record] = list(endpoint.filter(cf_inventory_uuid=uuid))
        if existing_by_uuid:
            return existing_by_uuid, True, False

    # 2. Búsqueda por Nombre (Fallback)
    existing_by_name: list[Record] = list(endpoint.filter(name=machine_name))
    if not existing_by_name:
        return [], False, False

    # 3. Inspección local de UUID en los resultados por nombre
    if has_uuid:
        for obj in existing_by_name:
            cf: dict[str, Any] = getattr(obj, "custom_fields", {}) or {}
            obj_uuid = str(cf.get("inventory_uuid") or "").strip().lower()
            if obj_uuid == uuid:
                return [obj], True, False

    # 4. Encontrado únicamente por nombre
    return existing_by_name, False, True


def _is_name_safely_unique(
    endpoint: Endpoint,
    existing: list[Record],
    machine_name: str,
    csv_name_counts: Counter[str],
) -> bool:
    """
    Valida que un nombre de máquina sea único tanto en NetBox
    como en el CSV, condición necesaria para permitir una
    actualización segura cuando la coincidencia es solo por nombre.

    Retorna True si el nombre es único en ambos sistemas.
    """
    node_type = get_node_type_from_object(endpoint)
    nb_count = len(existing)
    csv_count = csv_name_counts.get(machine_name, 0)
    if nb_count == 1 and csv_count == 1:
        return True
    log.warning(
        "SKIP %s '%s': coincidencia por nombre, pero no es único "
        "(NetBox=%d, CSV=%d). Se requiere UUID para sincronizar.",
        node_type,
        machine_name,
        nb_count,
        csv_count,
    )
    return False


def _execute_sync(
    endpoint: Endpoint,
    payload: NetBoxPayload,
    existing: list[Record],
    machine_name: str,
    raw_uuid: str,
    dry_run: bool,
) -> SyncResult:
    """
    Ejecuta CREATE, UPDATE, UNCHANGED o DRY-RUN según el objeto encontrado.

    Si una fila llega a esta función con `existing`, es porque superó
    la validación de unicidad previa, por lo que se actualiza con confianza.

    Retorna una tupla (SyncStatus, obj_id) donde obj_id es el ID del
    objeto en NetBox (int).
    En modo dry-run con objetos existentes se retorna su ID real;
    en creación dry-run se retorna 0 (mock).
    """
    uuid = raw_uuid or "N/A"
    node_type = get_node_type_from_object(endpoint)

    if dry_run:
        if not existing:
            log.info(
                "[DRY-RUN] Crearía %s: %s (UUID=%s)",
                node_type,
                machine_name,
                uuid,
            )
            return SyncStatus.CREATED, 0, None

        existing_id = get_netbox_object_id(existing[0])
        diff = check_record_changes(existing[0], payload)
        if diff:
            log.info(
                "[DRY-RUN] Actualizaría %s: %s (UUID=%s) - Cambios: %s",
                node_type,
                machine_name,
                uuid,
                list(diff.keys()),
            )
            return SyncStatus.UPDATED, existing_id, existing[0]

        log.info(
            "[DRY-RUN] UNCHANGED %s: %s (UUID=%s)",
            node_type,
            machine_name,
            uuid,
        )
        return SyncStatus.UNCHANGED, existing_id, existing[0]

    if not existing:
        try:
            obj = cast(Record, endpoint.create(**payload))
        except RequestError as e:
            raise NetBoxApiError(
                f"Error al crear {node_type} '{machine_name}': {e}"
            ) from e
        obj_id = get_netbox_object_id(obj)
        log.info("CREATED %s: %s (ID=%d)", node_type, machine_name, obj_id)
        return SyncStatus.CREATED, obj_id, obj

    existing_id = get_netbox_object_id(existing[0])
    try:
        updated = existing[0].update(payload)
    except RequestError as e:
        raise NetBoxApiError(
            f"Error al actualizar {node_type} '{machine_name}': {e}"
        ) from e
    if updated:
        log.info("UPDATED %s: %s", node_type, machine_name)
        return SyncStatus.UPDATED, existing_id, existing[0]

    log.info("UNCHANGED %s: %s", node_type, machine_name)
    return SyncStatus.UNCHANGED, existing_id, existing[0]


def _validate_sync(
    endpoint: Endpoint,
    payload: NetBoxPayload,
    machine_name: str,
    uuid: str,
    csv_name_counts: Counter[str],
    config: NetBoxMappingConfig,
    dry_run: bool,
) -> SyncResult:
    """Busca un objeto en NetBox, valida su unicidad si coincide por nombre y ejecuta la sincronización."""
    existing, found_by_uuid, found_by_name = _find_existing_object(
        uuid,
        machine_name,
        endpoint,
        config,
    )

    matched_by_name_only = found_by_name and not found_by_uuid
    if matched_by_name_only and not _is_name_safely_unique(
        endpoint, existing, machine_name, csv_name_counts
    ):
        raise RowSkipCondition("No se puede asegurar unicidad por nombre.")

    return _execute_sync(
        endpoint,
        payload,
        existing,
        machine_name,
        uuid,
        dry_run,
    )


def _resolve_base_node(
    node_type: NodeType,
    endpoints: NetBoxEndpoints,
    row: CsvRow,
    config: NetBoxMappingConfig,
    site: NetBoxObject,
    caches: CacheStore,
    native_maps: list[FieldMappingConfig],
    custom_maps: list[FieldMappingConfig],
    dry_run: bool,
) -> BaseNodeData:
    """
    Resuelve los campos comunes entre device y virtual_machine.
    """
    machine_name = extract_csv_value(row, "machine_name", config, required=True)
    uuid = extract_csv_value(row, "inventory_uuid", config).lower()
    machine_type = extract_csv_value(row, "machine_type", config, required=True)

    payload = build_payload(row, native_maps, custom_maps, config)

    platform_id = _resolve_platform(
        endpoints.platforms, row, caches.platforms, dry_run, config
    )
    if platform_id is not None:
        payload["platform"] = platform_id

    payload["role"] = _resolve_device_role(row, caches.device_roles, config)
    payload["status"] = _resolve_netbox_status(row, config, node_type)
    payload["site"] = get_netbox_object_id(site)

    return {
        "machine_name": machine_name,
        "inventory_uuid": uuid,
        "machine_type": machine_type,
        "payload": payload,
    }


def sync_device(
    endpoints: NetBoxEndpoints,
    row: CsvRow,
    config: NetBoxMappingConfig,
    site: NetBoxObject,
    cluster_type_map: dict[str, str],
    fallback_cluster_type: NetBoxObject,
    caches: CacheStore,
    csv_name_counts: Counter[str],
    dry_run: bool,
) -> SyncResult:
    """
    Sincroniza una fila de tipo "device" o "hipervisor" con NetBox.
    Retorna: (SyncStatus, obj_id)
    """
    # ── VALIDACIÓN TEMPRANA (Fail-Fast) ──
    _ = extract_csv_value(row, "manufacturer", config, required=True)
    _ = extract_csv_value(row, "model", config, required=True)

    node_cfg = config.node_types.get_config(NodeType.DEVICE)

    # Resolvemos los campos base
    base = _resolve_base_node(
        NodeType.DEVICE,
        endpoints,
        row,
        config,
        site,
        caches,
        node_cfg.native_mappings,
        node_cfg.custom_mappings,
        dry_run,
    )

    machine_name = base["machine_name"]
    uuid = base["inventory_uuid"]
    machine_type = base["machine_type"]
    payload = base["payload"]

    # Compensación de API NetBox: 'face' es obligatorio si 'position' existe.
    if payload.get("position") is not None and "face" not in payload:
        payload["face"] = "front"

    # Determinar si el dispositivo es un hipervisor (host de cluster) según YAML
    machine_type_col = config.csv_columns.get("machine_type")
    is_hypervisor = (
        machine_type_col is not None
        and machine_type_col.cluster_host_types is not None
        and machine_type in machine_type_col.cluster_host_types
    )

    # Cluster para hipervisores.
    if is_hypervisor:
        cluster_id = _resolve_cluster(
            endpoints.clusters,
            row,
            cluster_type_map,
            caches.cluster_types,
            fallback_cluster_type,
            site,
            caches.clusters,
            dry_run,
            config,
        )
        if cluster_id is not None:
            payload["cluster"] = cluster_id
        else:
            log.info("INFO (%s): Hipervisor sin cluster asignado.", machine_name)

    # Rack.
    rack_name = extract_csv_value(row, "rack", config)
    if rack_name:
        rack = ensure_rack(
            endpoints.racks,
            rack_name,
            site,
            caches.racks,
            dry_run,
        )
        payload["rack"] = get_netbox_object_id(rack)

    # DeviceType (busca el Manufacturer por dentro).
    payload["device_type"] = _resolve_device_type(
        endpoints,
        row,
        caches,
        config,
        dry_run,
    )

    # ── GET o CREATE/UPDATE ──────────────────────────────────
    return _validate_sync(
        endpoints.devices,
        payload,
        machine_name,
        uuid,
        csv_name_counts,
        config,
        dry_run,
    )


def sync_vm(
    endpoints: NetBoxEndpoints,
    row: CsvRow,
    config: NetBoxMappingConfig,
    site: NetBoxObject,
    cluster_type_map: dict[str, str],
    fallback_cluster_type: NetBoxObject,
    caches: CacheStore,
    csv_name_counts: Counter[str],
    dry_run: bool,
) -> SyncResult:
    """
    Sincroniza una fila de tipo "virtual_machine" con NetBox.
    Retorna: (SyncStatus, obj_id)
    """
    # ── VALIDACIÓN TEMPRANA (Fail-Fast) ──
    _ = extract_csv_value(row, "cluster_name", config, required=True)

    node_cfg = config.node_types.get_config(NodeType.VIRTUAL_MACHINE)

    # Resolvemos los campos base
    base = _resolve_base_node(
        NodeType.VIRTUAL_MACHINE,
        endpoints,
        row,
        config,
        site,
        caches,
        node_cfg.native_mappings,
        node_cfg.custom_mappings,
        dry_run,
    )

    machine_name = base["machine_name"]
    uuid = base["inventory_uuid"]
    payload = base["payload"]

    # Cluster.
    cluster_id = _resolve_cluster(
        endpoints.clusters,
        row,
        cluster_type_map,
        caches.cluster_types,
        fallback_cluster_type,
        site,
        caches.clusters,
        dry_run,
        config,
    )

    if cluster_id is not None:
        payload["cluster"] = cluster_id
    else:
        raise RowValidationError("Falló la resolución del cluster.")

    # Device del hipervisor host (acotado a site y cacheado).
    host_dev_id = _resolve_host_device(
        endpoints.devices,
        row,
        site,
        machine_name,
        caches.host_devices,
        config,
    )
    if host_dev_id is not None:
        payload["device"] = host_dev_id

    # ── GET o CREATE/UPDATE ──────────────────────────────────
    return _validate_sync(
        endpoints.virtual_machines,
        payload,
        machine_name,
        uuid,
        csv_name_counts,
        config,
        dry_run,
    )


# ============================================================
# PARSEO DE INTERFACES y IPs
# ============================================================


def _validate_interface_ip(ip_raw: str, name: str) -> str:
    """Valida que un string sea una dirección IP correcta."""
    try:
        ipaddress.ip_address(ip_raw)
        return ip_raw
    except ValueError as e:
        raise FieldParseError(
            f"IP '{ip_raw}' en interfaz '{name}' no es una dirección IP válida."
        ) from e


def _build_interface_cidr(ip_val: str, pfx_val: str, name: str) -> str:
    """Valida IP y prefijo construyendo una dirección CIDR válida."""
    try:
        mask_or_prefix = (
            pfx_val.split("/")[1].strip() if "/" in pfx_val else pfx_val.strip()
        )
        return str(ipaddress.ip_interface(f"{ip_val}/{mask_or_prefix}"))
    except ValueError as e:
        raise FieldParseError(
            f"Prefijo o CIDR inválido '{pfx_val}' para IP '{ip_val}' en interfaz '{name}'."
        ) from e


def _sanitize_mac_address(mac_raw: str, name: str) -> str:
    """Valida y formatea una MAC a su forma estándar (AA:BB:CC:DD:EE:FF)."""
    cleaned = re.sub(r"[^a-fA-F0-9]", "", mac_raw)

    if len(cleaned) != 12:
        raise FieldParseError(
            f"MAC '{mac_raw}' en interfaz '{name}' no es válida (esperado 12 hex)."
        )

    pairs = [cleaned[i : i + 2] for i in range(0, 12, 2)]
    return ":".join(pairs).upper()


def _parse_single_network_interface(
    name: str,
    status_raw: str,
    ip_raw: str,
    pfx_raw: str,
    mac_raw: str,
    status_map: dict[str, bool],
    config: NetBoxMappingConfig,
) -> NetworkInterfaceData:
    """Parsea una única interfaz aislando la lógica de validación de IPs."""
    if config.is_empty(name):
        raise RowValidationError(
            "El nombre de una interfaz de red no puede estar vacío."
        )

    enabled = status_map.get(status_raw.lower().strip(), True)
    ip_val = ip_raw if not config.is_empty(ip_raw) else None
    pfx_val = pfx_raw if not config.is_empty(pfx_raw) else None
    mac_val = mac_raw if not config.is_empty(mac_raw) else None

    cidr = None
    if ip_val:
        try:
            ip_val = _validate_interface_ip(ip_val, name)
            if pfx_val:
                cidr = _build_interface_cidr(ip_val, pfx_val, name)
        except FieldParseError as e:
            raise RowValidationError(str(e)) from e

    if mac_val:
        try:
            mac_val = _sanitize_mac_address(mac_val, name)
        except FieldParseError as e:
            raise RowValidationError(str(e)) from e

    return {
        "name": name,
        "enabled": enabled,
        "mac": mac_val,
        "ip": ip_val,
        "prefix": pfx_val,
        "cidr": cidr,
    }


def parse_network_interfaces(
    row: CsvRow,
    config: NetBoxMappingConfig,
) -> list[NetworkInterfaceData]:
    """
    Parsea las columnas de red del CSV y devuelve una lista de interfaces.
    Lanza RowValidationError si los arrays tienen longitudes distintas.

    Reglas de validación:
      - La columna "Interfaces" (names) define la cantidad de interfaces.
      - Las demás columnas deben tener exactamente la misma cantidad de
        elementos separados por comas, o estar completamente vacías.
      - Una columna completamente vacía indica que ninguna interfaz
        tiene ese dato (ej. todas las IPs son N/A o están en blanco).
      - Si una columna tiene datos pero su cantidad de elementos difiere
        de la cantidad de interfaces, la fila se descarta para interfaces
        (el Device/VM se sincroniza de todas formas).

    Columna "Red IP" (prefix):
      Contiene la red o máscara asociada a cada IP. Se combina con la
      columna "IP" para construir el CIDR que NetBox requiere en su API.

      Formatos soportados:
        - Red/Prefijo:  "136.12.34.128/26"          → se extrae "26"
        - Red/Máscara:  "192.168.1.0/255.255.255.0" → se extrae "255.255.255.0"
        - Prefijo solo: "26"                        → se usa directamente
        - Máscara sola: "255.255.255.0"             → se usa directamente

      En todos los casos, el script normaliza al formato CIDR canónico
      antes de enviarlo a NetBox.
    """

    def split_col(col_name: str) -> list[str]:
        raw = row.get(col_name, "")
        return [v.strip() for v in raw.split(",")] if not config.is_empty(raw) else []

    names = split_col(config.csv_columns["iface_names"].source)
    if not names:
        return []

    statuses = split_col(config.csv_columns["iface_status"].source)
    ips = split_col(config.csv_columns["iface_ip"].source)
    prefixes = split_col(config.csv_columns["iface_pfx"].source)
    macs = split_col(config.csv_columns["iface_mac"].source)

    max_len = len(names)
    inconsistencies = []
    col_mappings = {
        "iface_status": statuses,
        "iface_ip": ips,
        "iface_pfx": prefixes,
        "iface_mac": macs,
    }

    for col_key, lst in col_mappings.items():
        if lst and len(lst) != max_len:
            col_name = config.csv_columns[col_key].source
            inconsistencies.append(f"'{col_name}' tiene {len(lst)} elementos")

    if inconsistencies:
        ifaces_col = config.csv_columns["iface_names"].source
        raise RowValidationError(
            f"Discrepancia de elementos en red: se declararon {max_len} interfaces en '{ifaces_col}', "
            f"pero " + ", ".join(inconsistencies) + ". Revisa las comas."
        )

    def fill_if_empty(lst: list[str], length: int) -> list[str]:
        return lst if lst else [""] * length

    statuses = fill_if_empty(statuses, max_len)
    ips = fill_if_empty(ips, max_len)
    prefixes = fill_if_empty(prefixes, max_len)
    macs = fill_if_empty(macs, max_len)

    interfaces: list[NetworkInterfaceData] = []
    for i, name in enumerate(names):
        parsed = _parse_single_network_interface(
            name=name,
            status_raw=statuses[i],
            ip_raw=ips[i],
            pfx_raw=prefixes[i],
            mac_raw=macs[i],
            status_map=config.csv_columns["iface_status"].map or {},
            config=config,
        )
        interfaces.append(parsed)

    return interfaces


# ============================================================
# SINCRONIZACIÓN DE INTERFACES
# ============================================================


def _assign_ip(
    ip_addresses_endpoint: Endpoint,
    cidr: str,
    iface_obj: NetBoxObject,
    dry_run: bool,
) -> tuple[NetBoxObject, bool]:
    """
    Crea o actualiza una IP address en NetBox y la asigna a la interfaz.
    Retorna el objeto IP.
    """
    node_type = get_node_type_from_object(iface_obj)
    assigned_type: str = (
        "dcim.interface"
        if node_type == NodeType.DEVICE
        else "virtualization.vminterface"
    )
    existing_ips: list[Record] = list(ip_addresses_endpoint.filter(address=cidr))

    # 1. Verificar si la IP ya está asignada a esta interfaz
    for ip_obj in existing_ips:
        current_id = getattr(ip_obj, "assigned_object_id", None)
        current_type = getattr(ip_obj, "assigned_object_type", None)
        if current_id == iface_obj.id and str(current_type) == assigned_type:
            return ip_obj, False

    # 2. Buscar si hay alguna IP libre con este valor que podamos reclamar
    unassigned_ip = None
    for ip_obj in existing_ips:
        if getattr(ip_obj, "assigned_object_id", None) is None:
            unassigned_ip = ip_obj
            break

    if unassigned_ip:
        if dry_run:
            log.info(
                "[DRY-RUN] Actualizaría IP libre %s (asignación a objeto %s)",
                cidr,
                iface_obj.id,
            )
            return MockNetBoxRecord(
                id=0,
                address=cidr,
                assigned_object_id=iface_obj.id,
                assigned_object_type=assigned_type,
            ), True
        try:
            unassigned_ip.update(
                {
                    "assigned_object_type": assigned_type,
                    "assigned_object_id": iface_obj.id,
                }
            )
            log.info("IP libre reasignada: %s", cidr)
            return unassigned_ip, True
        except RequestError as e:
            raise NetBoxApiError(f"Error actualizando IP libre {cidr}: {e}") from e

    # 3. Todas las IPs existentes están ocupadas por otros nodos. Crear una nueva.
    if dry_run:
        log.info(
            "[DRY-RUN] Crearía nueva IP %s (asignada a objeto %s)", cidr, iface_obj.id
        )
        return MockNetBoxRecord(
            id=0,
            address=cidr,
            assigned_object_id=iface_obj.id,
            assigned_object_type=assigned_type,
        ), True

    try:
        obj = cast(
            Record,
            ip_addresses_endpoint.create(
                address=cidr,
                status="active",
                assigned_object_type=assigned_type,
                assigned_object_id=iface_obj.id,
            ),
        )
    except RequestError as e:
        raise NetBoxApiError(f"Error creando IP {cidr}: {e}") from e

    log.info("IP creada y asignada: %s", cidr)
    return obj, True


def _ensure_mac_address_assignment(
    mac_addresses_endpoint: Endpoint,
    mac_val: str,
    iface_obj: NetBoxObject,
    dry_run: bool,
) -> tuple[NetBoxObject, bool]:
    """
    Crea o actualiza una MAC address en NetBox y la asigna a la interfaz.
    Retorna el objeto MAC.
    """
    node_type = get_node_type_from_object(iface_obj)
    assigned_type: str = (
        "dcim.interface"
        if node_type == NodeType.DEVICE
        else "virtualization.vminterface"
    )
    existing_macs: list[Record] = list(
        mac_addresses_endpoint.filter(mac_address=mac_val)
    )

    # 1. Verificar si la MAC ya está asignada a esta interfaz
    for mac_obj in existing_macs:
        current_id = getattr(mac_obj, "assigned_object_id", None)
        current_type = getattr(mac_obj, "assigned_object_type", None)
        if current_id == iface_obj.id and str(current_type) == assigned_type:
            return mac_obj, False

    # 2. Buscar si hay alguna MAC libre con este valor que podamos reclamar
    unassigned_mac = None
    for mac_obj in existing_macs:
        if getattr(mac_obj, "assigned_object_id", None) is None:
            unassigned_mac = mac_obj
            break

    if unassigned_mac:
        if dry_run:
            log.info(
                "[DRY-RUN] Actualizaría MAC libre %s (asignación a objeto %s)",
                mac_val,
                iface_obj.id,
            )
            return MockNetBoxRecord(
                id=0,
                mac_address=mac_val,
                assigned_object_id=iface_obj.id,
                assigned_object_type=assigned_type,
            ), True
        try:
            unassigned_mac.update(
                {
                    "assigned_object_type": assigned_type,
                    "assigned_object_id": iface_obj.id,
                }
            )
            log.info("MAC libre reasignada: %s", mac_val)
            return unassigned_mac, True
        except RequestError as e:
            raise NetBoxApiError(f"Error actualizando MAC libre {mac_val}: {e}") from e

    # 3. Crear una nueva MAC.
    if dry_run:
        log.info(
            "[DRY-RUN] Crearía nueva MAC %s (asignada a objeto %s)",
            mac_val,
            iface_obj.id,
        )
        return MockNetBoxRecord(
            id=0,
            mac_address=mac_val,
            assigned_object_id=iface_obj.id,
            assigned_object_type=assigned_type,
        ), True

    try:
        obj = cast(
            Record,
            mac_addresses_endpoint.create(
                mac_address=mac_val,
                assigned_object_type=assigned_type,
                assigned_object_id=iface_obj.id,
            ),
        )
        log.info("MAC creada y asignada: %s", mac_val)
        return obj, True
    except RequestError as e:
        raise NetBoxApiError(f"Error creando MAC {mac_val}: {e}") from e


def _upsert_interface_record(
    name: str,
    payload: NetBoxPayload,
    existing_obj: NetBoxObject | None,
    iface_endpoint: Endpoint,
    obj_id: int,
    dry_run: bool,
) -> tuple[NetBoxObject, bool]:
    """Ejecuta la lógica de creación o actualización de una interfaz."""
    if dry_run:
        if not existing_obj:
            log.info("[DRY-RUN] Crearía interfaz %s en objeto %s", name, obj_id)
            return MockNetBoxRecord(id=0, name=name, **payload), True

        diff = check_record_changes(cast(Record, existing_obj), payload)
        if diff:
            log.info(
                "[DRY-RUN] Actualizaría interfaz %s en objeto %s - Cambios: %s",
                name,
                obj_id,
                list(diff.keys()),
            )
            return existing_obj, True
        return existing_obj, False

    try:
        if not existing_obj:
            new_obj = cast(Record, iface_endpoint.create(**payload))
            log.info("Interfaz creada: %s", name)
            return new_obj, True

        diff = check_record_changes(cast(Record, existing_obj), payload)
        if diff:
            cast(Record, existing_obj).update(payload)
            log.info("Interfaz actualizada: %s", name)
            return existing_obj, True
        return existing_obj, False
    except RequestError as e:
        raise NetBoxApiError(f"Error procesando interfaz '{name}': {e}") from e


def _sync_single_interface(
    iface_data: NetworkInterfaceData,
    obj_id: int,
    iface_endpoint: Endpoint,
    existing_ifaces: dict[str, NetBoxObject],
    endpoints: NetBoxEndpoints,
    dry_run: bool,
) -> tuple[NetBoxObject, NetBoxObject | None, NetBoxObject | None, bool]:
    """
    Sincroniza una interfaz individual y le asigna su IP y MAC.
    Retorna una tupla con (Interfaz, IP asignada o None, MAC asignada o None, hubo cambios).
    """
    name = iface_data["name"]
    payload: NetBoxPayload = {"name": name, "enabled": iface_data["enabled"]}

    if get_node_type_from_object(iface_endpoint) == NodeType.DEVICE:
        payload["device"] = obj_id
        payload["type"] = "other"  # tipo genérico; ajustable
    else:
        payload["virtual_machine"] = obj_id

    iface_obj, iface_changed = _upsert_interface_record(
        name,
        payload,
        existing_ifaces.get(name),
        iface_endpoint,
        obj_id,
        dry_run,
    )
    existing_ifaces[name] = iface_obj

    ip_obj = None
    ip_changed = False
    if cidr := iface_data.get("cidr"):
        ip_obj, ip_changed = _assign_ip(
            endpoints.ip_addresses, cidr, iface_obj, dry_run
        )

    mac_obj = None
    mac_changed = False
    if mac := iface_data.get("mac"):
        mac_obj, mac_changed = _ensure_mac_address_assignment(
            endpoints.mac_addresses, mac.upper(), iface_obj, dry_run
        )

    any_changes = iface_changed or ip_changed or mac_changed
    return iface_obj, ip_obj, mac_obj, any_changes


def _prune_orphan_interfaces(
    existing_ifaces: dict[str, NetBoxObject],
    csv_iface_names: set[str],
    obj_id: int,
    dry_run: bool,
) -> tuple[int, int]:
    """
    Elimina (poda) de NetBox las interfaces que ya no existen en el CSV.
    Retorna (cantidad_eliminadas, cantidad_errores).
    """
    errors = 0
    deleted_count = 0
    for name, iface_obj in existing_ifaces.items():
        if name not in csv_iface_names:
            if dry_run:
                log.info(
                    "[DRY-RUN] Eliminaría interfaz huérfana '%s' en objeto %s",
                    name,
                    obj_id,
                )
                deleted_count += 1
            else:
                try:
                    cast(Record, iface_obj).delete()
                    log.info("DELETED interfaz huérfana: %s", name)
                    deleted_count += 1
                except RequestError:
                    log.exception("Error eliminando interfaz huérfana '%s'", name)
                    errors += 1
    return deleted_count, errors


def _get_interface_endpoint_and_filter(
    endpoints: NetBoxEndpoints, obj_id: int, node_type: NodeType
) -> tuple[Endpoint, dict[str, Any]]:
    """Determina el endpoint y filtro correcto de interfaces según el tipo de nodo."""
    if node_type == NodeType.DEVICE:
        return endpoints.device_interfaces, {"device_id": obj_id}
    return endpoints.vm_interfaces, {"virtual_machine_id": obj_id}


def _fetch_existing_interfaces(
    iface_endpoint: Endpoint,
    iface_filter: dict[str, Any],
    obj_id: int,
) -> dict[str, NetBoxObject]:
    """Obtiene las interfaces existentes de un nodo desde NetBox."""
    if obj_id == 0:
        return {}
    filtered_ifaces = list(iface_endpoint.filter(**iface_filter))
    return {str(iface.name): iface for iface in filtered_ifaces}


def _process_interfaces_sync(
    interfaces: list[NetworkInterfaceData],
    obj_id: int,
    iface_endpoint: Endpoint,
    existing_ifaces: dict[str, NetBoxObject],
    endpoints: NetBoxEndpoints,
    dry_run: bool,
) -> tuple[int, list[int], bool]:
    """Ejecuta la sincronización de una lista de interfaces y recopila IDs de IPv4.
    Retorna una tupla: (cantidad de errores, lista de IDs de IPv4 asignadas, hubo cambios)."""
    errors = 0
    ipv4_ids: list[int] = []
    any_changes = False

    for iface_data in interfaces:
        try:
            _, ip_obj, _, iface_changed = _sync_single_interface(
                iface_data,
                obj_id,
                iface_endpoint,
                existing_ifaces,
                endpoints,
                dry_run,
            )
            any_changes |= iface_changed

            if ip_obj is not None:
                address = getattr(ip_obj, "address", "")
                if address and ":" not in str(address):
                    ipv4_ids.append(getattr(ip_obj, "id", 0))
        except NetBoxApiError:
            log.exception("ERROR de API sincronizando interfaz")
            errors += 1

    return errors, ipv4_ids, any_changes


def sync_interfaces_for_object(
    endpoints: NetBoxEndpoints,
    obj_id: int,
    node_type: NodeType,
    interfaces: list[NetworkInterfaceData],
    dry_run: bool,
    prune_interfaces: bool = False,
) -> tuple[int, list[int], bool]:
    """Sincroniza interfaces y sus IPs para un Device o VM.
    Retorna una tupla: (cantidad de errores, lista de IDs de IPv4 asignadas, hubo cambios)."""
    iface_endpoint, iface_filter = _get_interface_endpoint_and_filter(
        endpoints, obj_id, node_type
    )
    existing_ifaces = _fetch_existing_interfaces(iface_endpoint, iface_filter, obj_id)

    errors, ipv4_ids, any_changes = _process_interfaces_sync(
        interfaces, obj_id, iface_endpoint, existing_ifaces, endpoints, dry_run
    )

    if prune_interfaces and obj_id != 0:
        csv_names = {iface["name"] for iface in interfaces}
        pruned_count, prune_errors = _prune_orphan_interfaces(
            existing_ifaces, csv_names, obj_id, dry_run
        )
        errors += prune_errors
        any_changes |= pruned_count > 0

    return errors, ipv4_ids, any_changes


# ============================================================
# MAIN
# ============================================================


def _assign_primary_ipv4(
    main_obj: NetBoxObject,
    ipv4_ids: list[int],
    machine_name: str,
    dry_run: bool,
) -> bool:
    """
    Si existe exactamente 1 dirección IPv4 asignada a las interfaces,
    la define como IP primaria (primary_ip4) del dispositivo/VM.

    Retorna True si se asignó exitosamente (o se simuló en dry-run), False de lo contrario.
    """
    if len(ipv4_ids) != 1:
        return False

    primary_id = ipv4_ids[0]

    current_primary = getattr(main_obj, "primary_ip4", None)
    current_primary_id = (
        getattr(current_primary, "id", None) if current_primary else None
    )

    if current_primary_id == primary_id:
        return False

    if dry_run:
        log.info(
            "[DRY-RUN] Asignaría IP primaria (ID=%s) al nodo '%s'",
            primary_id,
            machine_name,
        )
        return True

    try:
        cast(Record, main_obj).update({"primary_ip4": primary_id})
        log.debug("IP primaria actualizada en '%s' (ID=%s)", machine_name, primary_id)
        return True
    except RequestError:
        log.exception(
            "Fallo al actualizar IP primaria en '%s'",
            machine_name,
        )
        return False


def _process_node_sync(
    machine_name: str,
    row: CsvRow,
    node_type: NodeType,
    endpoints: NetBoxEndpoints,
    config: NetBoxMappingConfig,
    site: NetBoxObject,
    cluster_type_map: dict[str, str],
    fallback_cluster_type: NetBoxObject,
    caches: CacheStore,
    csv_name_counts: Counter[str],
    dry_run: bool,
) -> tuple[SyncStatus, int, NetBoxObject | None]:
    """Sincroniza el nodo principal (Device o VM) en NetBox."""
    if node_type == NodeType.DEVICE:
        result, obj_id, main_obj = sync_device(
            endpoints,
            row,
            config,
            site,
            cluster_type_map,
            fallback_cluster_type,
            caches,
            csv_name_counts,
            dry_run,
        )
        site_id = get_netbox_object_id(site)
        caches.host_devices[(site_id, machine_name)] = obj_id
        return result, obj_id, main_obj
    else:
        return sync_vm(
            endpoints,
            row,
            config,
            site,
            cluster_type_map,
            fallback_cluster_type,
            caches,
            csv_name_counts,
            dry_run,
        )


def _parse_row_interfaces(
    row: CsvRow,
    machine_name: str,
    config: NetBoxMappingConfig,
    dry_run: bool,
) -> list[NetworkInterfaceData]:
    """Parsea las interfaces de la fila CSV y advierte si están vacías."""
    interfaces = parse_network_interfaces(row, config)
    if not interfaces and not dry_run:
        ifaces_col = config.csv_columns["iface_names"].source
        log.warning(
            "SKIP interfaces de '%s': columna '%s' está vacía u omitida.",
            machine_name,
            ifaces_col,
        )
    return interfaces


def _process_interfaces_and_ips(
    endpoints: NetBoxEndpoints,
    obj_id: int,
    node_type: NodeType,
    interfaces: list[NetworkInterfaceData],
    dry_run: bool,
    prune_interfaces: bool,
    main_obj: NetBoxObject | None,
    machine_name: str,
    counts: SyncCounts,
    result: SyncStatus,
) -> None:
    """Sincroniza interfaces y asigna la IP primaria, mutando los contadores."""
    iface_errors, ipv4_ids, ifaces_changed = sync_interfaces_for_object(
        endpoints,
        obj_id,
        node_type,
        interfaces,
        dry_run,
        prune_interfaces,
    )
    if iface_errors > 0:
        counts[SyncStatus.ERROR] += iface_errors

    primary_ip_changed = False
    if main_obj is not None:
        primary_ip_changed = _assign_primary_ipv4(
            main_obj, ipv4_ids, machine_name, dry_run
        )

    if result == SyncStatus.UNCHANGED and (ifaces_changed or primary_ip_changed):
        counts[SyncStatus.UNCHANGED] -= 1
        counts[SyncStatus.UPDATED] += 1


def _sync_row(
    row_num: int,
    row: CsvRow,
    node_type: NodeType,
    endpoints: NetBoxEndpoints,
    config: NetBoxMappingConfig,
    site: NetBoxObject,
    cluster_type_map: dict[str, str],
    fallback_cluster_type: NetBoxObject,
    caches: CacheStore,
    csv_name_counts: Counter[str],
    counts: SyncCounts,
    dry_run: bool,
    prune_interfaces: bool = False,
) -> SyncCounts:
    """
    Sincroniza una fila individual del CSV con NetBox, incluyendo
    la creación/actualización del objeto principal y sus interfaces de red.

    Gestiona internamente todas las excepciones esperadas y actualiza
    los contadores de resultado en el diccionario mutable `counts`.
    """
    machine_name = extract_csv_value(row, "machine_name", config) or f"fila {row_num}"

    try:
        result, obj_id, main_obj = _process_node_sync(
            machine_name,
            row,
            node_type,
            endpoints,
            config,
            site,
            cluster_type_map,
            fallback_cluster_type,
            caches,
            csv_name_counts,
            dry_run,
        )
    except ConfigValidationError as e:
        raise ConfigValidationError(
            f"Error de configuración en fila {row_num}: {e}"
        ) from e
    except RowSkipCondition as e:
        log.warning("SKIP fila %d: %s", row_num, e)
        counts[SyncStatus.SKIPPED] += 1
        return counts
    except (NetBoxApiError, RowValidationError):
        log.exception("ERROR en fila %d", row_num)
        counts[SyncStatus.ERROR] += 1
        return counts
    except Exception:
        log.exception(
            "ERROR inesperado al procesar fila %d ('%s')",
            row_num,
            machine_name,
        )
        counts[SyncStatus.ERROR] += 1
        return counts

    counts[result] += 1

    # ── Validar ID para interfaces (Fail-Fast) ────────────
    if not obj_id:
        if not dry_run:
            log.warning("SKIP interfaces de '%s': el objeto no tiene ID.", machine_name)
        return counts

    # ── Parsear interfaces ────────────────────────────────
    try:
        interfaces = _parse_row_interfaces(row, machine_name, config, dry_run)
    except RowValidationError as e:
        log.warning("Omitiendo interfaces de '%s': %s", machine_name, e)
        counts[SyncStatus.ERROR] += 1
        return counts
    except Exception:
        log.exception("ERROR inesperado al parsear interfaces de '%s'", machine_name)
        counts[SyncStatus.ERROR] += 1
        return counts

    # ── Sincronizar interfaces del objeto ─────────────────
    try:
        _process_interfaces_and_ips(
            endpoints,
            obj_id,
            node_type,
            interfaces,
            dry_run,
            prune_interfaces,
            main_obj,
            machine_name,
            counts,
            result,
        )
    except Exception:
        log.exception(
            "ERROR inesperado al sincronizar interfaces de '%s'", machine_name
        )
        counts[SyncStatus.ERROR] += 1

    return counts


def _print_summary_and_exit(
    total_rows: int,
    counts: SyncCounts,
    dry_run: bool,
) -> NoReturn:
    """Imprime el resumen de la operación y finaliza la ejecución."""
    summary = (
        "\n" + "=" * 50 + "\n"
        "Resumen de exportación a NetBox\n" + "=" * 50 + "\n"
        f"  Total filas procesadas : {total_rows}\n"
        f"  Creados                : {counts[SyncStatus.CREATED]}\n"
        f"  Actualizados           : {counts[SyncStatus.UPDATED]}\n"
        f"  Sin cambios            : {counts[SyncStatus.UNCHANGED]}\n"
        f"  Omitidos (SKIP)        : {counts[SyncStatus.SKIPPED]}\n"
        f"  Errores                : {counts[SyncStatus.ERROR]}\n" + "=" * 50
    )

    if dry_run:
        summary += "\n(Modo DRY-RUN: no se realizaron cambios en NetBox)"

    log.info(summary)

    sys.exit(0 if counts[SyncStatus.ERROR] == 0 else 1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Exporta merged_inventory.csv a NetBox 4.6+"
    )
    parser.add_argument(
        "csv",
        type=Path,
        help="Ruta al CSV fusionado (merged_inventory.csv).",
    )
    parser.add_argument(
        "mapping",
        type=Path,
        nargs="?",
        default=None,
        help="Ruta a netbox_mapping.yaml (por defecto: junto al script).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Muestra las operaciones sin modificar NetBox.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Activa logs de nivel DEBUG.",
    )
    parser.add_argument(
        "--prune-interfaces",
        action="store_true",
        help="Elimina las interfaces de NetBox que ya no existan en el CSV para ese nodo.",
    )

    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    if args.dry_run:
        log.info("Modo DRY-RUN activado. No se modificará NetBox.")

    # ── Cargar variables de entorno desde .env si existe ────
    load_dotenv()

    # ── Cargar configuración ─────────────────────────────────
    mapping_path = resolve_mapping_path(args.mapping)
    config: NetBoxMappingConfig = load_config(mapping_path)

    # ── Leer y Validar CSV (Fail-Fast) ───────────────────────
    rows = read_and_validate_csv(args.csv, config)

    # ── Conteo global de nombres de máquina ───────────────
    # Usado para validar unicidad antes de permitir
    # actualizaciones por nombre (sin UUID).
    csv_name_counts: Counter[str] = count_machine_names(rows, config)

    # ── Cargar credenciales ──────────────────────────────────
    url, token, verify_ssl = load_env()

    # ── Conectar ─────────────────────────────────────────────
    nb: Api = build_nb_client(url, token, verify_ssl)

    # ── Validar endpoints requeridos ─────────────────────────
    endpoints: NetBoxEndpoints = build_netbox_endpoints(nb)

    # ── Garantizar custom fields ─────────────────────────────
    ensure_custom_fields(endpoints, config, args.dry_run)

    # ── Garantizar taxonomía global ──────────────────────────
    site: NetBoxObject = ensure_site(endpoints, config.site, args.dry_run)

    # ── Inicializar caches ───────────────────────────────────
    caches: CacheStore = CacheStore()

    # ── Pre-escaneo: asociar clústeres con su tecnología ─────
    cluster_type_map = precompute_cluster_type_map(rows, config)
    fallback_cluster_type = ensure_dynamic_cluster_types(
        endpoints,
        cluster_type_map,
        config.cluster_type,
        caches.cluster_types,
        args.dry_run,
    )

    # ── Garantizar taxonomía local ──────────────────────────
    ensure_all_device_roles(
        endpoints, config.device_roles, caches.device_roles, args.dry_run
    )

    # ── Contadores ───────────────────────────────────────────
    counts: SyncCounts = {
        SyncStatus.CREATED: 0,
        SyncStatus.UPDATED: 0,
        SyncStatus.UNCHANGED: 0,
        SyncStatus.SKIPPED: 0,
        SyncStatus.ERROR: 0,
    }

    # ── Clasificar filas por tipo de nodo ─────────────────────
    device_rows: list[tuple[int, CsvRow]] = []
    vm_rows: list[tuple[int, CsvRow]] = []

    for row_num, row in enumerate(rows, start=2):
        try:
            node_type = get_node_type_from_row(row, config)
            if node_type == NodeType.DEVICE:
                device_rows.append((row_num, row))
            else:
                vm_rows.append((row_num, row))
        except RowValidationError:
            machine_name = (
                extract_csv_value(row, "machine_name", config) or f"fila {row_num}"
            )
            log.exception("ERROR fila %d ('%s')", row_num, machine_name)
            counts[SyncStatus.ERROR] += 1

    log.info(
        "Clasificación: %d device(s), %d VM(s), %d error(es) de tipo.",
        len(device_rows),
        len(vm_rows),
        counts[SyncStatus.ERROR],
    )

    # ── Fase 1: Sincronizar Devices ──────────────────────────
    log.info("── Fase 1: Sincronizando %d Device(s) ──", len(device_rows))
    for row_num, row in device_rows:
        counts = _sync_row(
            row_num,
            row,
            NodeType.DEVICE,
            endpoints,
            config,
            site,
            cluster_type_map,
            fallback_cluster_type,
            caches,
            csv_name_counts,
            counts,
            args.dry_run,
            args.prune_interfaces,
        )

    # ── Fase 2: Sincronizar VMs ──────────────────────────────
    log.info("── Fase 2: Sincronizando %d VM(s) ──", len(vm_rows))
    for row_num, row in vm_rows:
        counts = _sync_row(
            row_num,
            row,
            NodeType.VIRTUAL_MACHINE,
            endpoints,
            config,
            site,
            cluster_type_map,
            fallback_cluster_type,
            caches,
            csv_name_counts,
            counts,
            args.dry_run,
            args.prune_interfaces,
        )

    # ── Resumen ──────────────────────────────────────────────
    _print_summary_and_exit(len(rows), counts, args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except ConfigValidationError as e:
        log.critical("ERROR DE CONFIGURACIÓN. ABORTANDO PIPELINE: %s", e)
        sys.exit(1)
    except NetBoxApiError as e:
        log.critical("ERROR DE API. ABORTANDO PIPELINE: %s", e)
        sys.exit(1)
