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
    El lookup de Device y VirtualMachine se hace siempre por el
    custom field 'inventory_uuid'. Esto garantiza que un cambio
    de nombre en el CSV actualiza el objeto existente en NetBox
    en lugar de crear un duplicado.

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
from pathlib import Path
from typing import Any, Literal, TypeAlias, TypedDict, cast

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


class CentralizedColumns(BaseModel):
    """
    Nombres de columnas centralizadas de merged_inventory.csv,
    resueltos y validados desde netbox_mapping.yaml.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    machine_name: str = Field(min_length=1)
    machine_type: str = Field(min_length=1)
    os: str = Field(min_length=1)
    status: str = Field(min_length=1)
    role: str = Field(min_length=1)
    manufacturer: str = Field(min_length=1)
    model: str = Field(min_length=1)
    rack: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    cores: str = Field(min_length=1)
    uuid: str = Field(min_length=1)


class SiteConfig(BaseModel):
    """Configuración del Site en NetBox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    slug: str | None = None


class ClusterTypeConfig(BaseModel):
    """Configuración del ClusterType en NetBox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    slug: str | None = None


class DeviceRoleConfig(BaseModel):
    """Configuración de un DeviceRole."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    slug: str | None = None
    color: str = Field(default="9e9e9e", pattern=r"^[0-9a-fA-F]{6}$")


class ChoiceItemConfig(BaseModel):
    """Elemento individual de un Choice Set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = Field(min_length=1)
    label: str = Field(min_length=1)


class ChoiceSetConfig(BaseModel):
    """Definición de un Choice Set para Custom Fields de tipo selection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    choices: list[ChoiceItemConfig] = Field(default_factory=list)


OBJECT_TYPE_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


class BaseCustomFieldDef(BaseModel):
    """Definición base para campos personalizados."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    type: Literal[
        "text", "integer", "boolean", "selection", "date", "url", "json"
    ] = "text"
    required: bool = False
    object_types: list[str] = Field(default_factory=list)
    choice_set: ChoiceSetConfig | None = None

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
    def validate_choice_set_if_selection(self) -> "BaseCustomFieldDef":
        if self.type == "selection" and not self.choice_set:
            raise ValueError(
                "Los campos de tipo 'selection' deben definir un 'choice_set'."
            )
        return self


class SpecialCustomFieldConfig(BaseCustomFieldDef):
    """
    Definición para custom fields especiales definidos en el nivel
    superior del YAML (ej. machine_type, environment).
    """

    field_name: str = Field(min_length=1)


class CustomFieldConfig(BaseCustomFieldDef):
    """Definición estándar de Custom Field en la lista 'custom_fields'."""

    name: str = Field(min_length=1)
    default: Any = None


class FieldMappingConfig(BaseModel):
    """Definición de mapeo entre columna(s) CSV y atributo NetBox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str | list[str]
    target: str = Field(min_length=1)
    skip_if_empty: bool = True
    cast: Literal["int", "int_gb_to_mb", "bool_si_no"] | None = None
    transform: Literal["concat_dot"] | None = None
    map: str | None = None

    @model_validator(mode="after")
    def validate_transform_and_source(self) -> "FieldMappingConfig":
        if self.transform == "concat_dot":
            if not isinstance(self.source, list) or len(self.source) < 1:
                raise ValueError(
                    f"El transform 'concat_dot' para target '{self.target}' "
                    "requiere que 'source' sea una lista no vacía."
                )
            for s in self.source:
                if not isinstance(s, str) or not s.strip():
                    raise ValueError(
                        f"En 'source' para transform 'concat_dot' (target '{self.target}'), "
                        "ningún elemento puede estar vacío."
                    )
        elif isinstance(self.source, str):
            if not self.source.strip():
                raise ValueError(
                    f"El campo 'source' para target '{self.target}' no puede estar vacío."
                )
        elif isinstance(self.source, list):
            if not self.source:
                raise ValueError(
                    f"El campo 'source' para target '{self.target}' no puede ser una lista vacía."
                )
            for s in self.source:
                if not isinstance(s, str) or not s.strip():
                    raise ValueError(
                        f"En 'source' (target '{self.target}'), ningún elemento puede estar vacío."
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
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    centralized_columns: CentralizedColumns
    csv_extra_columns: dict[str, str] = Field(default_factory=dict)
    site: SiteConfig
    cluster_type: ClusterTypeConfig
    device_roles: list[DeviceRoleConfig]
    machine_type: SpecialCustomFieldConfig
    environment: SpecialCustomFieldConfig | None = None
    environment_map: dict[str, str] = Field(default_factory=dict)
    machine_type_map: dict[str, Literal["device", "virtual_machine"]]
    status_map: dict[str, str]
    status_defaults: StatusDefaultsConfig = Field(
        default_factory=StatusDefaultsConfig
    )
    custom_fields: list[CustomFieldConfig] = Field(default_factory=list)
    device_fields: list[FieldMappingConfig] = Field(default_factory=list)
    device_custom_fields: list[FieldMappingConfig] = Field(default_factory=list)
    vm_fields: list[FieldMappingConfig] = Field(default_factory=list)
    vm_custom_fields: list[FieldMappingConfig] = Field(default_factory=list)
    network: NetworkConfig
    empty_values: list[str] = Field(
        default_factory=lambda: ["N/A", "", "None", "n/a", "none"]
    )

    _empty_values_set: frozenset[str] = PrivateAttr(default_factory=frozenset)

    def model_post_init(self, __context: Any) -> None:
        """Inicializa el conjunto inmutable de valores considerados vacíos."""
        object.__setattr__(
            self,
            "_empty_values_set",
            frozenset(self.empty_values) | {""},
        )

    @property
    def columns(self) -> CentralizedColumns:
        """Alias ergonómico de acceso a las columnas centralizadas."""
        return self.centralized_columns

    @property
    def empty_values_set(self) -> frozenset[str]:
        """Conjunto inmutable de valores considerados vacíos."""
        return self._empty_values_set

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
        available_maps = {"environment_map": self.environment_map}
        for field_group_name, field_group in [
            ("device_fields", self.device_fields),
            ("device_custom_fields", self.device_custom_fields),
            ("vm_fields", self.vm_fields),
            ("vm_custom_fields", self.vm_custom_fields),
        ]:
            for f in field_group:
                if f.map and f.map not in available_maps:
                    raise ValueError(
                        f"En '{field_group_name}', target '{f.target}' "
                        f"referencia map '{f.map}', pero no está definido en el YAML."
                    )

        # 3. Validar machine_type_map no vacío
        if not self.machine_type_map:
            raise ValueError("'machine_type_map' no puede estar vacío.")

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
    device_types: dict[tuple[str, str], NetBoxObject] = Field(
        default_factory=dict
    )
    platforms: dict[str, NetBoxObject] = Field(default_factory=dict)
    racks: dict[str, NetBoxObject] = Field(default_factory=dict)
    clusters: dict[str, NetBoxObject] = Field(default_factory=dict)
    device_roles: dict[str, NetBoxObject] = Field(default_factory=dict)


# ============================================================
# PARSER DE INTERFAZ DE RED
# ============================================================

class NetworkInterfaceData(TypedDict):
    """Representa la estructura de datos parseada de una interfaz de red."""

    name: str
    enabled: bool
    mac: str | None
    ip: str | None
    prefix: str | None
    cidr: str | None


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

    try:
        with mapping_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError:
        log.exception("Error sintáctico de YAML al leer %s", mapping_path)
        sys.exit(1)

    if not isinstance(raw, dict):
        log.error(
            "El archivo de mapping %s no contiene un diccionario YAML válido.",
            mapping_path,
        )
        sys.exit(1)

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
    url = os.environ.get("NETBOX_URL", "").rstrip("/")
    token = os.environ.get("NETBOX_TOKEN", "")
    verify_ssl = os.environ.get("NETBOX_VERIFY_SSL", "true").lower() != "false"

    if not url:
        log.error("Variable de entorno NETBOX_URL no definida.")
        sys.exit(1)
    if not token:
        log.error("Variable de entorno NETBOX_TOKEN no definida.")
        sys.exit(1)
    return url, token, verify_ssl


# ============================================================
# CLIENTE PYNETBOX
# ============================================================


def build_nb_client(url: str, token: str, verify_ssl: bool) -> Api:
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
    Resuelve y valida todos los endpoints de NetBox utilizados
    por el script.

    La función falla antes de comenzar cualquier operación de
    sincronización si alguno de los endpoints requeridos no está
    expuesto por la instancia de pynetbox.
    """
    # TODO: Evaluar si los endpoints realmente se validan o no, solo a nivel de existencia
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
        return int(gb * 1024)
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


def apply_cast(value: Any, cast_type: str) -> Any:
    """Aplica un cast específico a un valor según la definición del campo."""
    if cast_type == "int":
        return safe_int(value)
    if cast_type == "int_gb_to_mb":
        return safe_int_gb_to_mb(value)
    if cast_type == "bool_si_no":
        return safe_bool_si_no(value)
    return value


def concat_dot(parts: list[str], config: NetBoxMappingConfig) -> str:
    """Concatena partes no vacías con '. ' como separador."""
    clean = [p.strip() for p in parts if not config.is_empty(p)]
    return ". ".join(clean)


# ============================================================
# LEER Y VALIDAR CSV
# ============================================================


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """
    Lee merged_inventory.csv.
    Devuelve (headers, rows) donde cada row es {header: value}.
    """
    if not path.is_file():
        log.error("No se encontró el CSV de entrada: %s", path)
        sys.exit(1)

    rows: list[dict[str, str]] = []
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
    Valida que los encabezados del CSV incluyan todas las columnas requeridas
    definidas en netbox_mapping.yaml.
    Retorna True si todas las columnas obligatorias están presentes.
    """
    # TODO: Evaluar si sería mejor iterar la config y validar los anclas en base a eso,
    # en vez de hardcodear los nombres de las columnas. Sería más escalable.
    # O usar algún otro mecanismo. De momento hardcodeado.
    header_set = set(headers)
    required_columns = {
        config.centralized_columns.machine_name,
        config.centralized_columns.machine_type,
        config.centralized_columns.uuid,
        config.centralized_columns.status,
    }

    missing_required = [
        col for col in required_columns if col not in header_set
    ]
    if missing_required:
        log.error(
            "El CSV no contiene las siguientes columnas obligatorias: %s",
            ", ".join(repr(c) for c in missing_required),
        )
        return False

    # Chequeo informativo de otras columnas esperadas
    all_expected_columns: set[str] = {
        config.centralized_columns.os,
        config.centralized_columns.role,
        config.centralized_columns.manufacturer,
        config.centralized_columns.model,
        config.centralized_columns.rack,
        config.centralized_columns.cluster,
        config.centralized_columns.cores,
        config.network.columns.names,
        config.network.columns.status,
        config.network.columns.ip,
        config.network.columns.prefix,
        config.network.columns.mac,
    }
    for field_list in (
        config.device_fields,
        config.device_custom_fields,
        config.vm_fields,
        config.vm_custom_fields,
    ):
        for f in field_list:
            if isinstance(f.source, str):
                all_expected_columns.add(f.source)
            elif isinstance(f.source, list):
                all_expected_columns.update(f.source)

    missing_optional = [
        col for col in sorted(all_expected_columns) if col not in header_set
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


def get_netbox_object_id(obj: NetBoxObject) -> int:
    obj_id = getattr(obj, "id", None)
    if obj_id is None:
        return 0
    return int(obj_id)


def ensure_site(
    endpoints: NetBoxEndpoints,
    cfg: NetBoxMappingConfig,
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el Site definido en el YAML exista en NetBox."""
    name = cfg.site.name
    slug = cfg.site.slug or slugify(name)

    results: list[Record] = list(endpoints.sites.filter(name=name))
    if results:
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía Site: %s", name)
        return MockNetBoxRecord(id=0, name=name, slug=slug)

    obj = cast(Record, endpoints.sites.create(name=name, slug=slug))
    log.info("Site creado: %s", name)
    return obj


def ensure_cluster_type(
    endpoints: NetBoxEndpoints,
    cfg: NetBoxMappingConfig,
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el ClusterType definido en el YAML exista en NetBox."""
    name = cfg.cluster_type.name
    slug = cfg.cluster_type.slug or slugify(name)

    results = list(endpoints.cluster_types.filter(name=name))
    if results:
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía ClusterType: %s", name)
        return MockNetBoxRecord(id=0, name=name, slug=slug)

    obj = cast(Record, endpoints.cluster_types.create(name=name, slug=slug))
    log.info("ClusterType creado: %s", name)
    return obj


def ensure_manufacturer(
    endpoints: NetBoxEndpoints,
    name: str,
    cache: dict[str, NetBoxObject],
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el Manufacturer exista en NetBox."""
    if name in cache:
        return cache[name]

    results: list[Record] = list(endpoints.manufacturers.filter(name=name))
    if results:
        cache[name] = results[0]
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía Manufacturer: %s", name)
        obj: NetBoxObject = MockNetBoxRecord(id=0, name=name)
        cache[name] = obj
        return obj

    obj = cast(
        Record,
        endpoints.manufacturers.create(
            name=name,
            slug=slugify(name),
        ),
    )
    log.info("Manufacturer creado: %s", name)
    cache[name] = obj
    return obj


def ensure_device_type(
    endpoints: NetBoxEndpoints,
    manufacturer: NetBoxObject,
    model: str,
    u_height: int,
    cache: dict[tuple[str, str], NetBoxObject],
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el DeviceType exista en NetBox."""
    manufacturer_id = get_netbox_object_id(manufacturer)
    key = (str(manufacturer_id), model)
    if key in cache:
        return cache[key]

    results: list[Record] = list(
        endpoints.device_types.filter(
            model=model, manufacturer_id=manufacturer_id
        )
    )
    if results:
        cache[key] = results[0]
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía DeviceType: %s / %s", manufacturer, model)
        obj: NetBoxObject = MockNetBoxRecord(id=0, model=model)
        cache[key] = obj
        return obj

    obj = cast(
        Record,
        endpoints.device_types.create(
            model=model,
            slug=slugify(model),
            manufacturer=manufacturer_id,
            u_height=u_height or 1,
        ),
    )
    log.info(
        "DeviceType creado: %s / %s", getattr(manufacturer, "name", "?"), model
    )
    cache[key] = obj
    return obj


def ensure_platform(
    endpoints: NetBoxEndpoints,
    name: str,
    cache: dict[str, NetBoxObject],
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el Platform exista en NetBox."""
    if name in cache:
        return cache[name]

    results: list[Record] = list(endpoints.platforms.filter(name=name))
    if results:
        cache[name] = results[0]
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía Platform: %s", name)
        obj: NetBoxObject = MockNetBoxRecord(id=0, name=name)
        cache[name] = obj
        return obj

    obj = cast(Record, endpoints.platforms.create(name=name, slug=slugify(name)))
    log.info("Platform creado: %s", name)
    cache[name] = obj
    return obj


def ensure_rack(
    endpoints: NetBoxEndpoints,
    name: str,
    site: NetBoxObject,
    cache: dict[str, NetBoxObject],
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el Rack exista en NetBox."""
    if name in cache:
        return cache[name]

    site_id = get_netbox_object_id(site)
    results: list[Record] = list(
        endpoints.racks.filter(name=name, site_id=site_id)
    )
    if results:
        cache[name] = results[0]
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía Rack: %s", name)
        obj: NetBoxObject = MockNetBoxRecord(id=0, name=name)
        cache[name] = obj
        return obj

    obj = cast(Record, endpoints.racks.create(name=name, site=site_id))
    log.info("Rack creado: %s", name)
    cache[name] = obj
    return obj


def ensure_cluster(
    endpoints: NetBoxEndpoints,
    name: str,
    cluster_type: NetBoxObject,
    site: NetBoxObject,
    cache: dict[str, NetBoxObject],
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el Cluster exista en NetBox."""
    if name in cache:
        return cache[name]

    results: list[Record] = list(endpoints.clusters.filter(name=name))
    if results:
        cache[name] = results[0]
        return results[0]

    cluster_type_id = get_netbox_object_id(cluster_type)
    site_id = get_netbox_object_id(site)

    if dry_run:
        log.info("[DRY-RUN] Crearía Cluster: %s", name)
        obj: NetBoxObject = MockNetBoxRecord(id=0, name=name)
        cache[name] = obj
        return obj

    obj = cast(
        Record,
        endpoints.clusters.create(
            name=name,
            type=cluster_type_id,
            site=site_id,
        ),
    )
    log.info("Cluster creado: %s", name)
    cache[name] = obj
    return obj


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


def _build_custom_field_definitions(
    cfg: NetBoxMappingConfig,
) -> list[CustomFieldConfig | SpecialCustomFieldConfig]:
    """
    Construye la lista unificada de definiciones de Custom Field
    a partir de 'custom_fields', 'machine_type' y 'environment'.
    """
    cf_definitions: list[CustomFieldConfig | SpecialCustomFieldConfig] = list(
        cfg.custom_fields
    )

    if cfg.machine_type:
        cf_definitions.append(cfg.machine_type)
    if cfg.environment:
        cf_definitions.append(cfg.environment)

    return cf_definitions


def _get_choice_set_choices(choice_set_cfg: ChoiceSetConfig) -> list[list[str]]:
    """Obtiene las opciones de un choice set, ya sea de tipo lista de listas o iterable."""
    return [
        [
            choice.value,
            choice.label,
        ]
        for choice in choice_set_cfg.choices
    ]


def _normalize_choices(choices: Any) -> list[list[str]]:
    """Convierte las opciones de un choice set a una lista de listas de strings."""
    if not choices:
        return []

    return [
        [str(choice[0]), str(choice[1])]
        for choice in choices
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
    choices: list[list[str]] = _get_choice_set_choices(choice_set_cfg)
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

    current_choices: list[list[str]] = _normalize_choices(
        choice_set.extra_choices
    )

    if current_choices != choices:
        if dry_run:
            log.info(
                "[DRY-RUN] Actualizaría Choice Set: %s",
                choice_set_name,
            )
        else:
            try:
                choice_set.update(
                    {
                        "extra_choices": choices,
                        "order_alphabetically": False,
                    }
                )
                log.info(
                    "Choice Set actualizado: %s",
                    choice_set_name,
                )
            except Exception:
                log.exception(
                    "Error al actualizar Choice Set '%s'",
                    choice_set_name,
                )
                return None

    return cast(int, choice_set.id)


def _ensure_custom_field(
    custom_fields_endpoint: Endpoint,
    existing_cfs: dict[str, Record],
    cf_def: CustomFieldConfig | SpecialCustomFieldConfig,
    ot_ids: list[int],
    choice_set_id: int | None,
    dry_run: bool,
) -> None:
    """Crea un custom field si no existe en NetBox."""
    name: str = (
        cf_def.field_name
        if isinstance(cf_def, SpecialCustomFieldConfig)
        else cf_def.name
    )

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

    create_kwargs: dict[str, Any] = {
        "name": name,
        "label": cf_def.label or name,
        "type": cf_def.type,
        "required": cf_def.required,
        "object_types": ot_ids,
    }

    if choice_set_id is not None:
        create_kwargs["choice_set"] = choice_set_id

    default_value: Any = getattr(cf_def, "default", None)
    if default_value is not None:
        create_kwargs["default"] = default_value

    try:
        created_cf: Record = cast(
            Record, custom_fields_endpoint.create(**create_kwargs)
        )
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

    Para los custom fields de tipo 'selection', garantiza también
    la existencia del Choice Set asociado.

    NetBox 4.5+:
    - Los Object Types se consultan mediante /api/core/object-types/.
    - El endpoint /api/extras/object-types/ fue eliminado en NetBox 4.5.
    - Los Choice Sets se gestionan mediante
    /api/extras/custom-field-choice-sets/.
    """
    # Obtener Custom Fields y Choice Sets existentes.
    existing_cfs: dict[str, Record] = {
        str(cf.name): cast(Record, cf)
        for cf in endpoints.custom_fields.all()
    }

    existing_choice_sets: dict[str, Record] = {
        str(choice_set.name): cast(Record, choice_set)
        for choice_set in endpoints.choice_sets.all()
    }

    # Construir mapa nombre → ID de Object Type.
    ot_cache: dict[str, int] = {}

    # Construir lista unificada de definiciones de Custom Field.
    cf_definitions: list[CustomFieldConfig | SpecialCustomFieldConfig] = (
        _build_custom_field_definitions(cfg)
    )

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

        if cf_def.type == "selection" and choice_set_cfg:
            choice_set_id = _ensure_choice_set(
                endpoints.choice_sets,
                existing_choice_sets,
                choice_set_cfg,
                dry_run,
            )

            if choice_set_id is None:
                continue

        _ensure_custom_field(
            endpoints.custom_fields,
            existing_cfs,
            cf_def,
            ot_ids,
            choice_set_id,
            dry_run,
        )


# ============================================================
# PARSEO DE RED
# ============================================================


def parse_network_interfaces(
    row: dict[str, str],
    config: NetBoxMappingConfig,
) -> list[NetworkInterfaceData] | None:
    """
    Parsea las 5 columnas de red del CSV (valores separados por comas)
    y devuelve una lista de dicts con la información de cada interfaz.

    Valida formato de IP y CIDR usando ipaddress.
    Retorna None si los arrays tienen longitudes distintas.
    """
    net_cfg = config.network
    status_map: dict[str, bool] = net_cfg.interface_status_map

    def split_col(col_name: str) -> list[str]:
        raw = row.get(col_name, "")
        if config.is_empty(raw):
            return []
        return [v.strip() for v in raw.split(",")]

    cols = net_cfg.columns
    names = split_col(cols.names)
    statuses = split_col(cols.status)
    ips = split_col(cols.ip)
    prefixes = split_col(cols.prefix)
    macs = split_col(cols.mac)

    if not names:
        return []

    # Rellenar con vacíos si alguna columna tiene menos elementos.
    max_len = len(names)
    for lst in (statuses, ips, prefixes, macs):
        if lst and len(lst) != max_len:
            return None  # longitudes incompatibles

    def pad(lst: list[str]) -> list[str]:
        return lst + [""] * (max_len - len(lst))

    statuses = pad(statuses)
    ips = pad(ips)
    prefixes = pad(prefixes)
    macs = pad(macs)

    interfaces: list[NetworkInterfaceData] = []
    for i, name in enumerate(names):
        if config.is_empty(name):
            continue

        status_raw = statuses[i].lower().strip()
        enabled = status_map.get(status_raw, True)

        ip_raw = ips[i] if not config.is_empty(ips[i]) else None
        pfx_raw = prefixes[i] if not config.is_empty(prefixes[i]) else None
        mac_raw = macs[i] if not config.is_empty(macs[i]) else None

        # Validar IP y construir dirección CIDR si tenemos IP y prefijo.
        cidr = None
        if ip_raw:
            try:
                ipaddress.ip_address(ip_raw)
            except ValueError:
                log.warning(
                    "IP '%s' en interfaz '%s' no es una dirección IP válida; "
                    "se omitirá la asignación de IP.",
                    ip_raw,
                    name,
                )
                ip_raw = None

        if ip_raw and pfx_raw:
            try:
                prefix_len = pfx_raw.split("/")[1].strip()
                candidate_cidr = f"{ip_raw}/{prefix_len}"
                # Validar con ip_interface
                ipaddress.ip_interface(candidate_cidr)
                cidr = candidate_cidr
            except (IndexError, ValueError):
                log.warning(
                    "Prefijo o CIDR inválido '%s' para IP '%s' en interfaz '%s'.",
                    pfx_raw,
                    ip_raw,
                    name,
                )
                cidr = None

        interfaces.append(
            {
                "name": name,
                "enabled": enabled,
                "mac": mac_raw,
                "ip": ip_raw,
                "prefix": pfx_raw,
                "cidr": cidr,
            }
        )

    return interfaces


# ============================================================
# SINCRONIZACIÓN DE INTERFACES
# ============================================================


def sync_interfaces_for_object(
    endpoints: NetBoxEndpoints,
    obj_id: int,
    obj_type: Literal["device", "virtual_machine"],
    interfaces: list[NetworkInterfaceData] | list[dict[str, Any]],
    dry_run: bool,
) -> None:
    """Sincroniza interfaces y sus IPs para un Device o VM."""
    iface_endpoint: Endpoint
    iface_filter: dict[str, int]
    if obj_type == "device":
        iface_endpoint = endpoints.device_interfaces
        iface_filter = {"device_id": obj_id}
    else:
        iface_endpoint = endpoints.vm_interfaces
        iface_filter = {"virtual_machine_id": obj_id}

    existing: dict[str, Record] = {
        str(iface.name): cast(Record, iface)
        for iface in iface_endpoint.filter(**iface_filter)
    }

    for iface_data in interfaces:
        name: str = str(iface_data["name"])
        enabled: bool = bool(iface_data["enabled"])
        mac: str | None = iface_data.get("mac")
        cidr: str | None = iface_data.get("cidr")

        payload: dict[str, Any] = {"name": name, "enabled": enabled}
        if mac:
            payload["mac_address"] = mac.upper()
        if obj_type == "device":
            payload["device"] = obj_id
            payload["type"] = "other"  # tipo genérico; ajustable
        else:
            payload["virtual_machine"] = obj_id

        if dry_run:
            action: str = "Actualizaría" if name in existing else "Crearía"
            log.info(
                "[DRY-RUN] %s interfaz %s en objeto %s", action, name, obj_id
            )
        elif name in existing:
            try:
                existing[name].update(payload)
            except Exception:
                log.exception("Error actualizando interfaz %s", name)
                continue
        else:
            try:
                existing[name] = cast(Record, iface_endpoint.create(**payload))
            except Exception:
                log.exception("Error creando interfaz %s", name)
                continue

        # Asignar IP si hay CIDR.
        if cidr and not dry_run:
            iface_obj: Record | None = existing.get(name)
            if iface_obj:
                _assign_ip(endpoints, cidr, iface_obj, obj_type)

        if cidr and dry_run:
            log.info("[DRY-RUN] Asignaría IP %s a interfaz %s", cidr, name)


def _assign_ip(
    endpoints: NetBoxEndpoints,
    cidr: str,
    iface_obj: Record,
    obj_type: Literal["device", "virtual_machine"],
) -> None:
    """Crea o actualiza una IP address en NetBox y la asigna a la interfaz."""
    assigned_type: str = (
        "dcim.interface" if obj_type == "device" else "virtualization.vminterface"
    )

    existing: list[Record] = list(endpoints.ip_addresses.filter(address=cidr))
    if existing:
        ip_obj: Record = existing[0]
        try:
            ip_obj.update(
                {
                    "assigned_object_type": assigned_type,
                    "assigned_object_id": iface_obj.id,
                }
            )
        except Exception:
            log.exception("Error actualizando IP %s", cidr)
    else:
        try:
            endpoints.ip_addresses.create(
                address=cidr,
                status="active",
                assigned_object_type=assigned_type,
                assigned_object_id=iface_obj.id,
            )
        except Exception:
            log.exception("Error creando IP %s", cidr)


# ============================================================
# NORMALIZACIÓN DE VALORES DE FILA
# ============================================================


def _get_custom_field_definition(
    target: str,
    config: NetBoxMappingConfig,
) -> CustomFieldConfig | SpecialCustomFieldConfig | None:
    """Busca la definición global de un Custom Field por su nombre."""
    custom_fields = _build_custom_field_definitions(config)

    for cf_def in custom_fields:
        name = (
            cf_def.field_name
            if isinstance(cf_def, SpecialCustomFieldConfig)
            else cf_def.name
        )
        if name == target:
            return cf_def

    return None


def resolve_field_value(
    row: dict[str, str],
    field_def: FieldMappingConfig,
    config: NetBoxMappingConfig,
) -> Any:
    """
    Resuelve el valor de un campo según su definición tipada en el YAML.

    Los valores específicos del origen se obtienen desde field_def.
    Los atributos globales del Custom Field, como 'default', se
    obtienen desde la definición global del Custom Field.
    """
    source = field_def.source
    target = field_def.target
    skip_if_empty = field_def.skip_if_empty
    transform = field_def.transform
    cast_type = field_def.cast
    map_key = field_def.map

    custom_field_def = _get_custom_field_definition(target, config)
    default = (
        getattr(custom_field_def, "default", None)
        if custom_field_def is not None
        else None
    )

    # Transformación multi-source (concat_dot).
    if isinstance(source, list) and transform == "concat_dot":
        parts = [row.get(s, "") for s in source]
        value = concat_dot(parts, config)

        if not value:
            if default is not None:
                return default
            if skip_if_empty:
                return None

        return value

    # Campo simple (source es str o list sin transform).
    if isinstance(source, str):
        value = row.get(source, "")
    else:
        value = row.get(source[0], "") if source else ""

    if config.is_empty(value):
        if default is not None:
            return default
        return None if skip_if_empty else ""

    # Mapeo de valores (ej. environment_map).
    if map_key:
        mapping_dict = (
            config.environment_map if map_key == "environment_map" else {}
        )
        mapped = mapping_dict.get(value.strip())
        if mapped is None:
            log.warning(
                "Valor '%s' de la columna '%s' no está definido en '%s'; "
                "el campo se omitirá para esta fila.",
                value,
                source,
                map_key,
            )
            if default is not None:
                return default
            return None if skip_if_empty else ""
        value = mapped

    # Cast de tipo.
    if cast_type:
        value = apply_cast(value, cast_type)

    return value


# ============================================================
# CONSTRUCCIÓN DE PAYLOAD
# ============================================================


def build_payload(
    row: dict[str, str],
    field_defs: list[FieldMappingConfig],
    cf_defs: list[FieldMappingConfig],
    config: NetBoxMappingConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Construye (payload_nativo, payload_cf) para una fila del CSV.
    Los campos con valor None (vacíos + skip_if_empty) se excluyen.
    """
    payload: dict[str, Any] = {}
    cf_payload: dict[str, Any] = {}

    for fd in field_defs:
        target = fd.target
        if target.startswith("_"):
            # Campo interno del script (ej. _u_height), no va al API directamente.
            continue
        value = resolve_field_value(row, fd, config)
        if value is not None:
            payload[target] = value

    for fd in cf_defs:
        target = fd.target
        value = resolve_field_value(row, fd, config)
        if value is not None:
            cf_payload[target] = value

    if cf_payload:
        payload["custom_fields"] = cf_payload

    return payload, cf_payload


def get_internal_field(
    row: dict[str, str],
    field_defs: list[FieldMappingConfig],
    internal_key: str,
    config: NetBoxMappingConfig,
) -> Any:
    """Extrae un campo interno (prefijado con '_') de los field_defs."""
    for fd in field_defs:
        if fd.target == internal_key:
            return resolve_field_value(row, fd, config)
    return None


# ============================================================
# RESOLVER STATUS
# ============================================================


def resolve_netbox_status(
    row: dict[str, str],
    config: NetBoxMappingConfig,
    object_type: str,
    columns: CentralizedColumns,
) -> str:
    """
    Resuelve el status NetBox a partir de la columna 'Estado'.

    Si el valor no existe en status_map:
        device         -> inventory
        virtual_machine -> staged
    """
    estado = row.get(columns.status, "").strip()
    status_mapped = config.status_map.get(estado)
    if status_mapped:
        return status_mapped
    if object_type == "device":
        return config.status_defaults.device
    return config.status_defaults.virtual_machine


# ============================================================
# RESOLVER PLATFORM
# ============================================================


def resolve_platform(
    endpoints: NetBoxEndpoints,
    row: dict[str, str],
    payload: dict[str, Any],
    caches: CacheStore,
    dry_run: bool,
    config: NetBoxMappingConfig,
) -> None:
    """Resuelve el Platform desde la columna OS y lo agrega al payload si existe."""
    platform_name = row.get(config.columns.os, "").strip()
    if not config.is_empty(platform_name):
        platforms_cache = caches.platforms
        platform = ensure_platform(
            endpoints,
            platform_name,
            platforms_cache,
            dry_run,
        )
        payload["platform"] = get_netbox_object_id(platform)


# ============================================================
# BUSCAR OBJETO POR UUID Y NOMBRE
# ============================================================


def find_existing_object(
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


# ============================================================
# VALIDAR CONFLICTO DE IDENTIDAD
# ============================================================


def validate_identity_conflict(
    object_type: Literal["device", "virtual_machine"],
    machine_name: str,
    uuid: str,
    found_by_uuid: bool,
    found_by_name: bool,
    config: NetBoxMappingConfig,
) -> bool:
    """
    Determina si existe un conflicto de identidad.

    Retorna True si la fila debe omitirse.

    Política:
    - UUID presente + UUID no encontrado + nombre encontrado -> SKIP.
    """
    if not config.is_empty(uuid) and not found_by_uuid and found_by_name:
        log.warning(
            "SKIP %s '%s': UUID=%s no encontrado en NetBox, "
            "pero ya existe un %s con el mismo nombre.",
            object_type,
            machine_name,
            uuid,
            "device" if object_type == "device" else "VM",
        )
        return True
    return False


# ============================================================
# SINCRONIZAR OBJETO EXISTENTE/NUEVO
# ============================================================


def apply_sync(
    endpoint: Endpoint,
    payload: dict[str, Any],
    existing: list[Record],
    machine_name: str,
    uuid: str,
    object_label: str,
    config: NetBoxMappingConfig,
    dry_run: bool,
) -> str:
    """
    Ejecuta CREATE, UPDATE o DRY-RUN según el objeto encontrado.

    Si existe un objeto y el UUID está vacío, no se actualiza.
    """
    if dry_run:
        if existing:
            if config.is_empty(uuid):
                log.info(
                    "[DRY-RUN] SKIP actualización de %s: %s "
                    "(encontrado por nombre; UUID vacío)",
                    object_label,
                    machine_name,
                )
                return "SKIPPED"
            log.info(
                "[DRY-RUN] Actualizaría %s: %s (UUID=%s)",
                object_label,
                machine_name,
                uuid,
            )
            return "UPDATED"
        log.info(
            "[DRY-RUN] Crearía %s: %s (UUID=%s)",
            object_label,
            machine_name,
            uuid or "N/A",
        )
        return "CREATED"
    if not existing:
        try:
            endpoint.create(**payload)
            log.info("CREATED %s: %s", object_label, machine_name)
            return "CREATED"
        except Exception:
            log.exception("ERROR creando %s %s", object_label, machine_name)
            return "ERROR"
    if config.is_empty(uuid):
        log.warning(
            "SKIP actualización de %s '%s': UUID vacío.",
            object_label,
            machine_name,
        )
        return "SKIPPED"
    try:
        existing[0].update(payload)
        log.info("UPDATED %s: %s", object_label, machine_name)
        return "UPDATED"
    except Exception:
        log.exception(
            "ERROR actualizando %s %s",
            object_label,
            machine_name,
        )
        return "ERROR"


# ============================================================
# SINCRONIZADOR DE DEVICE
# ============================================================


def sync_device(
    endpoints: NetBoxEndpoints,
    row: dict[str, str],
    config: NetBoxMappingConfig,
    site: NetBoxObject,
    cluster_type: NetBoxObject,
    caches: CacheStore,
    dry_run: bool,
) -> str:
    """
    Sincroniza una fila de tipo "device" o "hipervisor" con NetBox.
    Retorna: "CREATED" | "UPDATED" | "UNCHANGED" | "SKIPPED" | "ERROR"
    """
    columns = config.columns
    machine_name: str = row.get(columns.machine_name, "").strip()
    uuid: str = row.get(columns.uuid, "").strip()
    machine_type: str = row.get(columns.machine_type, "").strip()

    if config.is_empty(machine_name):
        log.warning("SKIP (%s vacío).", columns.machine_name)
        return "SKIPPED"
    if config.is_empty(machine_type):
        log.warning(
            "SKIP (%s): campo '%s' vacío.",
            machine_name,
            columns.machine_type,
        )
        return "SKIPPED"

    device_fields_cfg = config.device_fields
    device_cf_cfg = config.device_custom_fields
    payload, _ = build_payload(
        row, device_fields_cfg, device_cf_cfg, config
    )

    # ── Resolución de objetos relacionados ──────────────────
    # Role.
    rol_csv: str = row.get(columns.role, "").strip()
    role_obj = resolve_device_role(rol_csv, caches, config)
    if role_obj is None:
        log.error(
            "ERROR (%s): no existe el DeviceRole 'Others' en la configuración.",
            machine_name,
        )
        return "ERROR"

    payload["role"] = get_netbox_object_id(role_obj)

    # Manufacturer y DeviceType.
    marca: str = row.get(columns.manufacturer, "").strip()
    modelo: str = row.get(columns.model, "").strip()
    if config.is_empty(marca) or config.is_empty(modelo):
        log.warning("SKIP (%s): sin Marca o Modelo.", machine_name)
        return "SKIPPED"
    manufacturer = ensure_manufacturer(
        endpoints,
        marca,
        caches.manufacturers,
        dry_run,
    )
    u_height = (
        get_internal_field(
            row,
            device_fields_cfg,
            "_u_height",
            config,
        )
        or 1
    )

    # Resolver DeviceType.
    device_type = ensure_device_type(
        endpoints,
        manufacturer,
        modelo,
        u_height,
        caches.device_types,
        dry_run,
    )
    payload["device_type"] = get_netbox_object_id(device_type)

    # Platform.
    resolve_platform(endpoints, row, payload, caches, dry_run, config)

    # Rack.
    rack_name: str = row.get(columns.rack, "").strip()
    if not config.is_empty(rack_name):
        rack = ensure_rack(
            endpoints,
            rack_name,
            site,
            caches.racks,
            dry_run,
        )
        payload["rack"] = get_netbox_object_id(rack)

    # Campos obligatorios.
    payload["site"] = get_netbox_object_id(site)
    payload["status"] = resolve_netbox_status(row, config, "device", columns)

    # Cluster para hipervisores.
    if machine_type == "Hipervisor":
        cluster = ensure_cluster(
            endpoints,
            machine_name,
            cluster_type,
            site,
            caches.clusters,
            dry_run,
        )
        payload["cluster"] = get_netbox_object_id(cluster)

    # ── GET o CREATE/UPDATE ──────────────────────────────────
    try:
        existing, found_by_uuid, found_by_name = find_existing_object(
            uuid,
            machine_name,
            endpoints.devices,
            config,
        )
    except Exception:
        log.exception(
            "ERROR buscando device '%s' (UUID=%s)",
            machine_name,
            uuid or "N/A",
        )
        return "ERROR"

    if validate_identity_conflict(
        "device",
        machine_name,
        uuid,
        found_by_uuid,
        found_by_name,
        config,
    ):
        return "SKIPPED"

    return apply_sync(
        endpoints.devices,
        payload,
        existing,
        machine_name,
        uuid,
        "device",
        config,
        dry_run,
    )


# ============================================================
# SINCRONIZADOR DE VM
# ============================================================


def sync_vm(
    endpoints: NetBoxEndpoints,
    row: dict[str, str],
    config: NetBoxMappingConfig,
    site: NetBoxObject,
    cluster_type: NetBoxObject,
    caches: CacheStore,
    dry_run: bool,
) -> str:
    """
    Sincroniza una fila de tipo "virtual_machine" con NetBox.
    Retorna: "CREATED" | "UPDATED" | "UNCHANGED" | "SKIPPED" | "ERROR"
    """
    columns = config.columns
    machine_name: str = row.get(columns.machine_name, "").strip()
    uuid: str = row.get(columns.uuid, "").strip()
    machine_type: str = row.get(columns.machine_type, "").strip()
    if config.is_empty(machine_name):
        log.warning("SKIP (%s vacío).", columns.machine_name)
        return "SKIPPED"
    if config.is_empty(machine_type):
        log.warning(
            "SKIP (%s): campo '%s' vacío.",
            machine_name,
            columns.machine_type,
        )
        return "SKIPPED"

    vm_fields_cfg = config.vm_fields
    vm_cf_cfg = config.vm_custom_fields
    payload, _ = build_payload(
        row,
        vm_fields_cfg,
        vm_cf_cfg,
        config,
    )

    # Role.
    rol_csv: str = row.get(columns.role, "").strip()
    role_obj = resolve_device_role(rol_csv, caches, config)
    if role_obj is None:
        log.error(
            "ERROR (%s): no existe el DeviceRole 'Others' en la configuración.",
            machine_name,
        )
        return "ERROR"

    payload["role"] = get_netbox_object_id(role_obj)

    # Platform.
    resolve_platform(endpoints, row, payload, caches, dry_run, config)

    # Cluster.
    host_name: str = row.get(columns.cluster, "").strip()
    if config.is_empty(host_name):
        log.warning("SKIP (%s): VM sin %s.", machine_name, columns.cluster)
        return "SKIPPED"
    cluster = ensure_cluster(
        endpoints,
        host_name,
        cluster_type,
        site,
        caches.clusters,
        dry_run,
    )
    payload["cluster"] = get_netbox_object_id(cluster)

    # Campos obligatorios.
    payload["site"] = get_netbox_object_id(site)

    # Device del hipervisor host.
    try:
        host_devices = list(endpoints.devices.filter(name=host_name))
        if host_devices:
            payload["device"] = host_devices[0].id
    except Exception:
        log.exception(
            "No se pudo resolver el Device host '%s' para VM '%s'",
            host_name,
            machine_name,
        )

    # Estado.
    payload["status"] = resolve_netbox_status(
        row,
        config,
        "virtual_machine",
        columns,
    )

    # vcpus.
    cores: str = row.get(columns.cores, "").strip()
    cores_int = safe_int(cores)
    if cores_int is not None:
        payload["vcpus"] = float(cores_int)

    # ── GET o CREATE/UPDATE ──────────────────────────────────
    try:
        existing, found_by_uuid, found_by_name = find_existing_object(
            uuid,
            machine_name,
            endpoints.virtual_machines,
            config,
        )
    except Exception:
        log.exception(
            "ERROR buscando VM '%s' (UUID=%s)", machine_name, uuid or "N/A"
        )
        return "ERROR"

    if validate_identity_conflict(
        "virtual_machine",
        machine_name,
        uuid,
        found_by_uuid,
        found_by_name,
        config,
    ):
        return "SKIPPED"

    return apply_sync(
        endpoints.virtual_machines,
        payload,
        existing,
        machine_name,
        uuid,
        "VM",
        config,
        dry_run,
    )


# ============================================================
# ROLES DE DISPOSITIVO
# ============================================================


def ensure_all_device_roles(
    endpoints: NetBoxEndpoints,
    cfg: NetBoxMappingConfig,
    caches: CacheStore,
    dry_run: bool,
) -> None:
    """
    Garantiza que todos los device roles definidos en el YAML
    existen en NetBox (/api/dcim/device-roles/).
    Todos los roles se habilitan para su uso en Virtual Machines.
    Puebla caches.device_roles con {nombre_lower: objeto}.
    """
    for role_def in cfg.device_roles:
        name = role_def.name
        slug = role_def.slug or slugify(name)
        color = role_def.color
        key = name.lower()

        if key in caches.device_roles:
            continue

        results = list(endpoints.device_roles.filter(name=name))
        if results:
            role_obj = results[0]

            if not getattr(role_obj, "vm_role", False):
                if dry_run:
                    log.info(
                        "[DRY-RUN] Actualizaría DeviceRole para permitir VM: %s",
                        name,
                    )
                else:
                    try:
                        role_obj.update({"vm_role": True})
                        log.info(
                            "DeviceRole actualizado para permitir VM: %s",
                            name,
                        )
                    except Exception:
                        log.exception(
                            "Error actualizando DeviceRole '%s'",
                            name,
                        )
                        continue

            caches.device_roles[key] = role_obj
            continue

        if dry_run:
            log.info("[DRY-RUN] Crearía DeviceRole: %s", name)
            caches.device_roles[key] = MockNetBoxRecord(
                id=0,
                name=name,
                vm_role=True,
            )
            continue

        try:
            obj = cast(
                Record,
                endpoints.device_roles.create(
                    name=name,
                    slug=slug,
                    color=color,
                    vm_role=True,
                ),
            )
        except Exception:
            log.exception(
                "Error creando DeviceRole '%s'",
                name,
            )
            continue

        log.info("DeviceRole creado: %s", name)
        caches.device_roles[key] = obj


def resolve_device_role(
    role_name: str,
    caches: CacheStore,
    config: NetBoxMappingConfig,
) -> NetBoxObject | None:
    """
    Busca un DeviceRole por nombre (insensible a mayúsculas).
    Si el nombre está vacío o no existe, utiliza "Others" como fallback.
    """
    roles = caches.device_roles
    if config.is_empty(role_name):
        return roles.get("others")

    normalized = role_name.strip().lower()
    if normalized not in roles:
        return roles.get("others")

    return roles[normalized]


# ============================================================
# MAIN
# ============================================================


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
    columns: CentralizedColumns = config.columns

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
    site: NetBoxObject = ensure_site(endpoints, config, args.dry_run)
    cluster_type: NetBoxObject = ensure_cluster_type(endpoints, config, args.dry_run)

    # ── Inicializar caches ───────────────────────────────────
    caches: CacheStore = CacheStore()

    # ── Garantizar taxonomía local ──────────────────────────
    ensure_all_device_roles(endpoints, config, caches, args.dry_run)

    # ── Leer CSV ─────────────────────────────────────────────
    headers, rows = read_csv(args.csv)

    # ── Validar encabezados del CSV ──────────────────────────
    if not validate_csv_headers(headers, config):
        sys.exit(1)

    # ── Contadores ───────────────────────────────────────────
    counts = {
        "CREATED": 0,
        "UPDATED": 0,
        "UNCHANGED": 0,
        "SKIPPED": 0,
        "ERROR": 0,
    }
    # TODO: Implementar UNCHANGED, ya que actualmente no lo devuelve
    # ── Procesar filas ───────────────────────────────────────
    for row_num, row in enumerate(rows, start=2):
        tipo_raw = row.get(columns.machine_type, "").strip()
        nb_type = config.machine_type_map.get(tipo_raw)

        if nb_type is None:
            log.warning(
                "Fila %d SKIP: %s '%s' no está en machine_type_map.",
                row_num,
                columns.machine_type,
                tipo_raw,
            )
            counts["SKIPPED"] += 1
            continue

        # ── Parsear interfaces ────────────────────────────────
        interfaces = parse_network_interfaces(row, config)
        if interfaces is None:
            machine_name = row.get(columns.machine_name, f"fila {row_num}")
            log.warning(
                "Interfaces de '%s' tienen longitudes inconsistentes; "
                "se omitirán para esta fila.",
                machine_name,
            )
            interfaces = []

        # ── Sincronizar Device o VM ───────────────────────────
        if nb_type == "device":
            result = sync_device(
                endpoints,
                row,
                config,
                site,
                cluster_type,
                caches,
                args.dry_run,
            )
        else:
            result = sync_vm(
                endpoints,
                row,
                config,
                site,
                cluster_type,
                caches,
                args.dry_run,
            )

        counts[result] = counts.get(result, 0) + 1

        if result not in ("CREATED", "UPDATED") or not interfaces:
            continue

        if args.dry_run:
            machine_name = row.get(columns.machine_name, f"fila {row_num}")
            for iface in interfaces:
                log.info(
                    "[DRY-RUN] Sincronizaría interfaz %s en '%s'",
                    iface["name"],
                    machine_name,
                )
            continue

        # ── Resolver nuevamente el objeto sincronizado ────────
        #
        # El UUID puede ser vacío, por lo que no se puede depender
        # exclusivamente de cf_inventory_uuid. Se utiliza la misma
        # política de identificación: UUID cuando existe, nombre
        # como fallback.
        machine_name = row.get(columns.machine_name, "").strip()
        uuid = row.get(columns.uuid, "").strip()

        try:
            if nb_type == "device":
                endpoint = endpoints.devices
                object_type = "device"
            else:
                endpoint = endpoints.virtual_machines
                object_type = "virtual_machine"

            existing, found_by_uuid, found_by_name = find_existing_object(
                uuid,
                machine_name,
                endpoint,
                config,
            )
        except Exception:
            log.exception(
                "ERROR buscando objeto NetBox para sincronizar interfaces "
                "de '%s' (UUID=%s)",
                machine_name,
                uuid or "N/A",
            )
            counts["ERROR"] += 1
            continue

        # ----------------------------------------------------
        # Si el UUID está informado y no coincide con un objeto
        # existente, pero el nombre sí existe, se considera un
        # conflicto de identidad y las interfaces no se fusionan.
        # ----------------------------------------------------
        if validate_identity_conflict(
            object_type,
            machine_name,
            uuid,
            found_by_uuid,
            found_by_name,
            config,
        ):
            log.warning(
                "SKIP interfaces de '%s': conflicto de identidad.",
                machine_name,
            )
            continue

        if not existing:
            log.warning(
                "SKIP interfaces de '%s': no se encontró el objeto "
                "sincronizado en NetBox.",
                machine_name,
            )
            continue

        obj_id = get_netbox_object_id(existing[0])
        if obj_id == 0:
            log.warning(
                "SKIP interfaces de '%s': el objeto NetBox no tiene ID.",
                machine_name,
            )
            continue

        sync_interfaces_for_object(
            endpoints,
            obj_id,
            object_type,
            interfaces,
            args.dry_run,
        )

    # ── Resumen ──────────────────────────────────────────────
    print()
    print("=" * 50)
    print("Resumen de exportación a NetBox")
    print("=" * 50)
    print(f"  Total filas procesadas : {len(rows)}")
    print(f"  Creados                : {counts['CREATED']}")
    print(f"  Actualizados           : {counts['UPDATED']}")
    print(f"  Sin cambios            : {counts['UNCHANGED']}")
    print(f"  Omitidos (SKIP)        : {counts['SKIPPED']}")
    print(f"  Errores                : {counts['ERROR']}")
    print("=" * 50)

    if args.dry_run:
        print("(Modo DRY-RUN: no se realizaron cambios en NetBox)")

    sys.exit(0 if counts["ERROR"] == 0 else 1)


if __name__ == "__main__":
    main()
