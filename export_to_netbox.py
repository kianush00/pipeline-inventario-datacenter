"""
export_to_netbox.py
===================
Exporta el inventario fusionado (merged_inventory.csv) a NetBox 4.x.

Dependencias:
    pip install pydantic>=2.0.0 pynetbox>=7.3.0 PyYAML>=6.0

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
import ipaddress
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Literal, NoReturn, TypeAlias, TypedDict, cast

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
# TYPE ALIASES Y ESTRUCTURAS DE TIPOS
# ============================================================

SyncStatus: TypeAlias = Literal["CREATED", "UPDATED", "UNCHANGED", "SKIPPED", "ERROR"]
SyncResult: TypeAlias = tuple[SyncStatus, int | None]
NodeType: TypeAlias = Literal["device", "virtual_machine"]
CastType: TypeAlias = Literal["int", "int_gb_to_mb", "bool_si_no"]
CsvRow: TypeAlias = dict[str, str]
FieldValue: TypeAlias = str | int | float | bool | None
CustomFieldsPayload: TypeAlias = dict[str, FieldValue]
NetBoxPayload: TypeAlias = dict[str, Any]
CsvColumnAliases: TypeAlias = dict[str, str]


class SyncCounts(TypedDict):
    """Contadores de resultados de la sincronización con NetBox."""

    CREATED: int
    UPDATED: int
    UNCHANGED: int
    SKIPPED: int
    ERROR: int


class NetworkInterfaceData(TypedDict):
    """Representa la estructura de datos parseada de una interfaz de red."""

    name: str
    enabled: bool
    mac: str | None
    ip: str | None
    prefix: str | None
    cidr: str | None


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


class ClusterTypeConfig(BaseModel):
    """Configuración del ClusterType en NetBox."""

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
        if self.type == "select" and not self.choice_set:
            raise ValueError(
                "Los campos de tipo 'select' deben definir un 'choice_set'."
            )
        return self


class FieldMappingConfig(BaseModel):
    """Definición de mapeo entre columnas CSV y atributo NetBox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str | list[str]
    target: str = Field(min_length=1)
    is_optional: bool = True
    is_unique: bool = False
    cast: Literal["int", "int_gb_to_mb", "bool_si_no"] | None = None
    transform: Literal["concat_dot"] | None = None
    map: str | None = None

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

        if self.transform == "concat_dot" and not isinstance(self.source, list):
            raise ValueError(
                f"El transform 'concat_dot' para target '{self.target}' "
                "requiere que 'source' sea explícitamente una lista."
            )

        return self


class StatusDefaultsConfig(BaseModel):
    """Valores por defecto para status de Device y VM."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device: str = "inventory"
    virtual_machine: str = "staged"


class NetworkColumnsConfig(BaseModel):
    """Nombres de columnas del CSV para interfaces de red."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    names: str = Field(min_length=1)
    status: str = Field(min_length=1)
    ip: str = Field(min_length=1)
    prefix: str = Field(min_length=1)
    mac: str = Field(min_length=1)


class NetworkConfig(BaseModel):
    """Configuración de red y mapeo de estado de interfaces."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    columns: NetworkColumnsConfig
    interface_status_map: dict[str, bool] = Field(
        default_factory=lambda: {"up": True, "down": False}
    )


class NetBoxMappingConfig(BaseModel):
    """
    Contrato completo de configuración y mapeo cargado desde netbox_mapping.yaml.
    Valida tipos, restricciones de valor y consistencia referencial.
    Centraliza el acceso a columnas y métodos utilitarios de ejecución como is_empty().

    NOTA: Las listas y diccionarios de este modelo (ej. custom_field_definitions) son
    poblados automáticamente por Pydantic en la función load_config() al deserializar el
    archivo YAML.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    csv_column_aliases: CsvColumnAliases
    required_columns: list[str] = Field(min_length=1)
    site: SiteConfig
    cluster_type: ClusterTypeConfig
    device_roles: list[DeviceRoleConfig]
    environment_map: dict[str, str] = Field(default_factory=dict)
    machine_type_map: dict[str, Literal["device", "virtual_machine"]]
    status_map: dict[str, str]
    status_defaults: StatusDefaultsConfig = Field(default_factory=StatusDefaultsConfig)
    custom_field_definitions: list[CustomFieldConfig] = Field(default_factory=list)
    device_native_mappings: list[FieldMappingConfig] = Field(default_factory=list)
    device_custom_mappings: list[FieldMappingConfig] = Field(default_factory=list)
    vm_native_mappings: list[FieldMappingConfig] = Field(default_factory=list)
    vm_custom_mappings: list[FieldMappingConfig] = Field(default_factory=list)
    network: NetworkConfig
    empty_values: list[str] = Field(
        default_factory=lambda: ["N/A", "", "None", "n/a", "none"]
    )

    _empty_values_set: frozenset[str] = PrivateAttr(default_factory=frozenset)
    _custom_field_defs_map: dict[str, CustomFieldConfig] = PrivateAttr(
        default_factory=dict
    )
    _available_maps: dict[str, dict[str, FieldValue]] = PrivateAttr(
        default_factory=dict
    )

    def model_post_init(self, __context: Any, /) -> None:
        """Inicializa los valores vacíos y el mapa indexado de Custom Fields O(1)."""
        object.__setattr__(
            self,
            "_empty_values_set",
            frozenset(self.empty_values) | {""},
        )

        cf_map: dict[str, CustomFieldConfig] = {}
        for cf in self.custom_field_definitions:
            cf_map[cf.name] = cf
        object.__setattr__(self, "_custom_field_defs_map", cf_map)

        maps: dict[str, dict[str, FieldValue]] = {
            k: v
            for k, v in self.model_dump().items()
            if isinstance(v, dict) and k.endswith("_map")
        }
        object.__setattr__(self, "_available_maps", maps)

    @property
    def columns(self) -> CsvColumnAliases:
        """Alias ergonómico de acceso al glosario de columnas del CSV."""
        return self.csv_column_aliases

    def get_required_columns(self) -> set[str]:
        """
        Retorna el conjunto de columnas obligatorias configuradas en el YAML.
        Esta lista define las columnas que deben estar *presentes en los encabezados*
        del CSV (fila 1). No implica que cada fila deba tener obligatoriamente un
        *valor no vacío* en dicha columna.
        """
        return set(self.required_columns)

    def get_all_expected_columns(self) -> set[str]:
        """
        Retorna el catálogo completo de nombres de columnas esperadas
        en el CSV, combinando los alias de columnas y las requeridas.
        """
        return set(self.csv_column_aliases.values()) | set(self.required_columns)

    def get_custom_field_def(self, target: str) -> CustomFieldConfig | None:
        """Retorna la definición de un Custom Field en O(1) por nombre de target."""
        return self._custom_field_defs_map.get(target)

    def get_all_custom_field_defs(self) -> list[CustomFieldConfig]:
        """Retorna la lista completa de definiciones de Custom Field."""
        return list(self._custom_field_defs_map.values())

    def get_map(self, map_name: str) -> dict[str, FieldValue]:
        """Devuelve un mapa de configuración por nombre."""
        return self._available_maps.get(map_name, {})

    def is_empty(self, value: Any) -> bool:
        """Determina si un valor es considerado vacío según empty_values."""
        if value is None:
            return True
        return str(value).strip() in self._empty_values_set

    @model_validator(mode="after")
    def validate_config_cross_references(self) -> "NetBoxMappingConfig":
        # 1. Validar que exista el rol "Others" (insensible a mayúsculas) para fallback
        role_names_lower = {r.name.strip().lower() for r in self.device_roles}
        if "others" not in role_names_lower:
            raise ValueError(
                "La lista 'device_roles' debe incluir un rol 'Others' "
                "para fallback de roles no reconocidos."
            )

        # 2. Validar que los 'map' referenciados existan en el modelo
        for field_group_name, field_group in [
            ("device_native_mappings", self.device_native_mappings),
            ("device_custom_mappings", self.device_custom_mappings),
            ("vm_native_mappings", self.vm_native_mappings),
            ("vm_custom_mappings", self.vm_custom_mappings),
        ]:
            for f in field_group:
                if f.map and f.map not in self._available_maps:
                    raise ValueError(
                        f"En '{field_group_name}', target '{f.target}' "
                        f"referencia map '{f.map}', pero no está definido en el YAML."
                    )

        # 3. Validar que las columnas requeridas existan en el catálogo conocido
        known_columns = set(self.csv_column_aliases.values())
        unknown_required = set(self.required_columns) - known_columns
        if unknown_required:
            raise ValueError(
                f"Las siguientes columnas en 'required_columns' no están declaradas "
                f"en 'csv_column_aliases': {unknown_required}"
            )

        # 4. Validar machine_type_map no vacío
        if not self.machine_type_map:
            raise ValueError("'machine_type_map' no puede estar vacío.")

        # 5. Validar que el Custom Field 'machine_type' esté definido en custom_field_definitions
        cf_names = {cf.name for cf in self.custom_field_definitions}
        if "machine_type" not in cf_names:
            raise ValueError(
                "El Custom Field 'machine_type' es obligatorio dentro de custom_field_definitions."
            )

        return self


# ===========================================================
# CACHE DE DATOS
# ===========================================================


class MockNetBoxRecord(BaseModel):
    """Representa un objeto simulado de NetBox para ejecuciones en modo dry-run."""

    model_config = ConfigDict(frozen=True, extra="allow")

    id: int = 0
    name: str = ""
    slug: str = ""
    model: str = ""
    vm_role: bool = False
    custom_fields: dict[str, Any] = Field(default_factory=dict)


NetBoxObject: TypeAlias = Record | MockNetBoxRecord


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


def safe_int(value: Any) -> int | None:
    """Convierte un valor a int, retornando None si no es convertible."""
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def safe_int_gb_to_mb(value: Any) -> int | None:
    """Convierte GB (string/float) a MB (entero). NetBox espera MB para memory."""
    try:
        gb = float(str(value).strip())
        return round(gb * 1024)
    except (ValueError, TypeError):
        return None


def safe_bool_si_no(value: Any) -> bool | None:
    """
    Convierte un valor a booleano según reglas específicas de 'si/no'.
    'si'/'sí' → True, 'no' → False, otro → None.
    """
    v = str(value).strip().lower()
    if v in ("si", "sí", "yes", "true", "1"):
        return True
    if v in ("no", "false", "0"):
        return False
    return None


def apply_cast(value: Any, cast_type: CastType) -> FieldValue:
    """Aplica un cast específico a un valor según la definición del campo."""
    if cast_type == "int":
        return safe_int(value)
    if cast_type == "int_gb_to_mb":
        return safe_int_gb_to_mb(value)
    if cast_type == "bool_si_no":
        return safe_bool_si_no(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def concat_dot(parts: list[str], config: NetBoxMappingConfig) -> str:
    """Concatena partes no vacías con '. ' como separador."""
    clean = [p.strip() for p in parts if not config.is_empty(p)]
    return ". ".join(clean)


def extract_csv_value(row: CsvRow, col_alias: str, config: NetBoxMappingConfig) -> str:
    """
    Extrae y sanitiza un valor del CSV usando su alias definido en el YAML.
    Si la celda está vacía (según config.is_empty) o la columna no existe,
    retorna un string vacío (""), permitiendo validaciones directas tipo `if valor:`.
    """
    col_name = config.columns.get(col_alias)
    if not col_name:
        return ""
    val = row.get(col_name, "").strip()
    return "" if config.is_empty(val) else val


def get_netbox_object_id(obj: NetBoxObject) -> int:
    """Retorna el ID de un objeto NetBox. Si el objeto es None, retorna 0."""
    obj_id = getattr(obj, "id", None)
    if obj_id is None:
        return 0
    return int(obj_id)


def get_node_type_from_object(obj: Endpoint | Record) -> NodeType:
    """Retorna el tipo de nodo (virtual_machine o device) a partir de un Endpoint o Record."""
    url = getattr(obj, "url", "")
    if "virtualization" in url or getattr(obj, "name", "") == "virtual-machines":
        return "virtual_machine"
    return "device"


def get_node_type_from_row(row: CsvRow, config: NetBoxMappingConfig) -> NodeType | None:
    """Extrae el tipo de máquina y lo mapea al tipo de nodo NetBox."""
    machine_type = extract_csv_value(row, "machine_type", config)
    return config.machine_type_map.get(machine_type)


# ============================================================
# CARGA DE CONFIGURACIÓN
# ============================================================


def load_config(mapping_path: Path) -> NetBoxMappingConfig:
    """
    Carga y valida el archivo de mapping YAML utilizando Pydantic.
    Si hay errores de validación de sintaxis o de esquema, los reporta
    con detalle y termina la ejecución de manera controlada.
    """
    if not mapping_path.is_file():
        log.error("No se encontró el archivo de mapping: %s", mapping_path)
        sys.exit(1)

    # Cargar el YAML
    try:
        with mapping_path.open("r", encoding="utf-8") as f:
            raw_yaml = f.read()
        # Expande sintaxis $VAR o ${VAR} usando variables de entorno
        expanded_yaml = os.path.expandvars(raw_yaml)
        raw = yaml.safe_load(expanded_yaml)
    except yaml.YAMLError:
        log.exception("Error sintáctico de YAML al leer %s", mapping_path)
        sys.exit(1)

    if not isinstance(raw, dict):
        log.error(
            "El archivo de mapping %s no contiene un diccionario YAML válido.",
            mapping_path,
        )
        sys.exit(1)

    # Validar el esquema Pydantic para el archivo de mapping
    try:
        return NetBoxMappingConfig.model_validate(raw)
    except ValidationError as exc:
        log.error(
            "Error de validación en el archivo de mapping YAML (%s):",
            mapping_path,
        )
        for err in exc.errors():
            loc = " -> ".join(str(p) for p in err.get("loc", []))
            msg = err.get("msg", "")
            inp = err.get("input")
            log.error("  • [%s]: %s (valor recibido: %r)", loc, msg, inp)
        sys.exit(1)


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
        for var in missing:
            log.error("Variable de entorno %s no definida.", var)
        sys.exit(1)

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
    except Exception:
        log.exception("No se pudo conectar con NetBox (%s)", url)
        sys.exit(1)

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
        )
    except AttributeError:
        log.exception(
            "La instancia de pynetbox no expone uno de los endpoints "
            "requeridos por el script"
        )
        sys.exit(1)


# ============================================================
# LEER Y VALIDAR CSV
# ============================================================


def read_csv(path: Path) -> tuple[list[str], list[CsvRow]]:
    """
    Lee merged_inventory.csv.
    Devuelve (headers, rows) donde cada row es {header: value}.
    """
    if not path.is_file():
        log.error("No se encontró el CSV de entrada: %s", path)
        sys.exit(1)

    rows: list[CsvRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        for row in reader:
            rows.append(dict(row))

    log.info("CSV leído: %d filas, %d columnas", len(rows), len(headers))
    return list(headers), rows


def validate_csv_headers(
    headers: list[str],
    config: NetBoxMappingConfig,
) -> bool:
    """
    Valida que los encabezados del CSV incluyan todas las columnas obligatorias
    configuradas en 'required_columns' dentro de netbox_mapping.yaml.
    Valida la *existencia de la columna en la cabecera*, no que cada fila
    deba tener un valor no vacío. Retorna True si todas las columnas obligatorias
    están presentes en headers.
    """
    header_set = set(headers)

    # 1. Validación de columnas críticas (ERROR bloqueante)
    missing_required = [
        col for col in sorted(config.get_required_columns()) if col not in header_set
    ]
    if missing_required:
        log.error(
            "El CSV no contiene las siguientes columnas obligatorias: %s",
            ", ".join(repr(c) for c in missing_required),
        )
        return False

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

    return True


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
        log.exception(
            "Fallo crítico al inicializar el entorno base: NetBox rechazó la "
            "creación del Site '%s'.",
            name,
        )
        sys.exit(1)

    log.info("Site creado: %s", name)
    return obj


def ensure_cluster_type(
    endpoints: NetBoxEndpoints,
    cluster_type_cfg: ClusterTypeConfig,
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el ClusterType definido en el YAML exista en NetBox."""
    name = cluster_type_cfg.name
    slug = cast(str, cluster_type_cfg.slug)

    results: list[Record] = list(endpoints.cluster_types.filter(name=name))
    if results:
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía ClusterType: %s", name)
        return MockNetBoxRecord(id=0, name=name, slug=slug)

    try:
        obj = cast(Record, endpoints.cluster_types.create(name=name, slug=slug))
    except RequestError:
        log.exception(
            "Fallo crítico al inicializar el entorno base: NetBox rechazó la "
            "creación del ClusterType '%s'.",
            name,
        )
        sys.exit(1)

    log.info("ClusterType creado: %s", name)
    return obj


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

    try:
        obj = cast(
            Record,
            manufacturers_endpoint.create(name=name, slug=slug),
        )
    except RequestError:
        # Fallback: colisión de slug por race condition o datos no previstos.
        slug_fallback = f"{slug}-{hash(name) % 10000:04d}"
        log.warning(
            "Slug '%s' colisionó al crear Manufacturer '%s'; reintentando con '%s'.",
            slug,
            name,
            slug_fallback,
        )
        obj = cast(
            Record,
            manufacturers_endpoint.create(name=name, slug=slug_fallback),
        )
    log.info("Manufacturer creado: %s", name)
    cache[name] = obj
    return obj


def _sync_device_type_u_height(
    existing_dt: Record,
    model: str,
    target_height: int,
    dry_run: bool,
) -> None:
    """Sincroniza la altura en U del modelo de servidor."""
    target_height_val = target_height or 1
    current_height = float(getattr(existing_dt, "u_height", 1) or 1)

    if current_height == float(target_height_val):
        return

    if dry_run:
        log.info(
            "[DRY-RUN] Actualizaría u_height de DeviceType '%s' (de %g a %g)",
            model,
            current_height,
            target_height_val,
        )
        return

    try:
        existing_dt.update({"u_height": target_height_val})
        log.info(
            "DeviceType '%s' u_height actualizado a %g",
            model,
            target_height_val,
        )
    except Exception:
        log.exception("Error actualizando u_height de DeviceType '%s'", model)


def _create_device_type(
    endpoint: Endpoint,
    model: str,
    slug: str,
    manufacturer_id: int,
    u_height: int,
    manufacturer_name: str,
) -> Record:
    """Intenta crear el DeviceType, manejando colisiones de slug."""
    u_height_val = u_height or 1
    try:
        return cast(
            Record,
            endpoint.create(
                model=model,
                slug=slug,
                manufacturer=manufacturer_id,
                u_height=u_height_val,
            ),
        )
    except RequestError:
        slug_fallback = f"{slug}-{hash(model) % 10000:04d}"
        log.warning(
            "Slug '%s' colisionó al crear DeviceType '%s/%s'; reintentando con '%s'.",
            slug,
            manufacturer_name,
            model,
            slug_fallback,
        )
        return cast(
            Record,
            endpoint.create(
                model=model,
                slug=slug_fallback,
                manufacturer=manufacturer_id,
                u_height=u_height_val,
            ),
        )


def ensure_device_type(
    device_types_endpoint: Endpoint,
    manufacturer: NetBoxObject,
    model: str,
    u_height: int,
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

    results: list[Record] = list(
        device_types_endpoint.filter(model=model, manufacturer_id=manufacturer_id)
    )
    if results:
        existing_dt = results[0]
        _sync_device_type_u_height(existing_dt, model, u_height, dry_run)
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

    try:
        obj = cast(Record, platforms_endpoint.create(name=name, slug=slug))
    except RequestError:
        slug_fallback = f"{slug}-{hash(name) % 10000:04d}"
        log.warning(
            "Slug '%s' colisionó al crear Platform '%s'; reintentando con '%s'.",
            slug,
            name,
            slug_fallback,
        )
        obj = cast(
            Record,
            platforms_endpoint.create(name=name, slug=slug_fallback),
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

    results: list[Record] = list(racks_endpoint.filter(name=name, site_id=site_id))
    if results:
        cache[cache_key] = results[0]
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía Rack: %s", name)
        obj: NetBoxObject = MockNetBoxRecord(id=0, name=name)
        cache[cache_key] = obj
        return obj

    obj = cast(Record, racks_endpoint.create(name=name, site=site_id))
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

    results: list[Record] = list(clusters_endpoint.filter(name=name, site_id=site_id))
    if results:
        cache[cache_key] = results[0]
        return results[0]

    cluster_type_id = get_netbox_object_id(cluster_type)

    if dry_run:
        log.info("[DRY-RUN] Crearía Cluster: %s", name)
        obj: NetBoxObject = MockNetBoxRecord(id=0, name=name)
        cache[cache_key] = obj
        return obj

    obj = cast(
        Record,
        clusters_endpoint.create(
            name=name,
            type=cluster_type_id,
            site=site_id,
        ),
    )
    log.info("Cluster creado: %s", name)
    cache[cache_key] = obj
    return obj


def _sync_single_device_role(
    endpoints: NetBoxEndpoints,
    role_def: DeviceRoleConfig,
    device_roles_cache: dict[str, NetBoxObject],
    dry_run: bool,
) -> None:
    """Sincroniza un único DeviceRole y asegura que permita VMs."""
    name = role_def.name
    key = name.lower()

    if key in device_roles_cache:
        return

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
                except Exception:
                    log.exception("Error actualizando DeviceRole '%s'", name)
                    return

        device_roles_cache[key] = role_obj
        return

    if dry_run:
        log.info("[DRY-RUN] Crearía DeviceRole: %s", name)
        device_roles_cache[key] = MockNetBoxRecord(id=0, name=name, vm_role=True)
        return

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
        log.info("DeviceRole creado: %s", name)
        device_roles_cache[key] = obj
    except Exception:
        log.exception("Error creando DeviceRole '%s'", name)


def ensure_all_device_roles(
    endpoints: NetBoxEndpoints,
    device_roles: list[DeviceRoleConfig],
    device_roles_cache: dict[str, NetBoxObject],
    dry_run: bool,
) -> None:
    """
    Garantiza que todos los device roles definidos en el YAML
    existen en NetBox (/api/dcim/device-roles/).
    Todos los roles se habilitan para su uso en Virtual Machines.
    Puebla caches.device_roles con {nombre_lower: objeto}.
    """
    for role_def in device_roles:
        _sync_single_device_role(endpoints, role_def, device_roles_cache, dry_run)


# ============================================================
# CUSTOM FIELDS: ensure_custom_fields
# ============================================================


def _get_object_type_id(
    ots_endpoint: Endpoint,
    object_type: str,
    ot_cache: dict[str, int],
) -> int | None:
    """Obtiene el ID de Object Type en NetBox para un app_label.model dado."""
    if object_type in ot_cache:
        return ot_cache[object_type]

    if "." not in object_type:
        log.warning(
            "Formato de Object Type inválido: %s. Se esperaba 'app_label.model'.",
            object_type,
        )
        return None

    app_label, model = object_type.split(".", 1)

    try:
        results: list[Record] = list(
            ots_endpoint.filter(
                app_label=app_label,
                model=model,
            )
        )
    except Exception:
        log.exception(
            "Error consultando Object Type '%s' en '/api/core/object-types/'",
            object_type,
        )
        return None

    if not results:
        log.warning(
            "Object Type no encontrado en NetBox: %s",
            object_type,
        )
        return None

    ot_id = get_netbox_object_id(results[0])
    ot_cache[object_type] = ot_id
    return ot_id


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
) -> int | None:
    """Crea un choice set si no existe en NetBox."""
    choice_set_name: str = choice_set_cfg.name
    choices: list[list[str]] = _get_choice_set_choices(choice_set_cfg.choices)
    choice_set: Record | None = existing_choice_sets.get(choice_set_name)

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
        except Exception:
            log.exception(
                "Error al crear Choice Set '%s'",
                choice_set_name,
            )
            return None

        existing_choice_sets[choice_set_name] = choice_set
        log.info(
            "Choice Set creado: %s",
            choice_set_name,
        )
        return cast(int, choice_set.id)

    extra_choices: Any = getattr(choice_set, "extra_choices", None)
    current_choices: list[list[str]] = _normalize_choices(extra_choices)

    if current_choices == choices:
        return cast(int, choice_set.id)

    if dry_run:
        log.info("[DRY-RUN] Actualizaría Choice Set: %s", choice_set_name)
        return cast(int, choice_set.id)

    try:
        choice_set.update(
            {
                "extra_choices": choices,
                "order_alphabetically": False,
            }
        )
        log.info("Choice Set actualizado: %s", choice_set_name)
    except Exception:
        log.exception("Error al actualizar Choice Set '%s'", choice_set_name)
        return None

    return cast(int, choice_set.id)


def _ensure_custom_field(
    custom_fields_endpoint: Endpoint,
    existing_cfs: dict[str, Record],
    cf_def: CustomFieldConfig,
    ot_ids: list[int],
    choice_set_id: int | None,
    dry_run: bool,
) -> None:
    """Crea un custom field si no existe en NetBox."""
    name: str = cf_def.name

    if name in existing_cfs:
        log.debug(
            "Custom field ya existe: %s",
            name,
        )
        return

    if dry_run:
        log.info(
            "[DRY-RUN] Crearía custom field: %s (%s)",
            name,
            cf_def.type,
        )
        return

    create_kwargs: NetBoxPayload = {
        "name": name,
        "label": cf_def.label or name,
        "type": cf_def.type,
        "required": cf_def.required,
        "object_types": ot_ids,
    }

    if choice_set_id is not None:
        create_kwargs["choice_set"] = choice_set_id

    default_value: FieldValue = cf_def.default
    if default_value is not None:
        create_kwargs["default"] = default_value

    try:
        created_cf = cast(Record, custom_fields_endpoint.create(**create_kwargs))
        existing_cfs[name] = created_cf
        log.info("Custom field creado: %s", name)
    except Exception:
        log.exception("Error al crear custom field '%s'", name)


def ensure_custom_fields(
    endpoints: NetBoxEndpoints,
    cfg: NetBoxMappingConfig,
    dry_run: bool,
) -> None:
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
    """
    # Obtener Custom Fields y Choice Sets existentes.
    custom_fields = cast(list[Record], endpoints.custom_fields.all())
    choice_sets = cast(list[Record], endpoints.choice_sets.all())

    existing_cfs: dict[str, Record] = {str(cf.name): cf for cf in custom_fields}

    existing_choice_sets: dict[str, Record] = {
        str(ch_set.name): ch_set for ch_set in choice_sets
    }

    # Construir mapa nombre → ID de Object Type.
    ot_cache: dict[str, int] = {}

    # Obtener lista unificada de definiciones de Custom Field O(1).
    cf_definitions: list[CustomFieldConfig] = cfg.get_all_custom_field_defs()

    for cf_def in cf_definitions:
        raw_ot_ids: list[int | None] = [
            _get_object_type_id(
                endpoints.object_types,
                ot,
                ot_cache,
            )
            for ot in cf_def.object_types
        ]
        ot_ids: list[int] = [i for i in raw_ot_ids if i is not None]

        choice_set_id: int | None = None
        choice_set_cfg: ChoiceSetConfig | None = cf_def.choice_set

        if cf_def.type == "select" and choice_set_cfg:
            choice_set_id = _ensure_choice_set(
                endpoints.choice_sets,
                existing_choice_sets,
                choice_set_cfg,
                dry_run,
            )

            if choice_set_id is None:
                continue

        # Crear el Custom Field si no existe.
        _ensure_custom_field(
            endpoints.custom_fields,
            existing_cfs,
            cf_def,
            ot_ids,
            choice_set_id,
            dry_run,
        )


# ============================================================
# CONSTRUCCIÓN DE PAYLOAD
# ============================================================


def _resolve_default_or_empty(default: FieldValue, is_optional: bool) -> FieldValue:
    """Centraliza la política de fallback (default > None > cadena vacía)."""
    if default is not None:
        return default
    return None if is_optional else ""


def _extract_concat_dot_value(
    row: CsvRow,
    source: list[str],
    config: NetBoxMappingConfig,
) -> str:
    """Resuelve un campo multi-columna vía concatenación con punto."""
    parts = [row.get(s, "") for s in source]
    return concat_dot(parts, config)


def _extract_raw_source_value(row: CsvRow, source: str | list[str]) -> str:
    """Extrae el valor crudo de la(s) columna(s) origen, sin transformar."""
    if isinstance(source, str):
        return row.get(source, "")
    return row.get(source[0], "") if source else ""


def _apply_value_map(
    value: str,
    map_key: str,
    config: NetBoxMappingConfig,
    source: str | list[str],
) -> FieldValue:
    """
    Aplica el mapeo declarativo (ej. environment_map) sobre un valor crudo.
    Retorna None si el valor no está definido en el mapa (miss).
    """
    mapped = config.get_map(map_key).get(value.strip())
    if mapped is None:
        log.warning(
            "Valor '%s' de la columna '%s' no está definido en '%s'; "
            "el campo se omitirá para esta fila.",
            value,
            source,
            map_key,
        )
    return mapped


def _validate_select_choice(
    value: FieldValue,
    custom_field_def: CustomFieldConfig | None,
    target: str,
    is_optional: bool,
) -> FieldValue:
    """
    Valida value contra choice_set cuando el Custom Field es de tipo
    'select'. No-op para cualquier otro tipo de campo.
    """
    if not (
        custom_field_def
        and custom_field_def.type == "select"
        and custom_field_def.choice_set
    ):
        return value

    valid_choices = [c.value for c in custom_field_def.choice_set.choices]
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
    raise ValueError(f"Valor inválido '{value}' para el campo requerido '{target}'.")


def _resolve_field_value(
    row: CsvRow,
    field_def: FieldMappingConfig,
    config: NetBoxMappingConfig,
) -> FieldValue:
    """
    Resuelve el valor de un campo según su definición tipada en el YAML.
    Orquesta las etapas (extracción, mapeo, validación de choices, cast)
    delegando cada una a una función de responsabilidad única; corta
    temprano (fail-fast) en cuanto una etapa determina el valor final.
    """
    source = field_def.source
    target = field_def.target
    is_optional = field_def.is_optional
    custom_field_def = config.get_custom_field_def(target)
    default = custom_field_def.default if custom_field_def is not None else None

    # Ruta independiente: multi-columna con concatenación no pasa por
    # map/select/cast, igual que en el comportamiento original.
    if isinstance(source, list) and field_def.transform == "concat_dot":
        value = _extract_concat_dot_value(row, source, config)
        if value:
            return value
        return _resolve_default_or_empty(default, is_optional)

    raw_value = _extract_raw_source_value(row, source)
    if config.is_empty(raw_value):
        return _resolve_default_or_empty(default, is_optional)

    value: FieldValue = raw_value

    if field_def.map:
        mapped = _apply_value_map(raw_value, field_def.map, config, source)
        if mapped is None:
            return _resolve_default_or_empty(default, is_optional)
        value = mapped

    value = _validate_select_choice(value, custom_field_def, target, is_optional)

    if field_def.cast:
        value = apply_cast(value, field_def.cast)

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

    for maps, target_dict in [
        (native_maps, payload),
        (custom_maps, cf_payload),
    ]:
        for fd in maps:
            value = _resolve_field_value(row, fd, config)

            # Sanitización dinámica de constraints UNIQUE dictadas por el YAML.
            if fd.is_unique and value == "":
                value = None

            if value is not None:
                target_dict[fd.target] = value

    # Agregar los custom fields al payload
    if cf_payload:
        payload["custom_fields"] = cf_payload

    return payload


# ============================================================
# RESOLVERS GENERALES (PARA SINCRONIZACIÓN DE OBJETOS)
# ============================================================


def _resolve_netbox_status(row: CsvRow, config: NetBoxMappingConfig) -> str:
    """
    Resuelve el status NetBox a partir de la columna 'Estado'.

    Si el valor no existe en status_map:
        device         -> inventory
        virtual_machine -> staged
    """
    node_type: NodeType | None = get_node_type_from_row(row, config)
    estado = extract_csv_value(row, "status", config)
    status_mapped = config.status_map.get(estado)

    if node_type is None:
        log.error(
            "No se pudo determinar el tipo de nodo. Máquina: '%s', Tipo: '%s'",
            extract_csv_value(row, "machine_name", config) or "?",
            extract_csv_value(row, "machine_type", config) or "?",
        )
        raise ValueError("No se pudo determinar el tipo de nodo.")

    if status_mapped:
        return status_mapped
    if node_type == "device":
        return config.status_defaults.device
    return config.status_defaults.virtual_machine


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


def _resolve_host_device(
    devices_endpoint: Endpoint,
    host_name: str,
    site_id: int | None,
    cache: dict[tuple[int, str], int | None],
) -> int | None:
    """
    Resuelve y cachea el ID del Device correspondiente al hipervisor host,
    acotado estrictamente al site_id configurado.
    """
    if site_id is None:
        return None

    cache_key = (site_id, host_name)
    if cache_key in cache:
        return cache[cache_key]

    try:
        host_devices: list[Record] = list(
            devices_endpoint.filter(name=host_name, site_id=site_id)
        )
        if host_devices:
            dev_id: int = get_netbox_object_id(host_devices[0])
            cache[cache_key] = dev_id
            return dev_id
        else:
            log.warning(
                "No se encontró el Device host '%s' en el Site (ID: %s).",
                host_name,
                site_id,
            )
            cache[cache_key] = None
            return None
    except Exception:
        log.exception(
            "Error consultando Device host '%s' en Site (ID: %s)",
            host_name,
            site_id,
        )
        return None


def _resolve_device_role(
    role_name: str,
    roles_cache: dict[str, NetBoxObject],
    config: NetBoxMappingConfig,
) -> NetBoxObject | None:
    """
    Busca un DeviceRole por nombre (insensible a mayúsculas).
    Si el nombre está vacío o no existe, utiliza "Others" como fallback.
    """
    if config.is_empty(role_name):
        return roles_cache.get("others")

    normalized = role_name.strip().lower()
    if normalized not in roles_cache:
        return roles_cache.get("others")

    return roles_cache[normalized]


# ============================================================
# SINCRONIZACIÓN DE OBJETOS (DEVICES y VMS)
# ============================================================


def _check_missing_core_fields(
    machine_name: str,
    machine_type: str,
    config: NetBoxMappingConfig,
) -> bool:
    """
    Verifica si faltan campos principales requeridos (nombre o tipo).
    Retorna True si falta alguno y registra la advertencia (SKIP).
    """
    if not machine_name or not machine_type:
        empty_alias = "machine_name" if not machine_name else "machine_type"
        empty_field = config.columns.get(empty_alias, empty_alias)
        log.warning(
            "SKIP (%s): campo requerido '%s' vacío.",
            machine_name or "N/A",
            empty_field,
        )
        return True
    return False


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
    existing: list[Record] = []
    found_by_uuid = False
    found_by_name = False

    if not config.is_empty(uuid):
        existing = list(endpoint.filter(cf_inventory_uuid=uuid))
        found_by_uuid = bool(existing)

    if not existing:
        existing_by_name: list[Record] = list(endpoint.filter(name=machine_name))
        found_by_name = bool(existing_by_name)
        if found_by_name:
            existing = existing_by_name

    return existing, found_by_uuid, found_by_name


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


def _check_record_changes(
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
    objeto en NetBox (int), o None si hubo error o no aplica.
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
            return "CREATED", 0

        existing_id = get_netbox_object_id(existing[0])
        diff = _check_record_changes(existing[0], payload)
        if diff:
            log.info(
                "[DRY-RUN] Actualizaría %s: %s (UUID=%s) - Cambios: %s",
                node_type,
                machine_name,
                uuid,
                list(diff.keys()),
            )
            return "UPDATED", existing_id

        log.info(
            "[DRY-RUN] UNCHANGED %s: %s (UUID=%s)",
            node_type,
            machine_name,
            uuid,
        )
        return "UNCHANGED", existing_id

    if not existing:
        try:
            obj = cast(Record, endpoint.create(**payload))
            obj_id = get_netbox_object_id(obj)
            log.info("CREATED %s: %s (ID=%d)", node_type, machine_name, obj_id)
            return "CREATED", obj_id
        except Exception:
            log.exception("ERROR creando %s %s", node_type, machine_name)
            return "ERROR", None

    existing_id = get_netbox_object_id(existing[0])
    try:
        updated = existing[0].update(payload)
        if updated:
            log.info("UPDATED %s: %s", node_type, machine_name)
            return "UPDATED", existing_id

        log.info("UNCHANGED %s: %s", node_type, machine_name)
        return "UNCHANGED", existing_id
    except Exception:
        log.exception(
            "ERROR actualizando %s %s",
            node_type,
            machine_name,
        )
        return "ERROR", None


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
    try:
        existing, found_by_uuid, found_by_name = _find_existing_object(
            uuid,
            machine_name,
            endpoint,
            config,
        )
    except Exception:
        node_type = get_node_type_from_object(endpoint)
        log.exception(
            "ERROR buscando %s '%s' (UUID=%s)",
            node_type,
            machine_name,
            uuid or "N/A",
        )
        return "ERROR", None

    matched_by_name_only = found_by_name and not found_by_uuid
    if matched_by_name_only and not _is_name_safely_unique(
        endpoint, existing, machine_name, csv_name_counts
    ):
        return "SKIPPED", None

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
    cluster_type: NetBoxObject,
    caches: CacheStore,
    csv_name_counts: Counter[str],
    dry_run: bool,
) -> SyncResult:
    """
    Sincroniza una fila de tipo "device" o "hipervisor" con NetBox.
    Retorna: (SyncStatus, obj_id | None)
    """
    machine_name = extract_csv_value(row, "machine_name", config)
    uuid = extract_csv_value(row, "uuid", config)
    machine_type = extract_csv_value(row, "machine_type", config)

    if _check_missing_core_fields(machine_name, machine_type, config):
        return "SKIPPED", None

    # Construcción dinámica del payload.
    device_native_maps = config.device_native_mappings
    device_custom_maps = config.device_custom_mappings
    payload = build_payload(row, device_native_maps, device_custom_maps, config)

    # Compensación de API NetBox: 'face' es obligatorio si 'position' existe.
    if payload.get("position") is not None and "face" not in payload:
        payload["face"] = "front"

    # ── Resolución de objetos relacionados ──────────────────
    # Role.
    role_csv = extract_csv_value(row, "role", config)
    role_obj = _resolve_device_role(role_csv, caches.device_roles, config)
    if role_obj is None:
        log.error(
            "ERROR (%s): no existe el DeviceRole 'Others' en la configuración.",
            machine_name,
        )
        return "ERROR", None
    payload["role"] = get_netbox_object_id(role_obj)

    # Platform.
    platform_id = _resolve_platform(
        endpoints.platforms, row, caches.platforms, dry_run, config
    )
    if platform_id is not None:
        payload["platform"] = platform_id

    # Estado.
    payload["status"] = _resolve_netbox_status(row, config)

    # Site.
    payload["site"] = get_netbox_object_id(site)

    # Cluster para hipervisores.
    if machine_type == "Hipervisor":
        cluster_name_csv = extract_csv_value(row, "cluster_name", config)
        if cluster_name_csv:
            cluster = ensure_cluster(
                endpoints.clusters,
                cluster_name_csv,
                cluster_type,
                site,
                caches.clusters,
                dry_run,
            )
            payload["cluster"] = get_netbox_object_id(cluster)
        else:
            log.info("INFO (%s): Hipervisor sin cluster asignado.", machine_name)

    # Manufacturer.
    manufacturer = extract_csv_value(row, "manufacturer", config)
    model = extract_csv_value(row, "model", config)
    if not manufacturer or not model:
        log.warning("SKIP (%s): sin Marca o Modelo.", machine_name)
        return "SKIPPED", None

    manufacturer_obj = ensure_manufacturer(
        endpoints.manufacturers,
        manufacturer,
        caches.manufacturers,
        dry_run,
    )

    # Altura de unidad. Si no se proporciona, se asume 1.
    raw_u_height = extract_csv_value(row, "alt_u", config)
    u_height = safe_int(raw_u_height) or 1

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

    # Resolver DeviceType.
    device_type = ensure_device_type(
        endpoints.device_types,
        manufacturer_obj,
        model,
        u_height,
        caches.device_types,
        dry_run,
    )
    payload["device_type"] = get_netbox_object_id(device_type)

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
    cluster_type: NetBoxObject,
    caches: CacheStore,
    csv_name_counts: Counter[str],
    dry_run: bool,
) -> SyncResult:
    """
    Sincroniza una fila de tipo "virtual_machine" con NetBox.
    Retorna: (SyncStatus, obj_id | None)
    """
    machine_name = extract_csv_value(row, "machine_name", config)
    uuid = extract_csv_value(row, "uuid", config)
    machine_type = extract_csv_value(row, "machine_type", config)

    if _check_missing_core_fields(machine_name, machine_type, config):
        return "SKIPPED", None

    # Construcción dinámica del payload.
    vm_native_maps = config.vm_native_mappings
    vm_custom_maps = config.vm_custom_mappings
    payload = build_payload(
        row,
        vm_native_maps,
        vm_custom_maps,
        config,
    )

    # ── Resolución de objetos relacionados ──────────────────
    # Role.
    rol_csv = extract_csv_value(row, "role", config)
    role_obj = _resolve_device_role(rol_csv, caches.device_roles, config)
    if role_obj is None:
        log.error(
            "ERROR (%s): no existe el DeviceRole 'Others' en la configuración.",
            machine_name,
        )
        return "ERROR", None

    payload["role"] = get_netbox_object_id(role_obj)

    # Platform.
    platform_id = _resolve_platform(
        endpoints.platforms, row, caches.platforms, dry_run, config
    )
    if platform_id is not None:
        payload["platform"] = platform_id

    # Estado.
    payload["status"] = _resolve_netbox_status(row, config)

    # Site.
    site_id = get_netbox_object_id(site)
    payload["site"] = site_id

    # Cluster.
    cluster_name_csv = extract_csv_value(row, "cluster_name", config)
    if not cluster_name_csv:
        log.warning(
            "SKIP (%s): VM sin %s.",
            machine_name,
            config.columns.get("cluster_name", "Cluster"),
        )
        return "SKIPPED", None
    cluster = ensure_cluster(
        endpoints.clusters,
        cluster_name_csv,
        cluster_type,
        site,
        caches.clusters,
        dry_run,
    )
    payload["cluster"] = get_netbox_object_id(cluster)

    # Device del hipervisor host (acotado a site y cacheado).
    host_name = extract_csv_value(row, "host_device", config)
    if host_name:
        host_dev_id = _resolve_host_device(
            endpoints.devices,
            host_name,
            site_id,
            caches.host_devices,
        )
        if host_dev_id:
            payload["device"] = host_dev_id
        else:
            log.warning(
                "ADVERTENCIA (%s): El dispositivo host '%s' no se encontró en el site. La VM se creará sin asignación de host.",
                machine_name,
                host_name,
            )
    # vcpus.
    cores = extract_csv_value(row, "cores", config)
    cores_int = safe_int(cores)
    if cores_int is not None:
        payload["vcpus"] = float(cores_int)

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


def _validate_interface_ip(ip_raw: str, name: str) -> str | None:
    """Valida que un string sea una dirección IP correcta."""
    try:
        ipaddress.ip_address(ip_raw)
        return ip_raw
    except ValueError:
        log.warning(
            "IP '%s' en interfaz '%s' no es una dirección IP válida; "
            "se omitirá la asignación de IP.",
            ip_raw,
            name,
        )
        return None


def _build_interface_cidr(ip_val: str, pfx_val: str, name: str) -> str | None:
    """Valida IP y prefijo construyendo una dirección CIDR válida."""
    try:
        mask_or_prefix = (
            pfx_val.split("/")[1].strip() if "/" in pfx_val else pfx_val.strip()
        )
        return str(ipaddress.ip_interface(f"{ip_val}/{mask_or_prefix}"))
    except ValueError:
        log.warning(
            "Prefijo o CIDR inválido '%s' para IP '%s' en interfaz '%s'.",
            pfx_val,
            ip_val,
            name,
        )
        return None


def _parse_single_network_interface(
    name: str,
    status_raw: str,
    ip_raw: str,
    pfx_raw: str,
    mac_raw: str,
    status_map: dict[str, bool],
    config: NetBoxMappingConfig,
) -> NetworkInterfaceData | None:
    """Parsea una única interfaz aislando la lógica de validación de IPs."""
    if config.is_empty(name):
        return None

    enabled = status_map.get(status_raw.lower().strip(), True)
    ip_val = ip_raw if not config.is_empty(ip_raw) else None
    pfx_val = pfx_raw if not config.is_empty(pfx_raw) else None
    mac_val = mac_raw if not config.is_empty(mac_raw) else None

    cidr = None
    if ip_val:
        ip_val = _validate_interface_ip(ip_val, name)
        if ip_val and pfx_val:
            cidr = _build_interface_cidr(ip_val, pfx_val, name)

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
) -> list[NetworkInterfaceData] | None:
    """
    Parsea las columnas de red del CSV y devuelve una lista de interfaces.
    Retorna None si los arrays (listas tras el split) tienen longitudes distintas.
    """
    net_cfg = config.network
    cols = net_cfg.columns

    def split_col(col_name: str) -> list[str]:
        raw = row.get(col_name, "")
        return [v.strip() for v in raw.split(",")] if not config.is_empty(raw) else []

    names = split_col(cols.names)
    if not names:
        return []

    statuses = split_col(cols.status)
    ips = split_col(cols.ip)
    prefixes = split_col(cols.prefix)
    macs = split_col(cols.mac)

    max_len = len(names)
    for lst in (statuses, ips, prefixes, macs):
        if lst and len(lst) != max_len:
            return None  # Longitudes incompatibles

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
            status_map=net_cfg.interface_status_map,
            config=config,
        )
        if parsed:
            interfaces.append(parsed)

    return interfaces


# ============================================================
# SINCRONIZACIÓN DE INTERFACES
# ============================================================


def _assign_ip(
    ip_addresses_endpoint: Endpoint,
    cidr: str,
    iface_obj: Record,
    dry_run: bool,
) -> bool:
    """Crea o actualiza una IP address en NetBox y la asigna a la interfaz.
    Retorna True si fue exitoso, False en caso de error."""
    node_type = get_node_type_from_object(iface_obj)
    assigned_type: str = (
        "dcim.interface" if node_type == "device" else "virtualization.vminterface"
    )
    existing_ips: list[Record] = list(ip_addresses_endpoint.filter(address=cidr))

    # 1. Verificar si la IP ya está asignada a esta interfaz
    for ip_obj in existing_ips:
        current_id = getattr(ip_obj, "assigned_object_id", None)
        current_type = getattr(ip_obj, "assigned_object_type", None)
        if current_id == iface_obj.id and str(current_type) == assigned_type:
            return True

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
            return True
        try:
            unassigned_ip.update(
                {
                    "assigned_object_type": assigned_type,
                    "assigned_object_id": iface_obj.id,
                }
            )
            return True
        except Exception:
            log.exception("Error actualizando IP libre %s", cidr)
            return False

    # 3. Todas las IPs existentes están ocupadas por otros nodos. Crear una nueva.
    if dry_run:
        log.info(
            "[DRY-RUN] Crearía nueva IP %s (asignada a objeto %s)", cidr, iface_obj.id
        )
        return True

    try:
        ip_addresses_endpoint.create(
            address=cidr,
            status="active",
            assigned_object_type=assigned_type,
            assigned_object_id=iface_obj.id,
        )
        return True
    except Exception:
        log.exception("Error creando IP %s", cidr)
        return False


def _sync_single_interface(
    iface_data: NetworkInterfaceData,
    obj_id: int,
    iface_endpoint: Endpoint,
    existing_ifaces: dict[str, Record],
    ip_addresses_endpoint: Endpoint,
    dry_run: bool,
) -> bool:
    """
    Sincroniza una interfaz individual y le asigna su IP.
    Retorna True si la sincronización fue exitosa, False en caso de error.
    """
    node_type = get_node_type_from_object(iface_endpoint)
    name: str = iface_data["name"]
    enabled: bool = iface_data["enabled"]
    mac: str | None = iface_data.get("mac")
    cidr: str | None = iface_data.get("cidr")

    payload: NetBoxPayload = {"name": name, "enabled": enabled}
    if mac:
        payload["mac_address"] = mac.upper()

    if node_type == "device":
        payload["device"] = obj_id
        payload["type"] = "other"  # tipo genérico; ajustable
    else:
        payload["virtual_machine"] = obj_id

    if dry_run:
        action = "Actualizaría" if name in existing_ifaces else "Crearía"
        log.info("[DRY-RUN] %s interfaz %s en objeto %s", action, name, obj_id)
        if cidr:
            log.info("[DRY-RUN] Asignaría IP %s a interfaz %s", cidr, name)
        return True

    try:
        if name in existing_ifaces:
            existing_ifaces[name].update(payload)
        else:
            existing_ifaces[name] = cast(Record, iface_endpoint.create(**payload))
    except Exception:
        log.exception("Error procesando interfaz %s", name)
        return False

    if cidr:
        iface_obj = existing_ifaces[name]
        return _assign_ip(ip_addresses_endpoint, cidr, iface_obj, dry_run)

    return True


def sync_interfaces_for_object(
    endpoints: NetBoxEndpoints,
    obj_id: int,
    node_type: NodeType,
    interfaces: list[NetworkInterfaceData],
    dry_run: bool,
) -> int:
    """Sincroniza interfaces y sus IPs para un Device o VM.
    Retorna la cantidad de errores encontrados (0 si todo fue exitoso)."""
    if node_type == "device":
        iface_endpoint = endpoints.device_interfaces
        iface_filter = {"device_id": obj_id}
    else:
        iface_endpoint = endpoints.vm_interfaces
        iface_filter = {"virtual_machine_id": obj_id}

    filtered_ifaces: list[Record] = list(iface_endpoint.filter(**iface_filter))
    existing_ifaces: dict[str, Record] = {
        str(iface.name): iface for iface in filtered_ifaces
    }

    errors = 0
    for iface_data in interfaces:
        success = _sync_single_interface(
            iface_data,
            obj_id,
            iface_endpoint,
            existing_ifaces,
            endpoints.ip_addresses,
            dry_run,
        )
        if not success:
            errors += 1

    return errors


# ============================================================
# MAIN
# ============================================================


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
        f"  Creados                : {counts['CREATED']}\n"
        f"  Actualizados           : {counts['UPDATED']}\n"
        f"  Sin cambios            : {counts['UNCHANGED']}\n"
        f"  Omitidos (SKIP)        : {counts['SKIPPED']}\n"
        f"  Errores                : {counts['ERROR']}\n" + "=" * 50
    )

    if dry_run:
        summary += "\n(Modo DRY-RUN: no se realizaron cambios en NetBox)"

    log.info(summary)

    sys.exit(0 if counts["ERROR"] == 0 else 1)


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

    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    mapping_path = args.mapping or (
        Path(__file__).resolve().parent / "netbox_mapping.yaml"
    )

    # ── Cargar configuración ─────────────────────────────────
    config: NetBoxMappingConfig = load_config(mapping_path)
    columns: CsvColumnAliases = config.columns

    # ── Leer y Validar CSV (Fail-Fast) ───────────────────────
    headers, rows = read_csv(args.csv)

    if not validate_csv_headers(headers, config):
        sys.exit(1)

    # ── Cargar credenciales ──────────────────────────────────
    url, token, verify_ssl = load_env()

    if args.dry_run:
        log.info("Modo DRY-RUN activado. No se modificará NetBox.")

    # ── Conectar ─────────────────────────────────────────────
    nb: Api = build_nb_client(url, token, verify_ssl)

    # ── Validar endpoints requeridos ─────────────────────────
    endpoints: NetBoxEndpoints = build_netbox_endpoints(nb)

    # ── Garantizar custom fields ─────────────────────────────
    ensure_custom_fields(endpoints, config, args.dry_run)

    # ── Garantizar taxonomía global ──────────────────────────
    site: NetBoxObject = ensure_site(endpoints, config.site, args.dry_run)
    cluster_type: NetBoxObject = ensure_cluster_type(
        endpoints, config.cluster_type, args.dry_run
    )

    # ── Inicializar caches ───────────────────────────────────
    caches: CacheStore = CacheStore()

    # ── Garantizar taxonomía local ──────────────────────────
    ensure_all_device_roles(
        endpoints, config.device_roles, caches.device_roles, args.dry_run
    )

    # ── Contadores ───────────────────────────────────────────
    counts: SyncCounts = {
        "CREATED": 0,
        "UPDATED": 0,
        "UNCHANGED": 0,
        "SKIPPED": 0,
        "ERROR": 0,
    }

    # ── Conteo global de nombres de máquina ───────────────
    # Usado para validar unicidad antes de permitir
    # actualizaciones por nombre (sin UUID).
    csv_name_counts: Counter[str] = Counter(
        extract_csv_value(row, "machine_name", config) for row in rows
    )

    # ── Procesar filas ───────────────────────────────────────
    for row_num, row in enumerate(rows, start=2):
        machine_name_raw = extract_csv_value(row, "machine_name", config)
        machine_name = machine_name_raw or f"fila {row_num}"

        node_type: NodeType | None = get_node_type_from_row(row, config)

        if node_type is None:
            log.warning(
                "Fila %d SKIP: %s '%s' no está en machine_type_map.",
                row_num,
                columns["machine_type"],
                extract_csv_value(row, "machine_type", config),
            )
            counts["SKIPPED"] += 1
            continue

        # ── Sincronizar Device o VM ───────────────────────────
        try:
            if node_type == "device":
                result, obj_id = sync_device(
                    endpoints,
                    row,
                    config,
                    site,
                    cluster_type,
                    caches,
                    csv_name_counts,
                    args.dry_run,
                )
            else:
                result, obj_id = sync_vm(
                    endpoints,
                    row,
                    config,
                    site,
                    cluster_type,
                    caches,
                    csv_name_counts,
                    args.dry_run,
                )
        except ValueError as e:
            log.warning("SKIP fila %d: %s", row_num, e)
            counts["SKIPPED"] += 1
            continue
        except Exception:
            log.exception(
                "ERROR inesperado al procesar fila %d ('%s')",
                row_num,
                machine_name,
            )
            counts["ERROR"] += 1
            continue

        counts[result] += 1
        if result not in ("CREATED", "UPDATED", "UNCHANGED"):
            continue

        # ── Validar ID para interfaces (Fail-Fast) ────────────
        if not obj_id:
            if not args.dry_run:
                log.warning(
                    "SKIP interfaces de '%s': el objeto no tiene ID.",
                    machine_name,
                )
            continue

        # ── Parsear interfaces ────────────────────────────────
        interfaces = parse_network_interfaces(row, config)
        if interfaces is None:
            log.warning(
                "Interfaces de '%s' tienen longitudes inconsistentes; "
                "se omitirán para esta fila.",
                machine_name,
            )
            continue

        # ── Sincronizar interfaces del objeto ─────────────────
        try:
            iface_errors = sync_interfaces_for_object(
                endpoints,
                obj_id,
                node_type,
                interfaces,
                args.dry_run,
            )
            if iface_errors > 0:
                counts["ERROR"] += iface_errors
        except Exception:
            log.exception(
                "ERROR inesperado al sincronizar interfaces de '%s'", machine_name
            )
            counts["ERROR"] += 1

    # ── Resumen ──────────────────────────────────────────────
    _print_summary_and_exit(len(rows), counts, args.dry_run)


if __name__ == "__main__":
    main()
