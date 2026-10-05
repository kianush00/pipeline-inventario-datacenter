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
import random
import re
import sys
import time
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Literal,
    NamedTuple,
    NoReturn,
    TypeAlias,
    TypedDict,
    Union,
    assert_never,
    cast,
)

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


@contextmanager
def netbox_error_wrap(msg: str) -> Generator[None, None, None]:
    """Envuelve errores de la API de NetBox en excepciones legibles de nuestro dominio."""
    try:
        yield
    except RequestError as e:
        raise NetBoxApiError(f"{msg}: {e}") from e


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
CsvRow: TypeAlias = dict[str, str]
FieldValue: TypeAlias = str | int | float | bool | list[str] | None
CustomFieldsPayload: TypeAlias = dict[str, FieldValue]
SyncCounts: TypeAlias = dict[SyncStatus, int]
NetBoxPayload: TypeAlias = dict[str, Any]

# Alias de Tipos para los Cachés de NetBox
NameCache: TypeAlias = dict[str, NetBoxObject]
SiteNameCache: TypeAlias = dict[tuple[int, str], NetBoxObject]
ManufModelCache: TypeAlias = dict[tuple[str, str], NetBoxObject]
HostDeviceCache: TypeAlias = dict[tuple[int, str], int | None]


class SyncResult(NamedTuple):
    """
    Resultado de la sincronización de un Device o VM.
    Contiene: (estado de sincronización, ID del objeto en NetBox, objeto NetBox instanciado o None si hubo error/skip).
    """

    status: SyncStatus
    obj_id: int
    main_obj: NetBoxObject | None


class ClassifiedRows(NamedTuple):
    """
    Estructura de datos que almacena las filas del CSV clasificadas
    por su rol en la jerarquía (físico vs virtual).
    """

    device_rows: list[tuple[int, CsvRow]]
    vm_rows: list[tuple[int, CsvRow]]


class Ipv4Candidate(NamedTuple):
    """Tupla que representa un candidato a IPv4 primaria: (ip_id, iface_obj, mac_obj)."""

    ip_id: int
    iface_obj: NetBoxObject
    mac_obj: NetBoxObject | None


class SingleInterfaceResult(NamedTuple):
    """
    Resultado de procesar una única interfaz.
    Contiene: (Interfaz, IP asignada o None, MAC asignada o None, booleano indicando si hubo cambios).
    """

    iface_obj: NetBoxObject
    ip_obj: NetBoxObject | None
    mac_obj: NetBoxObject | None
    any_changes: bool


class InterfaceSyncResult(NamedTuple):
    """
    Resultado global de procesar una lista de interfaces para un nodo.
    Contiene: (cantidad de errores, lista de candidatos a IP primaria, booleano indicando si hubo cambios).
    """

    errors: int
    ipv4_candidates: list[Ipv4Candidate]
    any_changes: bool


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
    locations: Endpoint
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


def _generate_slug_for_model(data: Any) -> Any:
    """Helper compartido para autogenerar 'slug' a partir de 'name'."""
    if isinstance(data, dict):
        raw_slug = data.get("slug") or data.get("name")
        if raw_slug:
            data["slug"] = slugify(str(raw_slug))
    return data


class SiteConfig(BaseModel):
    """Configuración del Site en NetBox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    slug: str | None = None

    @model_validator(mode="before")
    @classmethod
    def generate_slug_if_missing(cls, data: Any) -> Any:
        """
        Genera un slug automáticamente a partir del nombre si el valor está vacío.
        Retorna el slug generado o el valor original.
        """
        return _generate_slug_for_model(data)

    @field_validator("name", "slug")
    @classmethod
    def validate_no_unresolved_vars(cls, v: str | None) -> str | None:
        """
        Valida que el string no contenga variables sin resolver (marcadas con @@).
        Retorna el valor original si es válido.
        """
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
        """
        Genera un slug automáticamente a partir del nombre si el valor está vacío.
        Retorna el slug generado o el valor original.
        """
        return _generate_slug_for_model(data)


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
        """
        Genera un slug automáticamente a partir del nombre si el valor está vacío.
        Retorna el slug generado o el valor original.
        """
        return _generate_slug_for_model(data)


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
        """
        Valida que los tipos de objeto estén en la lista permitida.
        Retorna la lista de object_types si es válida.
        """
        for ot in v:
            if not OBJECT_TYPE_PATTERN.match(ot):
                raise ValueError(
                    f"Formato de Object Type inválido: '{ot}'. "
                    "Se esperaba 'app_label.model' (ej. 'dcim.device')."
                )
        return v

    @model_validator(mode="after")
    def validate_choice_set_if_select(self) -> "CustomFieldConfig":
        """
        Valida que un campo de tipo select tenga definido un choice_set.
        Retorna la configuración actual si es válida.
        """
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
        """
        Valida las configuraciones de transformación y columna de origen.
        Retorna la configuración actual si es válida.
        """
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

        if isinstance(self.source, list) and not self.transform:
            raise ValueError(
                f"Si 'source' es una lista (target '{self.target}'), "
                "debes definir explícitamente un 'transform' (ej. concat_dot, coalesce)."
            )

        return self


class StatusDefaultConfig(BaseModel):
    """Configuración de estado por defecto."""

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
    """Definición de columna CSV de origen."""

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
        """
        Inicializa los valores vacíos y el mapa indexado de Custom Fields O(1).
        """
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
        """
        Valida que las referencias cruzadas dentro de la configuración sean correctas.
        Retorna la configuración completa si es válida.
        """
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
        """
        Busca la definición de la columna a partir de su nombre exacto (source) en el CSV.
        Retorna la definición de la columna si existe, None en caso contrario.
        """
        for col_def in self.csv_columns.values():
            if col_def.source == source_name:
                return col_def
        return None

    def _apply_map(
        self, col_def: CsvColumnDef | None, value: str, identifier: str, strict: bool
    ) -> Any:
        """
        Aplica un diccionario de mapeo a un valor dado.
        Retorna el valor mapeado, o lanza una excepción si strict=True y no hay coincidencia.
        """
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
        Aplica el mapa de transformación de la columna identificada por su alias.
        Retorna el valor transformado.
        """
        col_def = self.csv_columns.get(alias)
        return self._apply_map(col_def, value, f"columna '{alias}'", strict)

    def map_value_by_source(self, source: str, value: str, strict: bool = True) -> Any:
        """
        Igual que map_value, pero busca la columna por su nombre exacto (source) en el CSV.
        Retorna el valor transformado.
        """
        col_def = self.get_column_def_by_source(source)
        return self._apply_map(col_def, value, f"columna de origen '{source}'", strict)

    def get_custom_field_def(self, target: str) -> CustomFieldConfig | None:
        """Retorna la definición de un Custom Field en O(1) por nombre de target."""
        return self._custom_field_defs_map.get(target)

    def get_all_custom_field_defs(self) -> list[CustomFieldConfig]:
        """Retorna la lista completa de definiciones de Custom Field."""
        return list(self._custom_field_defs_map.values())

    def is_empty(self, value: Any) -> bool:
        """
        Determina si un valor es considerado vacío según empty_values.
        Retorna True si el valor se considera vacío, False de lo contrario.
        """
        if value is None:
            return True
        return str(value).strip().lower() in self._empty_values_set

    def is_cluster_host(self, machine_type: str) -> bool:
        """Determina si un machine_type (ya mapeado) es un host de clúster (hipervisor)."""
        col_def = self.csv_columns.get("machine_type")
        return bool(
            col_def
            and col_def.cluster_host_types
            and machine_type in col_def.cluster_host_types
        )

    def is_virtual_machine(self, machine_type: str) -> bool:
        """Determina si un machine_type (ya mapeado) corresponde a una máquina virtual."""
        col_def = self.csv_columns.get("machine_type")
        return bool(
            col_def
            and col_def.virtual_machine_types
            and machine_type in col_def.virtual_machine_types
        )

    def resolve_node_type(self, machine_type: str) -> NodeType:
        """
        Resuelve el NodeType basándose en las listas semánticas.
        Lanza RowValidationError si el campo está vacío.
        Retorna el NodeType correspondiente.
        """
        if not machine_type:
            raise RowValidationError("El campo 'machine_type' está vacío.")

        mapped_type = self.map_value("machine_type", machine_type, strict=True)

        if self.is_virtual_machine(mapped_type):
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

    manufacturers: NameCache = Field(default_factory=dict)
    device_types: ManufModelCache = Field(default_factory=dict)
    platforms: NameCache = Field(default_factory=dict)
    locations: SiteNameCache = Field(default_factory=dict)
    racks: SiteNameCache = Field(default_factory=dict)
    clusters: NameCache = Field(default_factory=dict)
    cluster_types: NameCache = Field(default_factory=dict)
    device_roles: NameCache = Field(default_factory=dict)
    host_devices: HostDeviceCache = Field(default_factory=dict)


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


def generate_fallback_slug(base_slug: str, original_name: str) -> str:
    """Genera un slug de respaldo determinista usando un hash md5 corto."""
    hash_suffix = hashlib.md5(original_name.encode("utf-8")).hexdigest()[:4]
    return f"{base_slug}-{hash_suffix}"


def _is_custom_field_changed(curr_val: Any, new_val: Any) -> bool:
    """Determina si un payload de custom_fields presenta cambios reales."""
    if not isinstance(new_val, dict):
        return curr_val != new_val
    if not isinstance(curr_val, dict):
        return True
    return any(curr_val.get(k) != v for k, v in new_val.items())


def _is_relation_changed(curr_val: Any, new_val: Any) -> bool | None:
    """
    Evalúa cambios en objetos anidados de pynetbox (FKs o Choices).
    Retorna None si el valor no califica como una relación manejable.
    """
    if curr_val is None:
        return new_val is not None

    if hasattr(curr_val, "id"):
        if new_val is None:
            return True
        if isinstance(new_val, int):
            return curr_val.id != new_val
        if isinstance(new_val, dict) and "id" in new_val:
            return curr_val.id != new_val["id"]

    if hasattr(curr_val, "value"):
        if new_val is None:
            return True
        if isinstance(new_val, str):
            return curr_val.value != new_val

    return None


def _is_field_changed(key: str, curr_val: Any, new_val: Any) -> bool:
    """Evalúa si un campo específico difiere de su valor actual."""
    if key == "custom_fields":
        return _is_custom_field_changed(curr_val, new_val)

    rel_changed = _is_relation_changed(curr_val, new_val)
    if rel_changed is not None:
        return rel_changed

    return curr_val != new_val


def check_record_changes(
    record: NetBoxObject,
    payload: NetBoxPayload,
) -> NetBoxPayload:
    """
    Determina qué campos cambiarían en el Record al aplicar el payload.
    Retorna un diccionario con las modificaciones detectadas.
    """
    updates: NetBoxPayload = {}

    for key, new_val in payload.items():
        if not hasattr(record, key) or _is_field_changed(
            key, getattr(record, key), new_val
        ):
            updates[key] = new_val

    if updates:
        obj_name = getattr(record, "name", str(record))
        log.debug("Cambios detectados en %s: %s", obj_name, updates)

    return updates


def format_diff_keys(existing_obj: NetBoxObject, diff: dict[str, Any]) -> list[str]:
    """
    Genera una lista plana de llaves modificadas a partir de un diccionario de diferencias.
    Si una llave es un diccionario (ej. 'custom_fields'), evalúa contra el objeto
    existente para extraer únicamente las subllaves específicas que cambiaron.
    """
    keys: list[str] = []
    for key, new_val in diff.items():
        if not isinstance(new_val, dict) or not hasattr(existing_obj, key):
            keys.append(key)
            continue

        curr_val = getattr(existing_obj, key, {})
        if not isinstance(curr_val, dict):
            keys.append(key)
            continue

        # Extraemos las subllaves que realmente cambiaron
        keys.extend(
            f"{key}.{sub_k}"
            for sub_k, sub_v in new_val.items()
            if curr_val.get(sub_k) != sub_v
        )

    return keys


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
    """Convierte GB (string/float) a MB (entero) usando multiplicador 1000.
    NetBox espera MB para memory/disk."""
    try:
        gb = _to_float(value)
        return round(gb * 1000)
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
            case _ as unreachable:
                assert_never(unreachable)
    except ValueError as e:
        raise RowValidationError(
            f"Valor inválido '{value}' para el campo '{target}'. {e}"
        ) from e


def concat_dot(parts: list[str], config: NetBoxMappingConfig) -> str:
    """Concatena partes no vacías con '. ' como separador."""
    clean = [p.strip() for p in parts if not config.is_empty(p)]
    return ". ".join(clean)


def _extract_raw_csv_value(
    row: CsvRow,
    col_alias: str,
    config: NetBoxMappingConfig,
    strict: bool = False,
) -> str:
    """
    Extrae y sanitiza un valor del CSV usando su alias definido en el YAML.
    Si la celda está vacía (según config.is_empty) o la columna no existe o está
    mapeada a None, retorna un string vacío (""). Si 'strict' es True y el
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
        if strict:
            raise RowValidationError(
                f"El campo obligatorio '{col_alias}' no está mapeado en la configuración."
            )
        return ""

    val = row.get(col_name, "").strip()
    val = "" if config.is_empty(val) else val

    if strict and not val:
        raise RowValidationError(
            f"El campo obligatorio '{col_alias}' está vacío en el CSV."
        )

    return val


def extract_csv_value(
    row: CsvRow,
    col_alias: str,
    config: NetBoxMappingConfig,
    strict_extract: bool = False,
    strict_map: bool = True,
    fallback: Any = None,
) -> Any:
    """
    Extrae el valor del CSV y aplica la función de mapeo (map_value)
    si existe en la configuración.
    """
    raw_val = _extract_raw_csv_value(row, col_alias, config, strict_extract)
    if not raw_val:
        return fallback if fallback is not None else raw_val

    col_def = config.csv_columns.get(col_alias)
    if col_def and col_def.map:
        mapped_val = config.map_value(col_alias, raw_val, strict=strict_map)
        if mapped_val is not None:
            return mapped_val
        return fallback if fallback is not None else raw_val
    return raw_val


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


def _log_netbox_response(
    response: requests.Response, *args: Any, **kwargs: Any
) -> None:
    """Hook para interceptar y registrar respuestas del API de NetBox."""
    log.debug(
        "[API %s] %s - Status: %s",
        response.request.method,
        response.url,
        response.status_code,
    )
    if "X-Netbox-Warning" in response.headers:
        log.warning("[API WARNING]: %s", response.headers["X-Netbox-Warning"])
    if not response.ok:
        log.debug("[API ERROR PAYLOAD]: %s", response.text)


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
    session.hooks["response"].append(_log_netbox_response)

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
            locations=nb.dcim.locations,
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
    Retorna una tupla: (cabeceras, filas), donde cada fila es
    un diccionario {cabecera: valor}.
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
# PATRÓN GET OR CREATE (CACHE)
# ============================================================


def _is_collision_error(e: RequestError, fields: tuple[str, ...]) -> bool:
    """Detecta si un RequestError (HTTP 400) se debe a colisión en campos específicos."""
    if e.req.status_code != 400:
        return False

    collision_keywords = ("already exists", "must make a unique set")

    try:
        data = e.req.json()
        if isinstance(data, dict):
            for field in fields:
                messages = data.get(field, [])
                if isinstance(messages, str):
                    messages = [messages]

                if any(
                    kw in str(msg).lower()
                    for msg in messages
                    for kw in collision_keywords
                ):
                    return True
    except (ValueError, TypeError, AttributeError):
        pass

    error_str = str(e.error).lower()
    has_field = any(field in error_str for field in fields)
    return has_field and "already exists" in error_str


def _is_slug_collision(e: RequestError) -> bool:
    """Verifica si el RequestError es causado específicamente por un slug duplicado."""
    return _is_collision_error(e, ("slug",))


def _is_name_collision(e: RequestError) -> bool:
    """Verifica si el RequestError es causado específicamente por un nombre duplicado."""
    return _is_collision_error(e, ("name", "non_field_errors"))


def _search_in_netbox(
    endpoint: Endpoint,
    filter_kwargs: dict[str, Any],
    create_kwargs: dict[str, Any],
    name: str,
    preventive_slug_search: bool,
) -> Record | None:
    """Busca un objeto en NetBox, aplicando búsqueda preventiva por slug si es requerida."""
    results: list[Record] = list(endpoint.filter(**filter_kwargs))
    if results:
        return results[0]

    if preventive_slug_search and "slug" in create_kwargs:
        slug_val = create_kwargs["slug"]
        slug_results: list[Record] = list(endpoint.filter(slug=slug_val))
        if slug_results:
            log.warning(
                "Objeto '%s' no existe por sus campos de filtro, pero su slug '%s' "
                "coincide con un objeto existente en '%s' ('%s'). Se reutiliza.",
                name,
                slug_val,
                endpoint.name,
                getattr(slug_results[0], "name", "?"),
            )
            return slug_results[0]

    return None


def _create_dry_run_mock(
    endpoint: Endpoint, name: str, create_kwargs: dict[str, Any]
) -> NetBoxObject:
    """Genera un mock del objeto para el modo Dry-Run."""
    log.info("[DRY-RUN] WOULD CREATE objeto en '%s': %s", endpoint.name, name)
    mock_kwargs = create_kwargs.copy()
    return MockNetBoxRecord(id=0, **mock_kwargs)


def _create_with_fallback_slug(
    endpoint: Endpoint,
    original_name: str,
    **kwargs: Any,
) -> Record:
    """
    Intenta crear un objeto en NetBox. Si ocurre colisión de slug (RequestError),
    genera un slug determinista de respaldo y reintenta la creación.
    Retorna el objeto creado.
    """
    slug: str = kwargs.get("slug", "")
    try:
        return cast(Record, endpoint.create(**kwargs))
    except RequestError as e:
        if not _is_slug_collision(e):
            raise

        slug_fallback = generate_fallback_slug(slug, original_name)
        log.warning(
            "Slug '%s' colisionó al crear objeto en '%s' con nombre '%s'; reintentando con '%s'.",
            slug,
            endpoint.name,
            original_name,
            slug_fallback,
        )
        kwargs["slug"] = slug_fallback
        with netbox_error_wrap(
            f"Imposible crear objeto en '{endpoint.name}' con nombre '{original_name}' "
            "debido a colisión persistente de slug o rechazo de NetBox"
        ):
            return cast(Record, endpoint.create(**kwargs))


def _execute_creation(
    endpoint: Endpoint,
    create_kwargs: dict[str, Any],
    name: str,
    use_fallback_slug: bool,
) -> Record:
    """Ejecuta la creación del objeto en NetBox, manejando fallbacks de slug."""
    if use_fallback_slug:
        obj = _create_with_fallback_slug(
            endpoint,
            name,
            **create_kwargs,
        )
    else:
        obj = cast(Record, endpoint.create(**create_kwargs))
    log.info("CREATED Objeto en '%s': %s", endpoint.name, name)
    return obj


def _handle_concurrency_backoff(
    name: str, endpoint_name: str, attempt: int, max_retries: int
) -> None:
    """Aplica backoff exponencial tras una colisión concurrente."""
    sleep_time = 0.5 * (2**attempt) + random.uniform(0, 0.5)
    log.warning(
        "Colisión de concurrencia al crear '%s' en '%s'. "
        "Reintentando en %.2fs (intento %d/%d)...",
        name,
        endpoint_name,
        sleep_time,
        attempt + 1,
        max_retries,
    )
    time.sleep(sleep_time)


def _attempt_get_or_create(
    attempt: int,
    endpoint: Endpoint,
    filter_kwargs: dict[str, Any],
    create_kwargs: dict[str, Any],
    name: str,
    dry_run: bool,
    use_fallback_slug: bool,
    skip_filter: bool,
    preventive_slug_search: bool,
    max_retries: int,
) -> NetBoxObject | None:
    """
    Ejecuta un único intento de búsqueda o creación.
    Retorna el objeto si tiene éxito, o None si hubo colisión concurrente y debe reintentarse.
    """
    if not skip_filter or attempt > 0:
        existing_obj = _search_in_netbox(
            endpoint, filter_kwargs, create_kwargs, name, preventive_slug_search
        )
        if existing_obj:
            return existing_obj

    if dry_run:
        return _create_dry_run_mock(endpoint, name, create_kwargs)

    try:
        return _execute_creation(endpoint, create_kwargs, name, use_fallback_slug)

    except RequestError as e:
        if not _is_name_collision(e) or attempt >= max_retries - 1:
            with netbox_error_wrap(
                f"No se pudo crear el objeto en '{endpoint.name}' con nombre '{name}'"
            ):
                raise

        _handle_concurrency_backoff(name, endpoint.name, attempt, max_retries)
        return None


def get_or_create_cached(
    endpoint: Endpoint,
    cache: dict[Any, NetBoxObject],
    cache_key: Any,
    filter_kwargs: dict[str, Any],
    create_kwargs: dict[str, Any],
    name: str,
    dry_run: bool,
    use_fallback_slug: bool = False,
    skip_filter: bool = False,
    preventive_slug_search: bool = False,
) -> tuple[NetBoxObject, dict[Any, NetBoxObject]]:
    """
    Motor centralizado de Identity Map (Caché) para el patrón Get-or-Create.

    Decisiones de diseño:
    - Eficiencia O(1) de Red: Evita avalanchas de peticiones HTTP (N+1 queries) contra
      NetBox manteniendo un registro en memoria de las entidades ya creadas o consultadas.
    - Prevención de Colisiones (preventive_slug_search): En NetBox, los slugs deben ser
      estrictamente únicos. Si buscamos un objeto por nombre y no lo encontramos, NetBox
      rechazará la creación si el slug generado colisiona con otro objeto (HTTP 400).
      La búsqueda preventiva por slug evita el crash permitiendo reutilizar objetos similares.
    - Flujo de Estado Explícito: Aunque el caché se muta in-place por máximo rendimiento
      (evitando copias masivas de memoria), se retorna explícitamente para mantener
      trazabilidad arquitectónica e indicarle al desarrollador que el estado fue alterado.

    Retorna:
        tuple[NetBoxObject, dict[Any, NetBoxObject]]: El objeto final y la caché propagada.
    """
    if cache_key in cache:
        log.debug("[Cache HIT] Objeto '%s' encontrado en memoria", name)
        return cache[cache_key], cache

    log.debug("[Cache MISS] Buscando '%s' en API...", name)

    MAX_RETRIES = 3
    for attempt in range(MAX_RETRIES):
        obj = _attempt_get_or_create(
            attempt,
            endpoint,
            filter_kwargs,
            create_kwargs,
            name,
            dry_run,
            use_fallback_slug,
            skip_filter,
            preventive_slug_search,
            MAX_RETRIES,
        )
        if obj is not None:
            cache[cache_key] = obj
            return obj, cache

    raise NetBoxApiError(
        f"No se pudo resolver '{name}' después de {MAX_RETRIES} intentos."
    )


# ============================================================
# CUSTOM FIELDS: ensure_custom_fields
# ============================================================


def _normalize_choices(extra_choices: Any) -> list[list[str]]:
    """Convierte las opciones de un choice set a una lista de listas de strings."""
    if not isinstance(extra_choices, (list, tuple)):
        return []

    return [
        [str(choice[0]), str(choice[1])]
        for choice in extra_choices
        if isinstance(choice, (list, tuple)) and len(choice) >= 2
    ]


def _sync_choice_set_choices(
    choice_set: Record,
    choice_set_name: str,
    choices: list[list[str]],
    dry_run: bool,
) -> int:
    """Sincroniza las opciones (choices) de un choice set.
    Retorna el ID del choice set."""

    def _get_choice_set_id(ch_set: Record) -> int:
        """
        Busca un Choice Set por su nombre.
        Retorna el ID del Choice Set encontrado, o lanza una excepción si no existe.
        """
        return cast(int, getattr(ch_set, "id", 0))

    extra_choices: Any = getattr(choice_set, "extra_choices", None)
    current_choices: list[list[str]] = _normalize_choices(extra_choices)

    if current_choices == choices:
        return _get_choice_set_id(choice_set)

    if dry_run:
        log.info("[DRY-RUN] WOULD UPDATE Choice Set: %s", choice_set_name)
        return _get_choice_set_id(choice_set)

    with netbox_error_wrap(f"Error al actualizar Choice Set '{choice_set_name}'"):
        choice_set.update(
            {
                "extra_choices": choices,
                "order_alphabetically": False,
            }
        )
        log.info("UPDATED Choice Set: %s", choice_set_name)

    return _get_choice_set_id(choice_set)


def _ensure_choice_set(
    choice_sets_endpoint: Endpoint,
    cache: NameCache,
    choice_set_cfg: ChoiceSetConfig,
    dry_run: bool,
) -> tuple[int, NameCache]:
    """Crea un choice set si no existe en NetBox.
    Retorna el ID del choice set y la caché actualizada.
    """

    def _get_choice_set_choices(choices: list[ChoiceItemConfig]) -> list[list[str]]:
        """
        Busca un Choice Set por su nombre y obtiene sus opciones (choices).
        Retorna una lista de listas con los valores y etiquetas de cada opción.
        """
        return [[choice.value, choice.label] for choice in choices]

    choice_set_name: str = choice_set_cfg.name
    choices: list[list[str]] = _get_choice_set_choices(choice_set_cfg.choices)

    try:
        choice_set, cache = get_or_create_cached(
            endpoint=choice_sets_endpoint,
            cache=cache,
            cache_key=choice_set_name,
            filter_kwargs={"name": choice_set_name},
            create_kwargs={
                "name": choice_set_name,
                "extra_choices": choices,
                "order_alphabetically": False,
            },
            name=choice_set_name,
            dry_run=dry_run,
        )
    except NetBoxApiError as e:
        raise ConfigValidationError(
            f"Error al crear Choice Set '{choice_set_name}': {e}"
        ) from e

    if getattr(choice_set, "id", 0) != 0:
        choice_set_id = _sync_choice_set_choices(
            cast(Record, choice_set), choice_set_name, choices, dry_run
        )
        return choice_set_id, cache

    return 0, cache


def _ensure_custom_field(
    custom_fields_endpoint: Endpoint,
    cache: NameCache,
    cf_def: CustomFieldConfig,
    object_types: list[str],
    choice_set_id: int | None,
    dry_run: bool,
) -> tuple[NetBoxObject, NameCache]:
    """
    Crea un custom field si no existe en NetBox.
    Retorna el objeto Custom Field y el caché actualizado.
    """
    name: str = cf_def.name

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
        return get_or_create_cached(
            endpoint=custom_fields_endpoint,
            cache=cache,
            cache_key=name,
            filter_kwargs={"name": name},
            create_kwargs=create_kwargs,
            name=name,
            dry_run=dry_run,
        )
    except NetBoxApiError as e:
        raise ConfigValidationError(f"Error al crear custom field '{name}': {e}") from e


def ensure_custom_fields(
    endpoints: NetBoxEndpoints,
    cfg: NetBoxMappingConfig,
    dry_run: bool,
) -> NameCache:
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

    cfs_cache: NameCache = {str(cf.name): cf for cf in custom_fields}
    choice_sets_cache: NameCache = {str(ch_set.name): ch_set for ch_set in choice_sets}

    # Obtener lista unificada de definiciones de Custom Field O(1).
    cf_definitions: list[CustomFieldConfig] = cfg.get_all_custom_field_defs()

    ensured_cfs: NameCache = {}

    for cf_def in cf_definitions:
        choice_set_id: int | None = None
        choice_set_cfg: ChoiceSetConfig | None = cf_def.choice_set

        if cf_def.type == "select" and choice_set_cfg:
            choice_set_id, choice_sets_cache = _ensure_choice_set(
                endpoints.choice_sets,
                choice_sets_cache,
                choice_set_cfg,
                dry_run,
            )

        # Crear el Custom Field si no existe.
        cf_obj, cfs_cache = _ensure_custom_field(
            endpoints.custom_fields,
            cfs_cache,
            cf_def,
            cf_def.object_types,
            choice_set_id,
            dry_run,
        )
        ensured_cfs[cf_def.name] = cf_obj

    return ensured_cfs


# ============================================================
# TAXONOMÍA: ensure_* (GET o CREATE)
# ============================================================


def ensure_site(
    sites_endpoint: Endpoint,
    site_cfg: SiteConfig,
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el Site definido en el YAML exista en NetBox.
    Retorna el objeto Site creado."""
    name = site_cfg.name
    slug = cast(str, site_cfg.slug)

    # Utilizamos un caché efímero solo para reutilizar la lógica de get_or_create_cached,
    # aunque realmente site se evalúa una sola vez por ejecución en _execute_pipeline.
    obj, _ = get_or_create_cached(
        endpoint=sites_endpoint,
        cache={},
        cache_key=name,
        filter_kwargs={"name": name},
        create_kwargs={"name": name, "slug": slug},
        name=name,
        dry_run=dry_run,
    )
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
        if not config.is_cluster_host(machine_type):
            continue

        cluster_name = extract_csv_value(row, "cluster_name", config)
        hypervisor_os = extract_csv_value(row, "hypervisor_os", config)

        if not cluster_name or not hypervisor_os:
            continue

        # Comprobar si existe un conflicto de SO en el clúster.
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


def _ensure_cluster_type(
    cluster_type_endpoint: Endpoint,
    name: str,
    slug: str,
    cache: dict[Any, NetBoxObject],
    dry_run: bool,
) -> tuple[NetBoxObject, dict[Any, NetBoxObject]]:
    """Garantiza que el ClusterType exista en NetBox.
    Retorna la tupla (ClusterType, caché actualizada)"""
    return get_or_create_cached(
        endpoint=cluster_type_endpoint,
        cache=cache,
        cache_key=name,
        filter_kwargs={"name": name},
        create_kwargs={"name": name, "slug": slug},
        name=name,
        dry_run=dry_run,
    )


def ensure_dynamic_cluster_types(
    endpoints: NetBoxEndpoints,
    cluster_type_map: dict[str, str],
    fallback_cfg: ClusterTypeConfig,
    cache: NameCache,
    dry_run: bool,
) -> tuple[NetBoxObject, NameCache]:
    """
    Crea los ClusterTypes dinámicos en NetBox a partir de los valores
    únicos de hypervisor_os y los almacena en el caché.

    Siempre garantiza el fallback estático del YAML como respaldo
    para clústeres sin información de SO.

    Retorna una tupla (ClusterType fallback, cache actualizado).
    """
    # Garantizar el fallback estático del YAML.
    fallback_name = fallback_cfg.default.name
    fallback_slug = cast(str, fallback_cfg.default.slug)
    fallback, cache = _ensure_cluster_type(
        endpoints.cluster_types, fallback_name, fallback_slug, cache, dry_run
    )

    # Crear ClusterTypes dinámicos (uno por cada SO único).
    # La caché en get_or_create_cached evita N+1 requests
    unique_os_names: set[str] = set(cluster_type_map.values())
    for os_name in sorted(unique_os_names):
        os_slug = slugify(os_name)
        _, cache = _ensure_cluster_type(
            endpoints.cluster_types, os_name, os_slug, cache, dry_run
        )

    return fallback, cache


def _sync_single_device_role(
    endpoints: NetBoxEndpoints,
    role_def: DeviceRoleConfig,
    device_roles_cache: NameCache,
    dry_run: bool,
) -> tuple[NetBoxObject, NameCache]:
    """
    Sincroniza un único DeviceRole y asegura que permita VMs.
    Retorna una tupla (DeviceRole, caché actualizado).
    """
    name = role_def.name
    key = name.lower()
    slug = cast(str, role_def.slug)

    try:
        obj, device_roles_cache = get_or_create_cached(
            endpoint=endpoints.device_roles,
            cache=device_roles_cache,
            cache_key=key,
            filter_kwargs={"name": name},
            create_kwargs={
                "name": name,
                "slug": slug,
                "color": role_def.color,
                "vm_role": True,
            },
            name=name,
            dry_run=dry_run,
        )
    except NetBoxApiError as e:
        raise ConfigValidationError(f"Error procesando DeviceRole '{name}': {e}") from e

    if getattr(obj, "id", 0) != 0 and not getattr(obj, "vm_role", False):
        if dry_run:
            log.info("[DRY-RUN] WOULD UPDATE DeviceRole, para permitir VM: %s", name)
        else:
            with netbox_error_wrap(f"Error actualizando DeviceRole '{name}'"):
                cast(Record, obj).update({"vm_role": True})
                log.info("UPDATED DeviceRole, para permitir VM: %s", name)

    return obj, device_roles_cache


def ensure_all_device_roles(
    endpoints: NetBoxEndpoints,
    device_roles: list[DeviceRoleConfig],
    device_roles_cache: NameCache,
    dry_run: bool,
) -> tuple[NameCache, NameCache]:
    """
    Garantiza que todos los device roles definidos en el YAML
    existen en NetBox (/api/dcim/device-roles/).
    Todos los roles se habilitan para su uso en Virtual Machines.
    Retorna una tupla (Roles asegurados, caché actualizado).
    """
    ensured_roles: NameCache = {}
    for role_def in device_roles:
        key = role_def.name.lower()
        obj, device_roles_cache = _sync_single_device_role(
            endpoints, role_def, device_roles_cache, dry_run
        )
        ensured_roles[key] = obj
    return ensured_roles, device_roles_cache


def ensure_platform(
    platforms_endpoint: Endpoint,
    name: str,
    cache: NameCache,
    dry_run: bool,
) -> tuple[NetBoxObject, NameCache]:
    """Garantiza que el Platform exista en NetBox.
    Retorna el objeto Platform creado y la caché actualizada."""
    slug = slugify(name)
    return get_or_create_cached(
        endpoint=platforms_endpoint,
        cache=cache,
        cache_key=name,
        filter_kwargs={"name": name},
        create_kwargs={"name": name, "slug": slug},
        name=name,
        dry_run=dry_run,
        use_fallback_slug=True,
        preventive_slug_search=True,
    )


def ensure_cluster(
    clusters_endpoint: Endpoint,
    name: str,
    cluster_type: NetBoxObject,
    cache: NameCache,
    dry_run: bool,
) -> tuple[NetBoxObject, NameCache]:
    """Garantiza que el Cluster exista en NetBox.
    Retorna el objeto Cluster creado y la caché actualizada."""
    cache_key = name
    cluster_type_id = get_netbox_object_id(cluster_type)

    return get_or_create_cached(
        endpoint=clusters_endpoint,
        cache=cache,
        cache_key=cache_key,
        filter_kwargs={"name": name},
        create_kwargs={"name": name, "type": cluster_type_id},
        name=name,
        dry_run=dry_run,
    )


def ensure_manufacturer(
    manufacturers_endpoint: Endpoint,
    name: str,
    cache: NameCache,
    dry_run: bool,
) -> tuple[NetBoxObject, NameCache]:
    """Garantiza que el Manufacturer exista en NetBox.
    Retorna el objeto Manufacturer creado y la caché actualizada."""
    slug = slugify(name)
    return get_or_create_cached(
        endpoint=manufacturers_endpoint,
        cache=cache,
        cache_key=name,
        filter_kwargs={"name": name},
        create_kwargs={"name": name, "slug": slug},
        name=name,
        dry_run=dry_run,
        use_fallback_slug=True,
        preventive_slug_search=True,
    )


def _sync_device_type_u_height(
    existing_dt: Record,
    model: str,
    target_height: float,
    dry_run: bool,
) -> bool:
    """
    Sincroniza la altura en U del modelo de servidor.
    Actualiza el objeto in-place si es necesario.
    Retorna True si el objeto fue (o habría sido) actualizado, False en caso contrario.
    """
    current_val = getattr(existing_dt, "u_height", 1)
    current_height = float(current_val) if current_val is not None else 1.0

    if current_height == target_height:
        return False

    if dry_run:
        log.info(
            "[DRY-RUN] WOULD UPDATE u_height de DeviceType '%s' (de %g a %g)",
            model,
            current_height,
            target_height,
        )
        return True

    with netbox_error_wrap(f"Error actualizando u_height de DeviceType '{model}'"):
        existing_dt.update({"u_height": target_height})
        log.info(
            "UPDATED DeviceType '%s' u_height a %g",
            model,
            target_height,
        )
        return True


def ensure_device_type(
    device_types_endpoint: Endpoint,
    manufacturer: NetBoxObject,
    model: str,
    u_height: float,
    cache: ManufModelCache,
    dry_run: bool,
) -> tuple[NetBoxObject, ManufModelCache]:
    """Garantiza que el DeviceType exista en NetBox.
    Retorna el objeto DeviceType creado y la caché actualizada."""
    manufacturer_id = get_netbox_object_id(manufacturer)

    # El ID numérico se retiene solo como fallback.
    manufacturer_name = str(getattr(manufacturer, "name", manufacturer_id))
    key = (manufacturer_name, model)

    slug = slugify(f"{manufacturer_name} {model}")

    obj, cache = get_or_create_cached(
        endpoint=device_types_endpoint,
        cache=cache,
        cache_key=key,
        filter_kwargs={"model": model, "manufacturer_id": manufacturer_id},
        create_kwargs={
            "model": model,
            "slug": slug,
            "manufacturer": manufacturer_id,
            "u_height": u_height,
        },
        name=f"{manufacturer_name} / {model}",
        dry_run=dry_run,
        use_fallback_slug=True,
        preventive_slug_search=True,
        skip_filter=(manufacturer_id == 0),
    )

    if getattr(obj, "id", 0) != 0:
        _sync_device_type_u_height(cast(Record, obj), model, u_height, dry_run)
    return obj, cache


def ensure_location(
    locations_endpoint: Endpoint,
    name: str,
    site: NetBoxObject,
    cache: SiteNameCache,
    dry_run: bool,
) -> tuple[NetBoxObject, SiteNameCache]:
    """Garantiza que el Location (Fila) exista en NetBox.
    Retorna el objeto Location creado y la caché actualizada."""
    site_id = get_netbox_object_id(site)
    cache_key = (site_id, name)

    return get_or_create_cached(
        endpoint=locations_endpoint,
        cache=cache,
        cache_key=cache_key,
        filter_kwargs={"name": name, "site_id": site_id},
        create_kwargs={"name": name, "site": site_id},
        name=name,
        dry_run=dry_run,
        skip_filter=(site_id == 0),
    )


def _sync_rack_location(
    existing_rack: Record,
    name: str,
    target_location_id: int,
    dry_run: bool,
) -> bool:
    """
    Sincroniza la location (fila) del rack.
    Actualiza el objeto in-place si es necesario.
    Retorna True si el objeto fue (o habría sido) actualizado, False en caso contrario.
    """
    # Pynetbox devuelve las referencias a objetos relacionales como diccionarios o Record
    current_loc = getattr(existing_rack, "location", None)
    current_loc_id = getattr(current_loc, "id", None) if current_loc else None

    if current_loc_id == target_location_id:
        return False

    if dry_run:
        log.info(
            "[DRY-RUN] WOULD UPDATE rack '%s' con location_id=%s (actual=%s)",
            name,
            target_location_id,
            current_loc_id,
        )
        return True

    with netbox_error_wrap(f"Error actualizando location de rack '{name}'"):
        existing_rack.update({"location": target_location_id})
        log.info(
            "UPDATED rack '%s': location %s -> %s",
            name,
            current_loc_id,
            target_location_id,
        )
        return True


def ensure_rack(
    racks_endpoint: Endpoint,
    name: str,
    site: NetBoxObject,
    cache: SiteNameCache,
    dry_run: bool,
    location_id: int | None = None,
) -> tuple[NetBoxObject, SiteNameCache]:
    """Garantiza que el Rack exista en NetBox y esté asociado a la Location indicada.
    Retorna el objeto Rack creado/actualizado y la caché actualizada."""
    site_id = get_netbox_object_id(site)
    cache_key = (site_id, name)

    create_kwargs = {"name": name, "site": site_id}
    if location_id is not None:
        create_kwargs["location"] = location_id

    obj, cache = get_or_create_cached(
        endpoint=racks_endpoint,
        cache=cache,
        cache_key=cache_key,
        filter_kwargs={"name": name, "site_id": site_id},
        create_kwargs=create_kwargs,
        name=name,
        dry_run=dry_run,
        skip_filter=(site_id == 0),
    )

    # Si ya existía, garantizamos que tenga la location correcta (Idempotencia)
    if getattr(obj, "id", 0) != 0 and location_id is not None:
        _sync_rack_location(cast(Record, obj), name, location_id, dry_run)

    return obj, cache


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
    """Resuelve un campo multi-columna vía concatenación con punto.

    Retorna la concatenación de los valores de las columnas source.
    Si todas las columnas source están vacías, retorna un string vacío.
    """
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
    Retorna el primer valor encontrado, o un string vacío si ninguno tiene valor.
    """
    values = []
    for s in source:
        val = row.get(s, "").strip()
        if not config.is_empty(val):
            values.append(val)

    # Eliminar duplicados para evitar falso conflicto si ambas columnas tienen el mismo valor exacto
    unique_values = list(dict.fromkeys(values))

    if len(unique_values) > 1:
        raise RowValidationError(
            f"Conflicto: Múltiples valores distintos no nulos {unique_values} "
            f"para un campo coalesce provenientes de las columnas {source}."
        )

    return unique_values[0] if unique_values else ""


def _extract_raw_source_value(row: CsvRow, source: str | list[str]) -> str:
    """Extrae el valor crudo de la(s) columna(s) origen, sin transformar."""
    if isinstance(source, list):
        raise TypeError(
            "No se puede extraer un valor crudo a partir de una lista de fuentes "
            "sin usar una función de transformación explícita."
        )
    return row.get(source, "")


def _validate_select_choice(
    value: FieldValue,
    custom_field_def: CustomFieldConfig | None,
    target: str,
    is_optional: bool,
) -> FieldValue:
    """
    Valida value contra choice_set cuando el Custom Field es de tipo
    'select' o 'multiselect'. No-op para cualquier otro tipo de campo.
    Retorna el valor transformado o lanza RowValidationError si falla.
    """
    if not (
        custom_field_def
        and custom_field_def.type in ("select", "multiselect")
        and custom_field_def.choice_set
    ):
        return value

    valid_choices = [c.value for c in custom_field_def.choice_set.choices]
    is_multi = custom_field_def.type == "multiselect"
    invalid_val: FieldValue = None

    if is_multi:
        parts = [p.strip() for p in str(value).split(",") if p.strip()]
        invalid_val = [p for p in parts if p not in valid_choices]
        if not invalid_val:
            return parts
    else:
        if value in valid_choices:
            return value
        invalid_val = value

    # Manejo unificado de fallbacks y errores
    msg_val = (
        f"Valores {invalid_val} no son válidos"
        if is_multi
        else f"Valor '{invalid_val}' no es válido"
    )
    msg_type = " multiselect" if is_multi else ""

    log.warning(
        "%s para el Custom Field%s '%s'. Opciones válidas: %s.",
        msg_val,
        msg_type,
        target,
        valid_choices,
    )

    if is_optional:
        return None

    err_prefix = "Valores inválidos" if is_multi else "Valor inválido"
    raise RowValidationError(
        f"{err_prefix} '{invalid_val}' para el campo requerido '{target}'."
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
    Retorna el valor transformado o lanza RowValidationError si falla.
    """
    source = field_def.source
    target = field_def.target

    # 1. Extracción Cruda o Transformación
    if isinstance(source, list) and field_def.transform == "concat_dot":
        raw_value = _extract_concat_dot_value(row, source, config)
    elif isinstance(source, list) and field_def.transform == "coalesce":
        raw_value = _extract_coalesce_value(row, source, config)
    else:
        raw_value = _extract_raw_source_value(row, source)

    # 2. Resolución de Vacíos y Defaults (Centralizado)
    if config.is_empty(raw_value):
        return _resolve_default_or_empty(default, is_optional, target)

    # 3. Procesamiento de campos simples (los transformados saltan el mapeo por diccionario)
    value: FieldValue = raw_value
    if isinstance(source, str):
        value = config.map_value_by_source(source, raw_value, strict=True)

    # 4. Validaciones y Casts (se aplican siempre al final del pipeline)
    value = _validate_select_choice(value, custom_field_def, target, is_optional)
    if field_def.cast:
        value = apply_cast(value, field_def.cast, target)

    if value != raw_value:
        log.debug("Mapeo %s: '%s' casteado a -> '%s'", target, raw_value, value)

    return value


def _assign_if_valid(
    target_payload: dict[str, Any], fd: FieldMappingConfig, val: FieldValue
) -> dict[str, Any]:
    """Sanitiza y asigna el valor al payload solo si es válido.
    Retorna el payload actualizado."""
    if fd.is_unique and val == "":
        val = None

    if val is not None:
        target_payload[fd.target] = val

    return target_payload


def _process_maps(
    config: NetBoxMappingConfig,
    row: CsvRow,
    maps: list[FieldMappingConfig],
    target_payload: dict[str, Any],
    is_custom: bool,
) -> dict[str, Any]:
    """Procesa una lista de mapeos y puebla el payload destino.
    Retorna el payload actualizado."""
    for fd in maps:
        cf_def = None
        default = None
        is_optional = not fd.required

        if is_custom:
            cf_def = config.get_custom_field_def(fd.target)
            if cf_def is None:
                raise ConfigValidationError(
                    f"Error crítico de configuración: El custom_mapping target "
                    f"'{fd.target}' no está definido en custom_field_definitions."
                )
            default = cf_def.default
            is_optional = not cf_def.required

        value = _resolve_field_value(
            row,
            fd,
            config,
            is_optional=is_optional,
            default=default,
            custom_field_def=cf_def,
        )
        target_payload = _assign_if_valid(target_payload, fd, value)

    return target_payload


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

    Retorna el payload transformado.
    """
    payload: NetBoxPayload = {}
    cf_payload: CustomFieldsPayload = {}

    payload = _process_maps(config, row, native_maps, payload, is_custom=False)
    cf_payload = _process_maps(config, row, custom_maps, cf_payload, is_custom=True)

    if cf_payload:
        payload["custom_fields"] = cf_payload

    log.debug("Payload construido: %s", payload)
    return payload


# ============================================================
# RESOLVERS GENERALES (PARA SINCRONIZACIÓN DE OBJETOS)
# ============================================================


def _resolve_platform(
    plt_endpoint: Endpoint,
    row: CsvRow,
    plt_cache: NameCache,
    dry_run: bool,
    config: NetBoxMappingConfig,
) -> tuple[int | None, NameCache]:
    """Resuelve el Platform desde la columna OS.
    Retorna una tupla con el ID del Platform y el Platform cache actualizado."""
    plt_name = extract_csv_value(row, "os", config)
    if not plt_name:
        return None, plt_cache

    platform, plt_cache = ensure_platform(
        plt_endpoint,
        plt_name,
        plt_cache,
        dry_run,
    )
    return get_netbox_object_id(platform), plt_cache


def _resolve_device_role(
    row: CsvRow,
    roles_cache: NameCache,
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


def _resolve_netbox_status(
    row: CsvRow, config: NetBoxMappingConfig, node_type: NodeType
) -> str:
    """
    Resuelve el status NetBox a partir de la columna 'Estado'.

    Si el valor no existe en status_map, se aplica el fallback
    correspondiente al tipo de nodo.

    Retorna el status NetBox.
    """
    node_cfg = config.node_types.get_config(node_type)
    return extract_csv_value(row, "status", config, fallback=node_cfg.status.default)


def _resolve_cluster(
    cluster_endpoint: Endpoint,
    row: CsvRow,
    cluster_type_map: dict[str, str],
    cluster_type_cache: NameCache,
    fallback_cluster_type: NetBoxObject,
    cluster_cache: NameCache,
    dry_run: bool,
    config: NetBoxMappingConfig,
) -> tuple[int | None, NameCache]:
    """
    Resuelve el Cluster desde la columna correspondiente.

    Determina el ClusterType correcto usando el mapa pre-computado
    (cluster_name → hypervisor_os → ClusterType). Si el clúster no
    tiene una tecnología asociada, usa el fallback genérico del YAML.

    Retorna una tupla con el ID del Cluster y el Cluster cache actualizado.
    """
    cluster_name = extract_csv_value(row, "cluster_name", config)
    if not cluster_name:
        return None, cluster_cache

    # Resolver el ClusterType correcto para este clúster.
    os_name = cluster_type_map.get(cluster_name)
    cluster_type = cluster_type_cache[os_name] if os_name else fallback_cluster_type

    cluster, cluster_cache = ensure_cluster(
        cluster_endpoint,
        cluster_name,
        cluster_type,
        cluster_cache,
        dry_run,
    )
    return get_netbox_object_id(cluster), cluster_cache


def _resolve_device_type(
    endpoints: NetBoxEndpoints,
    row: CsvRow,
    manufacturer: str,
    model: str,
    caches: CacheStore,
    config: NetBoxMappingConfig,
    dry_run: bool,
) -> tuple[int, CacheStore]:
    """
    Resuelve y retorna el ID del DeviceType utilizando el manufacturer y el modelo.
    Si la altura ('hei_u') no se proporciona o es inválida, asume 1 por defecto.
    Retorna una tupla con el ID del DeviceType y la caché (CacheStore) actualizada.
    """

    # Manufacturer.
    manufacturer_obj, caches.manufacturers = ensure_manufacturer(
        endpoints.manufacturers,
        manufacturer,
        caches.manufacturers,
        dry_run,
    )

    raw_u_height = extract_csv_value(row, "hei_u", config)
    if not raw_u_height:
        u_height = 1.0
    else:
        try:
            u_height = parse_float(raw_u_height)
        except ValueError:
            raise RowValidationError(
                f"Valor numérico inválido '{raw_u_height}' para 'hei_u'."
            )

    device_type, caches.device_types = ensure_device_type(
        endpoints.device_types,
        manufacturer_obj,
        model,
        u_height,
        caches.device_types,
        dry_run,
    )
    return get_netbox_object_id(device_type), caches


def _resolve_host_device(
    devices_endpoint: Endpoint,
    row: CsvRow,
    site: NetBoxObject,
    machine_name: str,
    cache: dict[tuple[int, str], int | None],
    config: NetBoxMappingConfig,
) -> tuple[int | None, dict[tuple[int, str], int | None]]:
    """
    Resuelve y cachea el ID del Device correspondiente al hipervisor host,
    acotado estrictamente al site configurado.
    Retorna una tupla con el ID del host (o None) y la caché actualizada.
    """
    host_name_csv = extract_csv_value(row, "host_device", config)
    if not host_name_csv:
        return None, cache

    site_id = get_netbox_object_id(site)
    cache_key = (site_id, host_name_csv)

    # Solo buscar si no existe en caché.
    if cache_key not in cache:
        try:
            host_devices: list[Record] = []
            if site_id != 0:
                host_devices = list(
                    devices_endpoint.filter(name=host_name_csv, site_id=site_id)
                )

            device_id: int | None = (
                get_netbox_object_id(host_devices[0]) if host_devices else None
            )
            cache[cache_key] = device_id
        except Exception as e:
            raise NetBoxApiError(
                f"ERROR ({machine_name}): falló la consulta del host_device '{host_name_csv}' "
                f"en Site (ID: {site_id}): {e}"
            ) from e

    dev_id = cache[cache_key]

    if dev_id is None:
        log.warning(
            "ADVERTENCIA (%s): El dispositivo host '%s' no se encontró en el site. "
            "La VM se creará sin asignación de host.",
            machine_name,
            host_name_csv,
        )

    return dev_id, cache


# ============================================================
# SINCRONIZACIÓN DE OBJETOS (DEVICES y VMS)
# ============================================================


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
) -> tuple[BaseNodeData, CacheStore]:
    """
    Resuelve los campos comunes entre device y virtual_machine.
    Retorna una tupla con los datos del nodo base y la caché actualizada.
    """
    machine_name = extract_csv_value(row, "machine_name", config, strict_extract=True)
    uuid = extract_csv_value(row, "inventory_uuid", config).lower()
    machine_type = extract_csv_value(row, "machine_type", config, strict_extract=True)

    payload = build_payload(row, native_maps, custom_maps, config)

    platform_id, caches.platforms = _resolve_platform(
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
    }, caches


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
    Ejecuta la operación final de sincronización (CREATE, UPDATE, UNCHANGED) contra NetBox.

    Decisiones de diseño:
    - Confianza delegada: Si una fila llega a esta función con la lista `existing` poblada,
      asumimos que ya superó la validación estricta de unicidad previa.
    - Idempotencia y Optimización: Antes de emitir un UPDATE a la API, evaluamos
      las diferencias (diff) localmente. Si no hay cambios reales, se clasifica
      como UNCHANGED para ahorrar ancho de banda y tiempo de procesamiento.

    Retorna:
        SyncResult: Resultado estructurado con el estado (CREATED/UPDATED/UNCHANGED),
        el ID del objeto final en NetBox, y la instancia del objeto (mock en caso
        de creaciones bajo dry-run).
    """
    uuid = raw_uuid or "N/A"
    node_type = get_node_type_from_object(endpoint)

    if not existing:
        if dry_run:
            log.info(
                "[DRY-RUN] WOULD CREATE %s: %s (UUID=%s)",
                node_type,
                machine_name,
                uuid,
            )
            return SyncResult(SyncStatus.CREATED, 0, None)

        with netbox_error_wrap(f"Error al crear {node_type} '{machine_name}'"):
            obj = cast(Record, endpoint.create(**payload))
        obj_id = get_netbox_object_id(obj)
        log.info("CREATED %s: %s (ID=%d)", node_type, machine_name, obj_id)
        return SyncResult(SyncStatus.CREATED, obj_id, obj)

    existing_obj = existing[0]
    existing_id = get_netbox_object_id(existing_obj)

    # Evaluación de diferencias para optimizar red
    diff = check_record_changes(existing_obj, payload)

    if not diff:
        prefix = "[DRY-RUN] " if dry_run else ""
        log.info(
            "%sUNCHANGED %s: %s (UUID=%s)",
            prefix,
            node_type,
            machine_name,
            uuid,
        )
        return SyncResult(SyncStatus.UNCHANGED, existing_id, existing_obj)

    if dry_run:
        log.info(
            "[DRY-RUN] WOULD UPDATE %s: %s (UUID=%s) - Cambios: %s",
            node_type,
            machine_name,
            uuid,
            format_diff_keys(existing_obj, diff),
        )
        return SyncResult(SyncStatus.UPDATED, existing_id, existing_obj)

    with netbox_error_wrap(f"Error al actualizar {node_type} '{machine_name}'"):
        updated = existing_obj.update(diff)

    if updated:
        log.info("UPDATED %s: %s", node_type, machine_name)
        return SyncResult(SyncStatus.UPDATED, existing_id, existing_obj)

    # Fallback si updated == False pero diff no estaba vacío (comportamiento defensivo)
    log.info("UNCHANGED %s: %s", node_type, machine_name)
    return SyncResult(SyncStatus.UNCHANGED, existing_id, existing_obj)


def _validate_sync(
    endpoint: Endpoint,
    payload: NetBoxPayload,
    machine_name: str,
    uuid: str,
    csv_name_counts: Counter[str],
    config: NetBoxMappingConfig,
    dry_run: bool,
) -> SyncResult:
    """
    Coordina la búsqueda, validación de seguridad y ejecución de sincronización.

    Decisiones de diseño:
    - Protección contra colisiones: Si un nodo no se encuentra por su UUID único,
      se hace un fallback por 'nombre'. Sin embargo, si existen múltiples nodos
      con el mismo nombre en NetBox o en el CSV, actualizar basándose solo en
      el nombre provocaría corrupción de datos cruzada.
    - Fail-Fast: Se aborta tempranamente lanzando `RowSkipCondition` si no se puede
      garantizar la unicidad absoluta.

    Retorna:
        SyncResult: El resultado final de la operación.
    """
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
) -> tuple[SyncResult, CacheStore]:
    """
    Orquesta la sincronización completa de una fila clasificada como Device o Hipervisor.

    Decisiones de diseño:
    - Requisitos estrictos (Fail-Fast): 'manufacturer' y 'model' son campos mandatorios
      en la jerarquía de NetBox; se aborta de inmediato si faltan.
    - Constraints de API: NetBox exige que si un dispositivo tiene 'position' (U-Location),
      también debe tener obligatoriamente un 'face' (front/rear). Se inyecta preventivamente.
    - Hipervisores: Los dispositivos físicos que actúan como hipervisores deben asignarse
      a un Cluster explícito para poder albergar Virtual Machines posteriormente.

    Retorna:
        tuple[SyncResult, CacheStore]: El resultado final y el estado de la caché puramente inyectado.
    """
    # ── VALIDACIÓN TEMPRANA (Fail-Fast) ──
    manufacturer = extract_csv_value(row, "manufacturer", config)
    model = extract_csv_value(row, "model", config)
    if not manufacturer or not model:
        raise RowSkipCondition("Falta 'manufacturer' o 'model'. Requerido para Device.")

    node_cfg = config.node_types.get_config(NodeType.DEVICE)

    # Resolvemos los campos base
    base, caches = _resolve_base_node(
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

    # Cluster para hipervisores.
    if config.is_cluster_host(machine_type):
        cluster_id, caches.clusters = _resolve_cluster(
            endpoints.clusters,
            row,
            cluster_type_map,
            caches.cluster_types,
            fallback_cluster_type,
            caches.clusters,
            dry_run,
            config,
        )
        if cluster_id is not None:
            payload["cluster"] = cluster_id
        else:
            log.info("INFO (%s): Hipervisor sin cluster asignado.", machine_name)

    # Location (Fila).
    location_id: int | None = None
    location_name = extract_csv_value(row, "rack_location", config)
    if location_name:
        location_obj, caches.locations = ensure_location(
            endpoints.locations,
            location_name,
            site,
            caches.locations,
            dry_run,
        )
        location_id = get_netbox_object_id(location_obj)

    # Rack.
    rack_name = extract_csv_value(row, "rack", config)
    if rack_name:
        rack, caches.racks = ensure_rack(
            endpoints.racks,
            rack_name,
            site,
            caches.racks,
            dry_run,
            location_id=location_id,
        )
        payload["rack"] = get_netbox_object_id(rack)

    # DeviceType (busca el Manufacturer por dentro).
    device_type_id, caches = _resolve_device_type(
        endpoints,
        row,
        manufacturer,
        model,
        caches,
        config,
        dry_run,
    )
    payload["device_type"] = device_type_id

    # ── GET o CREATE/UPDATE ──────────────────────────────────
    sync_res = _validate_sync(
        endpoints.devices,
        payload,
        machine_name,
        uuid,
        csv_name_counts,
        config,
        dry_run,
    )
    return sync_res, caches


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
) -> tuple[SyncResult, CacheStore]:
    """
    Orquesta la sincronización completa de una fila clasificada como Virtual Machine.

    Decisiones de diseño:
    - Requisitos estrictos (Fail-Fast): En NetBox, toda VM DEBE pertenecer a un Cluster.
      Se descarta el registro inmediatamente si 'cluster_name' está ausente.
    - Host Device (Opcional): Se intenta asociar la VM con el hipervisor físico (Device)
      especificado. Esto permite mapear la topología virtualizada sobre el hardware real.

    Retorna:
        tuple[SyncResult, CacheStore]: El resultado final y el estado de la caché puramente inyectado.
    """
    # ── VALIDACIÓN TEMPRANA (Fail-Fast) ──
    cluster_name = extract_csv_value(row, "cluster_name", config)
    if not cluster_name:
        raise RowSkipCondition("Falta 'cluster_name'. Requerido para Virtual Machine.")

    node_cfg = config.node_types.get_config(NodeType.VIRTUAL_MACHINE)

    # Resolvemos los campos base
    base, caches = _resolve_base_node(
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
    cluster_id, caches.clusters = _resolve_cluster(
        endpoints.clusters,
        row,
        cluster_type_map,
        caches.cluster_types,
        fallback_cluster_type,
        caches.clusters,
        dry_run,
        config,
    )

    if cluster_id is not None:
        payload["cluster"] = cluster_id
    else:
        raise RowSkipCondition(f"Falló la resolución del cluster '{cluster_name}'.")

    # Device del hipervisor host (acotado a site y cacheado).
    host_dev_id, caches.host_devices = _resolve_host_device(
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
    sync_res = _validate_sync(
        endpoints.virtual_machines,
        payload,
        machine_name,
        uuid,
        csv_name_counts,
        config,
        dry_run,
    )
    return sync_res, caches


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
    status_map: dict[str, Any],
    config: NetBoxMappingConfig,
) -> NetworkInterfaceData:
    """Parsea una única interfaz aislando la lógica de validación de IPs."""
    if config.is_empty(name):
        raise RowValidationError(
            "El nombre de una interfaz de red no puede estar vacío."
        )

    def get_raw_value_or_none(raw_value: str) -> str | None:
        """
        Filtra el valor crudo.
        Retorna el valor original si no está vacío, o None en caso contrario.
        """
        return raw_value if not config.is_empty(raw_value) else None

    enabled = bool(status_map.get(status_raw.lower().strip(), True))
    ip_val = get_raw_value_or_none(ip_raw)
    pfx_val = get_raw_value_or_none(pfx_raw)
    mac_val = get_raw_value_or_none(mac_raw)

    cidr = None
    try:
        if ip_val:
            ip_val = _validate_interface_ip(ip_val, name)
            if pfx_val:
                cidr = _build_interface_cidr(ip_val, pfx_val, name)

        if mac_val:
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


def _parse_network_interfaces(
    row: CsvRow,
    config: NetBoxMappingConfig,
) -> list[NetworkInterfaceData]:
    """
    Parsea las columnas de red del CSV y retorna una lista de interfaces.
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
        """
        Divide una celda CSV separada por comas.
        Retorna una lista de strings con espacios en blanco removidos.
        """
        raw = row.get(col_name, "")
        return [v.strip() for v in raw.split(",")] if not config.is_empty(raw) else []

    # Validar nombres
    names = split_col(config.csv_columns["iface_names"].source)
    if not names:
        return []

    keys = ["iface_status", "iface_ip", "iface_pfx", "iface_mac"]
    cols = {k: split_col(config.csv_columns[k].source) for k in keys}

    max_len = len(names)
    inconsistencies = []

    for k, lst in cols.items():
        if lst and len(lst) != max_len:
            col_name = config.csv_columns[k].source
            inconsistencies.append(f"'{col_name}' tiene {len(lst)} elementos")

    if inconsistencies:
        ifaces_col = config.csv_columns["iface_names"].source
        raise RowValidationError(
            f"Discrepancia de elementos en red: se declararon {max_len} interfaces en '{ifaces_col}', "
            f"pero " + ", ".join(inconsistencies) + ". Revisa las comas."
        )

    def fill_if_empty(lst: list[str]) -> list[str]:
        """
        Asegura que la lista tenga el tamaño requerido si está vacía.
        Retorna la lista original si tiene elementos, o una lista de strings vacíos de tamaño max_len.
        """
        return lst if lst else [""] * max_len

    cols = {k: fill_if_empty(lst) for k, lst in cols.items()}

    interfaces: list[NetworkInterfaceData] = []
    status_map = config.csv_columns["iface_status"].map or {}

    for i, name in enumerate(names):
        parsed = _parse_single_network_interface(
            name=name,
            status_raw=cols["iface_status"][i],
            ip_raw=cols["iface_ip"][i],
            pfx_raw=cols["iface_pfx"][i],
            mac_raw=cols["iface_mac"][i],
            status_map=status_map,
            config=config,
        )
        interfaces.append(parsed)

    return interfaces


def parse_row_interfaces(
    row: CsvRow,
    machine_name: str,
    config: NetBoxMappingConfig,
) -> list[NetworkInterfaceData]:
    """
    Parsea estructuralmente las columnas de interfaces para la fila actual.

    Decisiones de diseño:
    - Visibilidad de Gaps: Si no se detectan interfaces, se lanza una advertencia.
      Esto indica un posible hueco en la estructura del CSV o un equipo que
      perderá su telemetría (in-band/out-of-band) en NetBox.

    Retorna:
        list[NetworkInterfaceData]: Lista de diccionarios validados con datos de red.
    """
    interfaces = _parse_network_interfaces(row, config)
    if not interfaces:
        ifaces_col = config.csv_columns["iface_names"].source
        log.warning(
            "SKIP interfaces de '%s': columna '%s' está vacía u omitida.",
            machine_name,
            ifaces_col,
        )
    return interfaces


# ============================================================
# SINCRONIZACIÓN DE INTERFACES
# ============================================================


def _assign_network_resource(
    endpoint: Endpoint,
    resource_name: str,
    search_kwargs: dict[str, Any],
    create_kwargs: dict[str, Any],
    value_str: str,
    iface_obj: NetBoxObject,
    dry_run: bool,
) -> tuple[NetBoxObject, bool]:
    """
    Centraliza la asignación polimórfica de recursos de red (IP o MAC) a una interfaz.

    Decisiones de diseño:
    - Interfaz Polimórfica: Tanto las direcciones IP como las MACs en NetBox se asignan
      mediante ContentTypes (`assigned_object_type`, `assigned_object_id`).
    - Reciclaje (Evitar Duplicidad Global): Antes de crear un nuevo recurso, se verifica
      si ya existe uno libre (huérfano) con el mismo valor, y se reasigna a la interfaz.
      Esto previene colisiones y uso excesivo de espacio en el IPAM de NetBox.

    Retorna:
        tuple[NetBoxObject, bool]: El objeto (IP/MAC) NetBox y un booleano indicando si hubo cambios.
    """
    node_type = get_node_type_from_object(iface_obj)
    assigned_type: str = (
        "dcim.interface"
        if node_type == NodeType.DEVICE
        else "virtualization.vminterface"
    )
    existing_records: list[Record] = list(endpoint.filter(**search_kwargs))

    # 1. Verificar si ya está asignado a esta interfaz
    for record in existing_records:
        current_id = getattr(record, "assigned_object_id", None)
        current_type = getattr(record, "assigned_object_type", None)
        if current_id == iface_obj.id and str(current_type) == assigned_type:
            return record, False

    # 2. Buscar si hay alguno libre para reasignar
    unassigned = next(
        (r for r in existing_records if getattr(r, "assigned_object_id", None) is None),
        None,
    )

    if unassigned:
        if dry_run:
            log.info(
                "[DRY-RUN] WOULD UPDATE %s libre %s (asignación a objeto %s)",
                resource_name,
                value_str,
                iface_obj.id,
            )
            return MockNetBoxRecord(
                id=0,
                assigned_object_id=iface_obj.id,
                assigned_object_type=assigned_type,
                **search_kwargs,
            ), True
        with netbox_error_wrap(f"Error actualizando {resource_name} libre {value_str}"):
            unassigned.update(
                {
                    "assigned_object_type": assigned_type,
                    "assigned_object_id": iface_obj.id,
                }
            )
            log.info("UPDATED %s libre reasignada: %s", resource_name, value_str)
            return unassigned, True

    # 3. Crear nuevo recurso
    if dry_run:
        log.info(
            "[DRY-RUN] WOULD CREATE nuevo %s %s (asignada a objeto %s)",
            resource_name,
            value_str,
            iface_obj.id,
        )
        return MockNetBoxRecord(
            id=0,
            assigned_object_id=iface_obj.id,
            assigned_object_type=assigned_type,
            **search_kwargs,
        ), True

    with netbox_error_wrap(f"Error creando {resource_name} {value_str}"):
        obj = cast(
            Record,
            endpoint.create(
                assigned_object_type=assigned_type,
                assigned_object_id=iface_obj.id,
                **create_kwargs,
            ),
        )

    log.info("%s CREATED y asignada: %s", resource_name, value_str)
    return obj, True


def _assign_ip(
    ip_addresses_endpoint: Endpoint,
    cidr: str,
    iface_obj: NetBoxObject,
    dry_run: bool,
) -> tuple[NetBoxObject, bool]:
    """Crea o actualiza una IP address en NetBox y la asigna a la interfaz."""
    return _assign_network_resource(
        endpoint=ip_addresses_endpoint,
        resource_name="IP",
        search_kwargs={"address": cidr},
        create_kwargs={"address": cidr, "status": "active"},
        value_str=cidr,
        iface_obj=iface_obj,
        dry_run=dry_run,
    )


def _assign_mac(
    mac_addresses_endpoint: Endpoint,
    mac_val: str,
    iface_obj: NetBoxObject,
    dry_run: bool,
) -> tuple[NetBoxObject, bool]:
    """Crea o actualiza una MAC address en NetBox y la asigna a la interfaz."""
    return _assign_network_resource(
        endpoint=mac_addresses_endpoint,
        resource_name="MAC",
        search_kwargs={"mac_address": mac_val},
        create_kwargs={"mac_address": mac_val},
        value_str=mac_val,
        iface_obj=iface_obj,
        dry_run=dry_run,
    )


def _upsert_interface_record(
    name: str,
    payload: NetBoxPayload,
    existing_obj: NetBoxObject | None,
    iface_endpoint: Endpoint,
    obj_id: int,
    dry_run: bool,
) -> tuple[NetBoxObject, bool]:
    """
    Ejecuta la lógica de creación o actualización de una interfaz.
    Retorna una tupla con (Interfaz, hubo cambios).
    """
    if not existing_obj:
        if dry_run:
            log.info("[DRY-RUN] WOULD CREATE interfaz %s en objeto %s", name, obj_id)
            return MockNetBoxRecord(id=0, name=name, **payload), True

        with netbox_error_wrap(f"Error procesando interfaz '{name}'"):
            new_obj = cast(Record, iface_endpoint.create(**payload))
            log.info("CREATED Interfaz: %s", name)
            return new_obj, True

    diff = check_record_changes(existing_obj, payload)

    if not diff:
        return existing_obj, False

    if dry_run:
        log.info(
            "[DRY-RUN] WOULD UPDATE interfaz %s en objeto %s - Cambios: %s",
            name,
            obj_id,
            format_diff_keys(existing_obj, diff),
        )
        return existing_obj, True

    with netbox_error_wrap(f"Error procesando interfaz '{name}'"):
        cast(Record, existing_obj).update(payload)
        log.info("UPDATED Interfaz: %s", name)

    return existing_obj, True


def _sync_single_interface(
    iface_data: NetworkInterfaceData,
    obj_id: int,
    iface_endpoint: Endpoint,
    ifaces_cache: NameCache,
    endpoints: NetBoxEndpoints,
    dry_run: bool,
) -> tuple[SingleInterfaceResult, NameCache]:
    """
    Sincroniza una interfaz individual y le asigna su IP y MAC.
    Retorna una tupla: (SingleInterfaceResult, caché actualizada).
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
        ifaces_cache.get(name),
        iface_endpoint,
        obj_id,
        dry_run,
    )
    ifaces_cache[name] = iface_obj

    ip_obj = None
    ip_changed = False
    if cidr := iface_data.get("cidr"):
        ip_obj, ip_changed = _assign_ip(
            endpoints.ip_addresses, cidr, iface_obj, dry_run
        )

    mac_obj = None
    mac_changed = False
    if mac := iface_data.get("mac"):
        mac_obj, mac_changed = _assign_mac(
            endpoints.mac_addresses, mac.upper(), iface_obj, dry_run
        )

    any_changes = iface_changed or ip_changed or mac_changed
    return SingleInterfaceResult(iface_obj, ip_obj, mac_obj, any_changes), ifaces_cache


def _prune_orphan_interfaces(
    ifaces_cache: NameCache,
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

    for name, iface_obj in ifaces_cache.items():
        if name in csv_iface_names:
            continue

        if dry_run:
            log.info(
                "[DRY-RUN] Eliminaría interfaz huérfana '%s' en objeto %s",
                name,
                obj_id,
            )
            deleted_count += 1
            continue

        try:
            cast(Record, iface_obj).delete()
            log.info("DELETED interfaz huérfana: %s", name)
            deleted_count += 1
        except RequestError as e:
            log.error("Error eliminando interfaz huérfana '%s': %s", name, e)
            errors += 1

    return deleted_count, errors


def _get_interface_endpoint_and_filter(
    endpoints: NetBoxEndpoints, obj_id: int, node_type: NodeType
) -> tuple[Endpoint, dict[str, Any]]:
    """Determina el endpoint y filtro correcto de interfaces según el tipo de nodo."""
    if node_type == NodeType.DEVICE:
        return endpoints.device_interfaces, {"device_id": obj_id}
    return endpoints.vm_interfaces, {"virtual_machine_id": obj_id}


def _fetch_interfaces_cache(
    iface_endpoint: Endpoint,
    iface_filter: dict[str, Any],
    obj_id: int,
) -> NameCache:
    """Obtiene las interfaces existentes de un nodo desde NetBox."""
    if obj_id == 0:
        return {}
    filtered_ifaces = list(iface_endpoint.filter(**iface_filter))
    return {str(iface.name): iface for iface in filtered_ifaces}


def _process_interfaces_sync(
    interfaces: list[NetworkInterfaceData],
    obj_id: int,
    iface_endpoint: Endpoint,
    ifaces_cache: NameCache,
    endpoints: NetBoxEndpoints,
    dry_run: bool,
) -> tuple[InterfaceSyncResult, NameCache]:
    """Ejecuta la sincronización de una lista de interfaces y recopila IPs asignadas.
    Retorna una tupla: (InterfaceSyncResult, caché actualizada)."""
    errors = 0
    ipv4_candidates: list[Ipv4Candidate] = []
    any_changes = False

    for iface_data in interfaces:
        try:
            single_result, ifaces_cache = _sync_single_interface(
                iface_data,
                obj_id,
                iface_endpoint,
                ifaces_cache,
                endpoints,
                dry_run,
            )
            iface_obj, ip_obj, mac_obj, iface_changed = single_result
            any_changes |= iface_changed

            if ip_obj is not None:
                address = getattr(ip_obj, "address", "")
                if address and ":" not in str(address):
                    ipv4_candidates.append(
                        Ipv4Candidate(int(getattr(ip_obj, "id", 0)), iface_obj, mac_obj)
                    )
        except NetBoxApiError as e:
            log.error("ERROR de API sincronizando interfaz: %s", e)
            errors += 1

    return InterfaceSyncResult(errors, ipv4_candidates, any_changes), ifaces_cache


def _log_interface_deltas(
    obj_id: int,
    interfaces: list[NetworkInterfaceData],
    ifaces_cache: dict[str, NetBoxObject],
    prune_interfaces: bool,
) -> None:
    """Calcula y registra los deltas de las interfaces antes de sincronizar."""
    csv_names = {iface["name"] for iface in interfaces}
    cache_names = set(ifaces_cache.keys())
    to_create = csv_names - cache_names
    to_update = csv_names & cache_names
    to_prune = cache_names - csv_names if prune_interfaces else set()

    log.debug(
        "Deltas de interfaces (obj_id=%s) -> Crear: %d | Actualizar: %d | Eliminar: %d",
        obj_id,
        len(to_create),
        len(to_update),
        len(to_prune),
    )


def _sync_interfaces_for_object(
    endpoints: NetBoxEndpoints,
    obj_id: int,
    node_type: NodeType,
    interfaces: list[NetworkInterfaceData],
    dry_run: bool,
    prune_interfaces: bool = False,
) -> InterfaceSyncResult:
    """Sincroniza interfaces y sus IPs para un Device o VM.
    Retorna: InterfaceSyncResult."""
    iface_endpoint, iface_filter = _get_interface_endpoint_and_filter(
        endpoints, obj_id, node_type
    )
    ifaces_cache = _fetch_interfaces_cache(iface_endpoint, iface_filter, obj_id)

    _log_interface_deltas(obj_id, interfaces, ifaces_cache, prune_interfaces)

    sync_res, ifaces_cache = _process_interfaces_sync(
        interfaces, obj_id, iface_endpoint, ifaces_cache, endpoints, dry_run
    )
    errors, ipv4_candidates, any_changes = sync_res

    if prune_interfaces and obj_id != 0:
        csv_names = {iface["name"] for iface in interfaces}
        pruned_count, prune_errors = _prune_orphan_interfaces(
            ifaces_cache, csv_names, obj_id, dry_run
        )
        errors += prune_errors
        any_changes |= pruned_count > 0

    return InterfaceSyncResult(errors, ipv4_candidates, any_changes)


def _assign_primary_resource(
    target_obj: NetBoxObject,
    field_name: str,
    resource_id: int,
    resource_val: str | int,
    log_context: str,
    dry_run: bool,
    resource_type_label: str,
) -> bool:
    """
    Helper genérico para asignar recursos primarios (IP primaria, MAC primaria).
    Retorna True si se asignó exitosamente (o se simuló en dry-run), False de lo contrario.
    """
    current_primary = getattr(target_obj, field_name, None)
    current_primary_id = (
        getattr(current_primary, "id", None) if current_primary else None
    )

    if current_primary_id == resource_id:
        return False

    if dry_run:
        log.info(
            "[DRY-RUN] WOULD UPDATE %s %s a %s",
            resource_type_label,
            resource_val,
            log_context,
        )
        return True

    try:
        cast(Record, target_obj).update({field_name: resource_id})
        log.info(
            "%s asignada a %s: %s",
            resource_type_label.capitalize(),
            log_context,
            resource_val,
        )
        return True
    except RequestError as e:
        log.error(
            "Error al actualizar %s en %s: %s", resource_type_label, log_context, e
        )
        return False


def _assign_primary_ipv4(
    main_obj: NetBoxObject,
    primary_id: int,
    machine_name: str,
    dry_run: bool,
) -> bool:
    """
    Define la dirección IPv4 indicada como IP primaria (primary_ip4) del dispositivo/VM.
    Retorna True si se asignó exitosamente (o se simuló en dry-run), False de lo contrario.
    """
    return _assign_primary_resource(
        target_obj=main_obj,
        field_name="primary_ip4",
        resource_id=primary_id,
        resource_val=f"(ID={primary_id})",
        log_context=f"nodo '{machine_name}'",
        dry_run=dry_run,
        resource_type_label="IP primaria",
    )


def _assign_primary_mac(
    iface_obj: NetBoxObject,
    mac_obj: NetBoxObject | None,
    dry_run: bool,
) -> bool:
    """
    Asigna la MAC como primaria en la interfaz si aún no lo está.
    Retorna True si se asignó exitosamente (o se simuló en dry-run), False de lo contrario.
    """
    if not mac_obj:
        return False

    mac_id = get_netbox_object_id(mac_obj)
    mac_addr = str(getattr(mac_obj, "mac_address", mac_id))
    iface_name = str(getattr(iface_obj, "name", get_netbox_object_id(iface_obj)))

    return _assign_primary_resource(
        target_obj=iface_obj,
        field_name="primary_mac_address",
        resource_id=mac_id,
        resource_val=mac_addr,
        log_context=f"interfaz '{iface_name}'",
        dry_run=dry_run,
        resource_type_label="MAC primaria",
    )


def process_interfaces_and_ips(
    endpoints: NetBoxEndpoints,
    obj_id: int,
    node_type: NodeType,
    interfaces: list[NetworkInterfaceData],
    dry_run: bool,
    prune_interfaces: bool,
    main_obj: NetBoxObject | None,
    machine_name: str,
) -> tuple[int, bool]:
    """
    Orquesta la sincronización de interfaces hijas y gestiona la asignación de redes primarias.

    Decisiones de diseño:
    - Asignación Primaria Segura: Si (y solo si) existe un único candidato IPv4/MAC válido
      para todo el nodo, se le asigna de forma automática como IP/MAC primaria del dispositivo.
      Si hay múltiples, NetBox exige resolución manual para evitar ambigüedades destructivas.

    Retorna:
        tuple[int, bool]: Una tupla con (cantidad_de_errores, boolean_hubo_cambios_reales).
    """
    iface_errors, ipv4_candidates, ifaces_changed = _sync_interfaces_for_object(
        endpoints,
        obj_id,
        node_type,
        interfaces,
        dry_run,
        prune_interfaces,
    )
    primary_ip_changed = False
    primary_mac_changed = False

    if len(ipv4_candidates) == 1:
        ip_id, iface_obj, mac_obj = ipv4_candidates[0]

        if main_obj is not None:
            primary_ip_changed = _assign_primary_ipv4(
                main_obj, ip_id, machine_name, dry_run
            )

        primary_mac_changed = _assign_primary_mac(iface_obj, mac_obj, dry_run)

    any_changes = ifaces_changed or primary_ip_changed or primary_mac_changed
    return iface_errors, any_changes


# ============================================================
# MAIN
# ============================================================


def _classify_rows(
    rows: list[CsvRow],
    config: NetBoxMappingConfig,
    counts: SyncCounts,
) -> tuple[ClassifiedRows, SyncCounts]:
    """
    Clasifica las filas del CSV separando dispositivos físicos (Devices)
    de máquinas virtuales (VMs).

    Decisiones de diseño:
    - Orden de Dependencia: En NetBox, una VM pertenece a un Cluster, el cual
      suele depender de un Host Device. Para evitar el fallo de dependencias
      circulares o rotas, garantizamos que todo el hardware físico (Devices)
      se sincronice estrictamente en una fase anterior a las VMs.
    - Tolerancia a Fallos: Atrapa errores de validación (ej. falta de OS)
      al vuelo, logueando el error y escalando el contador sin quebrar el pipeline.

    Retorna:
        tuple[ClassifiedRows, SyncCounts]: Las filas separadas y los contadores mutados.
    """
    device_rows: list[tuple[int, CsvRow]] = []
    vm_rows: list[tuple[int, CsvRow]] = []

    for row_num, row in enumerate(rows, start=2):
        try:
            node_type = get_node_type_from_row(row, config)
            if node_type == NodeType.DEVICE:
                device_rows.append((row_num, row))
            else:
                vm_rows.append((row_num, row))
        except RowValidationError as e:
            machine_name = (
                extract_csv_value(row, "machine_name", config) or f"fila {row_num}"
            )
            log.error("ERROR fila %d ('%s'): %s", row_num, machine_name, e)
            counts[SyncStatus.ERROR] += 1

    log.info(
        "Clasificación: %d device(s), %d VM(s), %d error(es) de tipo.",
        len(device_rows),
        len(vm_rows),
        counts[SyncStatus.ERROR],
    )

    return ClassifiedRows(device_rows, vm_rows), counts


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
) -> tuple[SyncResult, CacheStore]:
    """
    Actúa como despachador (dispatcher) polimórfico para sincronizar nodos (Device o VM).

    Decisiones de diseño:
    - Abstracción: Oculta la complejidad de las diferentes jerarquías requeridas por
      físicos y virtuales al pipeline principal.
    - Propagación de Caché: En caso de crear/actualizar un Device, se guarda inmediatamente
      su ID en `caches.host_devices`. Así, si una VM subsecuente en el mismo CSV lo
      declara como su hipervisor, evitamos un viaje de red costoso (API call) inyectando
      directamente el ID cacheado.

    Retorna:
        tuple[SyncResult, CacheStore]: El resultado final y la caché propagada.
    """
    if node_type == NodeType.DEVICE:
        sync_res, caches = sync_device(
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
        caches.host_devices[(site_id, machine_name)] = sync_res[1]
        return sync_res, caches
    else:
        sync_res, caches = sync_vm(
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
        return sync_res, caches


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
) -> tuple[SyncCounts, CacheStore]:
    """
    Unidad de trabajo (Unit of Work) para procesar de forma atómica una fila completa del CSV.

    Decisiones de diseño:
    - Tolerancia a Fallos (Fault Tolerance): Captura proactivamente todas las excepciones
      de dominio (Skip, Error de API, Validación) garantizando que un nodo corrupto no
      quiebre todo el pipeline ETL, permitiendo que el resto de filas se procesen.
    - Escalada de Estado: Si el objeto padre (Device/VM) no sufrió cambios (UNCHANGED),
      pero alguna de sus interfaces hijas sí fue creada o actualizada, se escala el
      estado final del nodo a UPDATED para que los reportes de ejecución reflejen
      el cambio global de la entidad.

    Retorna:
        tuple[SyncCounts, CacheStore]: El acumulador de métricas mutado y la caché propagada.
    """
    machine_name = extract_csv_value(row, "machine_name", config) or f"fila {row_num}"

    try:
        sync_res, caches = _process_node_sync(
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
        result, obj_id, main_obj = sync_res
    except ConfigValidationError as e:
        raise ConfigValidationError(
            f"Error de configuración en fila {row_num}: {e}"
        ) from e
    except RowSkipCondition as e:
        log.warning("SKIP fila %d: %s", row_num, e)
        counts[SyncStatus.SKIPPED] += 1
        return counts, caches
    except (NetBoxApiError, RowValidationError, RequestError) as e:
        log.error("ERROR de API o validación en fila %d: %s", row_num, e)
        counts[SyncStatus.ERROR] += 1
        return counts, caches
    except Exception:
        log.exception(
            "ERROR inesperado al procesar fila %d ('%s')",
            row_num,
            machine_name,
        )
        counts[SyncStatus.ERROR] += 1
        return counts, caches

    counts[result] += 1

    # ── Validar ID para interfaces (Fail-Fast) ────────────
    if not obj_id:
        if not dry_run:
            log.warning("SKIP interfaces de '%s': el objeto no tiene ID.", machine_name)
        return counts, caches

    # ── Parsear interfaces ────────────────────────────────
    try:
        interfaces = parse_row_interfaces(row, machine_name, config)
    except RowValidationError as e:
        log.error(
            "Error de validación en interfaces de '%s': %s. Se omitirán.",
            machine_name,
            e,
        )
        counts[SyncStatus.ERROR] += 1
        return counts, caches
    except Exception:
        log.exception("ERROR inesperado al parsear interfaces de '%s'", machine_name)
        counts[SyncStatus.ERROR] += 1
        return counts, caches

    # ── Sincronizar interfaces del objeto ─────────────────
    try:
        iface_errors, any_changes = process_interfaces_and_ips(
            endpoints,
            obj_id,
            node_type,
            interfaces,
            dry_run,
            prune_interfaces,
            main_obj,
            machine_name,
        )
        if iface_errors > 0:
            counts[SyncStatus.ERROR] += iface_errors

        if result == SyncStatus.UNCHANGED and any_changes and iface_errors == 0:
            counts[SyncStatus.UNCHANGED] -= 1
            counts[SyncStatus.UPDATED] += 1

    except Exception:
        log.exception(
            "ERROR inesperado al sincronizar interfaces de '%s'", machine_name
        )
        counts[SyncStatus.ERROR] += 1

    return counts, caches


def _print_summary_and_exit(
    total_rows: int,
    counts: SyncCounts,
    dry_run: bool,
) -> NoReturn:
    """
    Imprime el resumen de la operación y finaliza la ejecución.
    Finaliza con sys.exit().
    """
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
    """
    Punto de entrada principal para la sincronización del inventario con NetBox.
    """
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
    site: NetBoxObject = ensure_site(endpoints.sites, config.site, args.dry_run)

    # ── Inicializar caches ───────────────────────────────────
    caches: CacheStore = CacheStore()

    # ── Pre-escaneo: asociar clústeres con su tecnología ─────
    cluster_type_map = precompute_cluster_type_map(rows, config)
    fallback_cluster_type, caches.cluster_types = ensure_dynamic_cluster_types(
        endpoints,
        cluster_type_map,
        config.cluster_type,
        caches.cluster_types,
        args.dry_run,
    )

    # ── Garantizar taxonomía local ──────────────────────────
    _, caches.device_roles = ensure_all_device_roles(
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
    classified_rows, counts = _classify_rows(rows, config, counts)
    device_rows = classified_rows.device_rows
    vm_rows = classified_rows.vm_rows

    # ── Fase 1: Sincronizar Devices ──────────────────────────
    log.info("── Fase 1: Sincronizando %d Device(s) ──", len(device_rows))
    for row_num, row in device_rows:
        counts, caches = _sync_row(
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
        counts, caches = _sync_row(
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
