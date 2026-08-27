"""
export_to_netbox.py
===================
Exporta el inventario fusionado (merged_inventory.csv) a NetBox 4.x.

Dependencias:
    pip install pynetbox>=7.3.0 PyYAML>=6.0

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
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias, TypedDict, cast

import pynetbox
import requests
import urllib3
import yaml
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
# ESTADO GLOBAL DE CONFIGURACIÓN
# ============================================================

EMPTY_VALUES: set[str] = set()
INVENTORY_COLUMNS: dict[str, str] = {}

# ============================================================
# ENDPOINTS DE NETBOX
# ============================================================


@dataclass(frozen=True)
class NetBoxEndpoints:
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


# ===========================================================
# CACHE DE DATOS
# ===========================================================

NetBoxObject: TypeAlias = Record | dict[str, Any]


class CacheStore(TypedDict):
    manufacturers: dict[str, NetBoxObject]
    device_types: dict[tuple[str, str], NetBoxObject]
    platforms: dict[str, NetBoxObject]
    racks: dict[str, NetBoxObject]
    clusters: dict[str, NetBoxObject]
    device_roles: dict[str, NetBoxObject]


# ============================================================
# CARGA DE CONFIGURACIÓN
# ============================================================


def load_config(mapping_path: Path) -> dict[str, Any]:
    if not mapping_path.is_file():
        log.error("No se encontró el archivo de mapping: %s", mapping_path)
        sys.exit(1)
    with mapping_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


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


def build_nb_client(url: str, token: str, verify_ssl: bool) -> pynetbox.api:
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    session = requests.Session()
    session.verify = verify_ssl

    nb = pynetbox.api(url, token=token)
    nb.http_session = session

    # Verificar conectividad con una llamada liviana.
    try:
        nb.dcim.sites.filter(limit=1)
    except Exception:
        log.exception("No se pudo conectar con NetBox (%s)", url)
        sys.exit(1)

    log.info("Conectado a NetBox %s", url)
    return nb


def build_netbox_endpoints(nb: pynetbox.api) -> NetBoxEndpoints:
    """
    Resuelve y valida todos los endpoints de NetBox utilizados
    por el script.

    La función falla antes de comenzar cualquier operación de
    sincronización si alguno de los endpoints requeridos no está
    expuesto por la instancia de pynetbox.
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


def is_empty(value: Any) -> bool:
    """Determina si un valor es considerado vacío según EMPTY_VALUES."""
    if value is None:
        return True
    return str(value).strip() in EMPTY_VALUES


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


def apply_cast(value: Any, cast: str) -> Any:
    """Aplica un cast específico a un valor según la definición del campo."""
    if cast == "int":
        return safe_int(value)
    if cast == "int_gb_to_mb":
        return safe_int_gb_to_mb(value)
    if cast == "bool_si_no":
        return safe_bool_si_no(value)
    return value


def concat_dot(parts: list[str]) -> str:
    """Concatena partes no vacías con '. ' como separador."""
    clean = [p.strip() for p in parts if not is_empty(p)]
    return ". ".join(clean)


# ============================================================
# LEER CSV
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


# ============================================================
# TAXONOMÍA: ensure_* (GET o CREATE)
# ============================================================


def get_netbox_object_id(obj: NetBoxObject) -> int:
    if isinstance(obj, dict):
        return int(obj.get("id", 0))
    obj_id = obj.id
    if obj_id is None:
        return 0
    return int(obj_id)


def ensure_site(
    endpoints: NetBoxEndpoints,
    cfg: dict[str, Any],
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el Site definido en el YAML exista en NetBox."""
    name = cfg["site"]["name"]
    slug = cfg["site"].get("slug") or slugify(name)

    results: list[Record] = list(endpoints.sites.filter(name=name))
    if results:
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía Site: %s", name)
        return {"id": 0, "name": name}

    obj = cast(Record, endpoints.sites.create(name=name, slug=slug))
    log.info("Site creado: %s", name)
    return obj


def ensure_cluster_type(
    endpoints: NetBoxEndpoints, cfg: dict[str, Any], dry_run: bool
) -> NetBoxObject:
    """Garantiza que el ClusterType definido en el YAML exista en NetBox."""
    name = cfg["cluster_type"]["name"]
    slug = cfg["cluster_type"].get("slug") or slugify(name)

    results = list(endpoints.cluster_types.filter(name=name))
    if results:
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía ClusterType: %s", name)
        return {"id": 0, "name": name}

    obj = cast(Record, endpoints.cluster_types.create(name=name, slug=slug))
    log.info("ClusterType creado: %s", name)
    return obj


def ensure_manufacturer(
    endpoints: NetBoxEndpoints, name: str, cache: dict[str, NetBoxObject], dry_run: bool
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
        obj = {"id": 0, "name": name}
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
    manufacturer: Any,
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
        endpoints.device_types.filter(model=model, manufacturer_id=manufacturer_id)
    )
    if results:
        cache[key] = results[0]
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía DeviceType: %s / %s", manufacturer, model)
        obj = {"id": 0, "model": model}
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
    log.info("DeviceType creado: %s / %s", getattr(manufacturer, "name", "?"), model)
    cache[key] = obj
    return obj


def ensure_platform(
    endpoints: NetBoxEndpoints, name: str, cache: dict[str, NetBoxObject], dry_run: bool
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
        obj = {"id": 0, "name": name}
        cache[name] = obj
        return obj

    obj = cast(Record, endpoints.platforms.create(name=name, slug=slugify(name)))
    log.info("Platform creado: %s", name)
    cache[name] = obj
    return obj


def ensure_rack(
    endpoints: NetBoxEndpoints,
    name: str,
    site: Any,
    cache: dict[str, NetBoxObject],
    dry_run: bool,
) -> NetBoxObject:
    """Garantiza que el Rack exista en NetBox."""
    if name in cache:
        return cache[name]

    site_id = get_netbox_object_id(site)
    results: list[Record] = list(endpoints.racks.filter(name=name, site_id=site_id))
    if results:
        cache[name] = results[0]
        return results[0]

    if dry_run:
        log.info("[DRY-RUN] Crearía Rack: %s", name)
        obj = {"id": 0, "name": name}
        cache[name] = obj
        return obj

    obj = cast(Record, endpoints.racks.create(name=name, site=site_id))
    log.info("Rack creado: %s", name)
    cache[name] = obj
    return obj


def ensure_cluster(
    endpoints: NetBoxEndpoints,
    name: str,
    cluster_type: Any,
    site: Any,
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
        obj = {"id": 0, "name": name}
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
    object_types_endpoint: Endpoint,
    app_model: str,
    cache: dict[str, int],
) -> int | None:
    """Obtiene el ID de Object Type en NetBox para un app_label.model dado."""
    if app_model in cache:
        return cache[app_model]

    if "." not in app_model:
        log.warning(
            "Formato de Object Type inválido: %s. Se esperaba 'app_label.model'.",
            app_model,
        )
        return None

    app_label, model = app_model.split(".", 1)

    try:
        results: list[Record] = list(
            object_types_endpoint.filter(
                app_label=app_label,
                model=model,
            )
        )
    except Exception:
        log.exception(
            "Error consultando Object Type '%s' en '/api/core/object-types/'", app_model
        )
        return None

    if not results:
        log.warning(
            "Object Type no encontrado en NetBox: %s",
            app_model,
        )
        return None

    ot_id = get_netbox_object_id(results[0])
    cache[app_model] = ot_id
    return ot_id


def _build_custom_field_definitions(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    cf_definitions: list[dict[str, Any]] = list(cfg.get("custom_fields", []))

    for special_key in ("machine_type", "environment"):
        special_def = cfg.get(special_key)
        if not special_def:
            continue

        cf_definitions.append(
            {
                "name": special_def["field_name"],
                "label": special_def.get("label", special_def["field_name"]),
                "type": special_def.get("type", "text"),
                "required": special_def.get("required", False),
                "object_types": special_def.get("object_types", []),
                "choice_set": special_def.get("choice_set"),
            }
        )

    return cf_definitions


def _get_choice_set_choices(choice_set_cfg: dict) -> list[list[str]]:
    return [
        [
            choice["value"],
            choice.get("label", choice["value"]),
        ]
        for choice in choice_set_cfg.get("choices", [])
    ]


def _normalize_choices(choices: Any) -> list[list[str]]:
    if not choices:
        return []

    return [
        [str(choice[0]), str(choice[1])]
        for choice in choices
        if isinstance(choice, (list, tuple)) and len(choice) >= 2
    ]


def _ensure_choice_set(
    choice_sets_endpoint: Any,
    existing_choice_sets: dict,
    choice_set_cfg: dict,
    dry_run: bool,
) -> int | None:
    choice_set_name = choice_set_cfg["name"]
    choices = _get_choice_set_choices(choice_set_cfg)
    choice_set = existing_choice_sets.get(choice_set_name)

    if choice_set is None:
        if dry_run:
            log.info(
                "[DRY-RUN] Crearía Choice Set: %s",
                choice_set_name,
            )
            return 0

        try:
            choice_set = choice_sets_endpoint.create(
                name=choice_set_name,
                extra_choices=choices,
                order_alphabetically=False,
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
        return choice_set.id

    current_choices = _normalize_choices(getattr(choice_set, "extra_choices", None))

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

    return choice_set.id


def _ensure_custom_field(
    custom_fields_endpoint: Any,
    existing_cfs: dict,
    cf_def: dict,
    ot_ids: list[int],
    choice_set_id: int | None,
    dry_run: bool,
) -> None:
    name = cf_def["name"]

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
            cf_def.get("type"),
        )
        return

    create_kwargs = {
        "name": name,
        "label": cf_def.get("label", name),
        "type": cf_def.get("type", "text"),
        "required": cf_def.get("required", False),
        "object_types": ot_ids,
    }

    if choice_set_id is not None:
        create_kwargs["choice_set"] = choice_set_id

    default_value = cf_def.get("default")
    if default_value is not None:
        create_kwargs["default"] = default_value

    try:
        custom_fields_endpoint.create(**create_kwargs)
        existing_cfs[name] = True  # marcar como existente
        log.info("Custom field creado: %s", name)
    except Exception:
        log.exception("Error al crear custom field '%s'", name)


def ensure_custom_fields(
    endpoints: NetBoxEndpoints,
    cfg: dict[str, Any],
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
    existing_cfs = {cf.name: cf for cf in endpoints.custom_fields.all()}

    existing_choice_sets = {
        choice_set.name: choice_set for choice_set in endpoints.choice_sets.all()
    }

    # Construir mapa nombre → ID de Object Type.
    object_type_cache: dict[str, int] = {}

    # Construir lista unificada de definiciones de Custom Field.
    #
    # 'custom_fields' contiene los campos normales.
    # 'machine_type' y 'environment' son definiciones especiales
    # pero deben terminar igualmente como Custom Fields en NetBox.
    cf_definitions = _build_custom_field_definitions(cfg)

    for cf_def in cf_definitions:
        ot_ids = [
            _get_object_type_id(
                endpoints.object_types,
                ot,
                object_type_cache,
            )
            for ot in cf_def.get("object_types", [])
        ]
        ot_ids = [i for i in ot_ids if i is not None]

        choice_set_id = None
        choice_set_cfg = cf_def.get("choice_set")

        if cf_def.get("type") == "selection" and choice_set_cfg:
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
    net_cfg: dict,
) -> list[dict] | None:
    """
    Parsea las 5 columnas de red del CSV (valores separados por comas)
    y devuelve una lista de dicts con la información de cada interfaz.

    Retorna None si los arrays tienen longitudes distintas.
    """
    status_map: dict[str, bool] = net_cfg.get("interface_status_map", {})

    def split_col(col_name: str) -> list[str]:
        raw = row.get(col_name, "")
        if raw.strip() in EMPTY_VALUES:
            return []
        return [v.strip() for v in raw.split(",")]

    cols = net_cfg["columns"]
    names = split_col(cols["names"])
    statuses = split_col(cols["status"])
    ips = split_col(cols["ip"])
    prefixes = split_col(cols["prefix"])
    macs = split_col(cols["mac"])

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

    interfaces = []
    for i, name in enumerate(names):
        if not name or name in EMPTY_VALUES:
            continue

        status_raw = statuses[i].lower().strip()
        enabled = status_map.get(status_raw, True)

        ip_raw = ips[i] if ips[i] not in EMPTY_VALUES else None
        pfx_raw = prefixes[i] if prefixes[i] not in EMPTY_VALUES else None
        mac_raw = macs[i] if macs[i] not in EMPTY_VALUES else None

        # Construir dirección CIDR si tenemos IP y prefijo.
        cidr = None
        if ip_raw and pfx_raw:
            try:
                prefix_len = pfx_raw.split("/")[1]
                cidr = f"{ip_raw}/{prefix_len}"
            except IndexError:
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
    interfaces: list[dict],
    dry_run: bool,
) -> None:
    """Sincroniza interfaces y sus IPs para un Device o VM."""

    if obj_type == "device":
        iface_endpoint = endpoints.device_interfaces
        iface_filter = {"device_id": obj_id}
    else:
        iface_endpoint = endpoints.vm_interfaces
        iface_filter = {"virtual_machine_id": obj_id}

    existing = {iface.name: iface for iface in iface_endpoint.filter(**iface_filter)}

    for iface_data in interfaces:
        name = iface_data["name"]
        enabled = iface_data["enabled"]
        mac = iface_data["mac"]
        cidr = iface_data["cidr"]

        payload: dict[str, Any] = {"name": name, "enabled": enabled}
        if mac:
            payload["mac_address"] = mac.upper()
        if obj_type == "device":
            payload["device"] = obj_id
            payload["type"] = "other"  # tipo genérico; ajustable
        else:
            payload["virtual_machine"] = obj_id

        if dry_run:
            action = "Actualizaría" if name in existing else "Crearía"
            log.info("[DRY-RUN] %s interfaz %s en objeto %s", action, name, obj_id)
        elif name in existing:
            try:
                existing[name].update(payload)
            except Exception:
                log.exception("Error actualizando interfaz %s", name)
                continue
        else:
            try:
                existing[name] = iface_endpoint.create(**payload)
            except Exception:
                log.exception("Error creando interfaz %s", name)
                continue

        # Asignar IP si hay CIDR.
        if cidr and not dry_run:
            iface_obj = existing.get(name)
            if iface_obj:
                _assign_ip(endpoints, cidr, iface_obj, obj_type)

        if cidr and dry_run:
            log.info("[DRY-RUN] Asignaría IP %s a interfaz %s", cidr, name)


def _assign_ip(
    endpoints: NetBoxEndpoints,
    cidr: str,
    iface_obj: Any,
    obj_type: Literal["device", "virtual_machine"],
) -> None:
    """Crea o actualiza una IP address en NetBox y la asigna a la interfaz."""
    if obj_type == "device":
        assigned_type = "dcim.interface"
    else:
        assigned_type = "virtualization.vminterface"

    existing = list(endpoints.ip_addresses.filter(address=cidr))
    if existing:
        ip_obj = existing[0]
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
    config: dict,
) -> dict | None:
    """Busca la definición global de un Custom Field por su nombre."""
    custom_fields = _build_custom_field_definitions(config)

    for cf_def in custom_fields:
        if cf_def.get("name") == target:
            return cf_def

    return None


def resolve_field_value(
    row: dict[str, str],
    field_def: dict[str, Any],
    config: dict[str, Any],
) -> Any:
    """
    Resuelve el valor de un campo según su definición en el YAML.

    Los valores específicos del origen se obtienen desde field_def.
    Los atributos globales del Custom Field, como 'default', se
    obtienen desde la definición global del Custom Field.
    """
    source: str = field_def.get("source", "")
    target: str = field_def.get("target", "")
    skip_if_empty: bool = field_def.get("skip_if_empty", True)
    transform: str | None = field_def.get("transform")
    cast: str | None = field_def.get("cast")
    map_key: str | None = field_def.get("map")

    custom_field_def = _get_custom_field_definition(target, config)
    default = custom_field_def.get("default") if custom_field_def is not None else None

    # Transformación multi-source (concat_dot).
    if isinstance(source, list) and transform == "concat_dot":
        parts = [row.get(s, "") for s in source]
        value = concat_dot(parts)

        if not value:
            if default is not None:
                return default
            if skip_if_empty:
                return None

        return value

    # Campo simple.
    value = row.get(source, "")

    if is_empty(value):
        if default is not None:
            return default
        return None if skip_if_empty else ""

    # Mapeo de valores (ej. status_map, operational_status_map).
    if map_key:
        mapping = config.get(map_key, {})
        value = mapping.get(value.strip(), value)

    # Cast de tipo.
    if cast:
        value = apply_cast(value, cast)

    return value


# ============================================================
# CONSTRUCCIÓN DE PAYLOAD
# ============================================================


def build_payload(
    row: dict[str, str],
    field_defs: list[dict[str, Any]],
    cf_defs: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Construye (payload_nativo, payload_cf) para una fila del CSV.
    Los campos con valor None (vacíos + skip_if_empty) se excluyen.
    """
    payload: dict[str, Any] = {}
    cf_payload: dict[str, Any] = {}

    for fd in field_defs:
        target: str = fd.get("target", "")
        if target.startswith("_"):
            # Campo interno del script (ej. _u_height), no va al API directamente.
            continue
        value = resolve_field_value(row, fd, config)
        if value is not None:
            payload[target] = value

    for fd in cf_defs:
        target: str = fd.get("target", "")
        value = resolve_field_value(row, fd, config)
        if value is not None:
            cf_payload[target] = value

    if cf_payload:
        payload["custom_fields"] = cf_payload

    return payload, cf_payload


def get_internal_field(
    row: dict[str, str],
    field_defs: list[dict[str, Any]],
    internal_key: str,
    config: dict,
) -> Any:
    """Extrae un campo interno (prefijado con '_') de los field_defs."""
    for fd in field_defs:
        if fd.get("target") == internal_key:
            return resolve_field_value(row, fd, config)
    return None


# ============================================================
# RESOLVER STATUS
# ============================================================


def resolve_netbox_status(
    row: dict[str, str],
    config: dict,
    object_type: str,
) -> str:
    """
    Resuelve el status NetBox a partir de la columna 'Estado'.

    Si el valor no existe en status_map:
        device         -> inventory
        virtual_machine -> staged
    """
    estado = row.get(INVENTORY_COLUMNS.get("status", ""), "").strip()
    status_mapped = config.get("status_map", {}).get(estado)
    if status_mapped:
        return status_mapped
    defaults = config.get("status_defaults", {})
    if object_type == "device":
        return defaults.get("device", "inventory")
    return defaults.get("virtual_machine", "staged")


# ============================================================
# RESOLVER PLATFORM
# ============================================================


def resolve_platform(
    endpoints: NetBoxEndpoints,
    row: dict[str, str],
    payload: dict[str, Any],
    caches: CacheStore,
    dry_run: bool,
) -> None:
    """Resuelve el Platform desde la columna OS y lo agrega al payload si existe."""
    platform_name = row.get(INVENTORY_COLUMNS.get("os", ""), "").strip()
    if not is_empty(platform_name):
        platform = ensure_platform(
            endpoints,
            platform_name,
            caches["platforms"],
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
    if not is_empty(uuid):
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
) -> bool:
    """
    Determina si existe un conflicto de identidad.

    Retorna True si la fila debe omitirse.

    Política:
    - UUID presente + UUID no encontrado + nombre encontrado -> SKIP.
    """
    if not is_empty(uuid) and not found_by_uuid and found_by_name:
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
    payload: dict,
    existing: list[Any],
    machine_name: str,
    uuid: str,
    object_label: str,
    dry_run: bool,
) -> str:
    """
    Ejecuta CREATE, UPDATE o DRY-RUN según el objeto encontrado.

    Si existe un objeto y el UUID está vacío, no se actualiza.
    """
    if dry_run:
        if existing:
            if is_empty(uuid):
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
    if is_empty(uuid):
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
    config: dict[str, Any],
    site: Any,
    cluster_type: Any,
    caches: CacheStore,
    dry_run: bool,
) -> str:
    """
    Sincroniza una fila de tipo "device" o "hipervisor" con NetBox.
    Retorna: "CREATED" | "UPDATED" | "UNCHANGED" | "SKIPPED" | "ERROR"
    """
    machine_name: str = row.get(INVENTORY_COLUMNS.get("machine_name", ""), "").strip()
    uuid: str = row.get(INVENTORY_COLUMNS.get("uuid", ""), "").strip()
    machine_type: str = row.get(INVENTORY_COLUMNS.get("machine_type", ""), "").strip()

    if is_empty(machine_name):
        log.warning(f"SKIP ({INVENTORY_COLUMNS.get('machine_name')} vacío).")
        return "SKIPPED"
    if is_empty(machine_type):
        log.warning(
            f"SKIP ({machine_name}): campo '{INVENTORY_COLUMNS.get('machine_type')}' vacío."
        )
        return "SKIPPED"

    device_fields_cfg: list[dict[str, Any]] = config.get("device_fields", [])
    device_cf_cfg: list[dict[str, Any]] = config.get("device_custom_fields", [])
    payload, _ = build_payload(row, device_fields_cfg, device_cf_cfg, config)

    # ── Resolución de objetos relacionados ──────────────────
    # Role.
    rol_csv: str = row.get(INVENTORY_COLUMNS.get("role", ""), "").strip()
    role_obj = resolve_device_role(rol_csv, caches)
    if role_obj is None:
        log.error(
            "ERROR (%s): no existe el DeviceRole 'Others' en la configuración.",
            machine_name,
        )
        return "ERROR"

    payload["role"] = get_netbox_object_id(role_obj)

    # Manufacturer y DeviceType.
    marca: str = row.get(INVENTORY_COLUMNS.get("manufacturer", ""), "").strip()
    modelo: str = row.get(INVENTORY_COLUMNS.get("model", ""), "").strip()
    if is_empty(marca) or is_empty(modelo):
        log.warning("SKIP (%s): sin Marca o Modelo.", machine_name)
        return "SKIPPED"
    manufacturer = ensure_manufacturer(
        endpoints,
        marca,
        caches["manufacturers"],
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
        caches["device_types"],
        dry_run,
    )
    payload["device_type"] = get_netbox_object_id(device_type)

    # Platform.
    resolve_platform(endpoints, row, payload, caches, dry_run)

    # Rack.
    rack_name: str = row.get(INVENTORY_COLUMNS.get("rack", ""), "").strip()
    if not is_empty(rack_name):
        rack = ensure_rack(
            endpoints,
            rack_name,
            site,
            caches["racks"],
            dry_run,
        )
        payload["rack"] = get_netbox_object_id(rack)

    # Campos obligatorios.
    payload["site"] = get_netbox_object_id(site)
    payload["status"] = resolve_netbox_status(row, config, "device")

    # Cluster para hipervisores.
    if machine_type == "Hipervisor":
        cluster = ensure_cluster(
            endpoints,
            machine_name,
            cluster_type,
            site,
            caches["clusters"],
            dry_run,
        )
        payload["cluster"] = get_netbox_object_id(cluster)

    # ── GET o CREATE/UPDATE ──────────────────────────────────
    try:
        existing, found_by_uuid, found_by_name = find_existing_object(
            uuid,
            machine_name,
            endpoints.devices,
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
    ):
        return "SKIPPED"

    return apply_sync(
        endpoints.devices,
        payload,
        existing,
        machine_name,
        uuid,
        "device",
        dry_run,
    )


# ============================================================
# SINCRONIZADOR DE VM
# ============================================================


def sync_vm(
    endpoints: NetBoxEndpoints,
    row: dict[str, str],
    config: dict[str, Any],
    site: Any,
    cluster_type: Any,
    caches: CacheStore,
    dry_run: bool,
) -> str:
    """
    Sincroniza una fila de tipo "virtual_machine" con NetBox.
    Retorna: "CREATED" | "UPDATED" | "UNCHANGED" | "SKIPPED" | "ERROR"
    """
    machine_name: str = row.get(INVENTORY_COLUMNS.get("machine_name", ""), "").strip()
    uuid: str = row.get(INVENTORY_COLUMNS.get("uuid", ""), "").strip()
    machine_type: str = row.get(INVENTORY_COLUMNS.get("machine_type", ""), "").strip()
    if is_empty(machine_name):
        log.warning(f"SKIP ({INVENTORY_COLUMNS.get('machine_name')} vacío).")
        return "SKIPPED"
    if is_empty(machine_type):
        log.warning(
            f"SKIP ({machine_name}): campo '{INVENTORY_COLUMNS.get('machine_type')}' vacío."
        )
        return "SKIPPED"

    vm_fields_cfg: list[dict] = config.get("vm_fields", [])
    vm_cf_cfg: list[dict] = config.get("vm_custom_fields", [])
    payload, _ = build_payload(
        row,
        vm_fields_cfg,
        vm_cf_cfg,
        config,
    )

    # Role.
    rol_csv: str = row.get(INVENTORY_COLUMNS.get("role", ""), "").strip()
    role_obj = resolve_device_role(rol_csv, caches)
    if role_obj is None:
        log.error(
            "ERROR (%s): no existe el DeviceRole 'Others' en la configuración.",
            machine_name,
        )
        return "ERROR"

    payload["role"] = get_netbox_object_id(role_obj)

    # Platform.
    resolve_platform(endpoints, row, payload, caches, dry_run)

    # Cluster.
    host_name: str = row.get(INVENTORY_COLUMNS.get("cluster", ""), "").strip()
    if is_empty(host_name):
        log.warning(
            f"SKIP ({machine_name}): VM sin {INVENTORY_COLUMNS.get('cluster')}.",
        )
        return "SKIPPED"
    cluster = ensure_cluster(
        endpoints,
        host_name,
        cluster_type,
        site,
        caches["clusters"],
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
    )

    # vcpus.
    cores: str = row.get(INVENTORY_COLUMNS.get("cores", ""), "").strip()
    cores_int = safe_int(cores)
    if cores_int is not None:
        payload["vcpus"] = float(cores_int)

    # ── GET o CREATE/UPDATE ──────────────────────────────────
    try:
        existing, found_by_uuid, found_by_name = find_existing_object(
            uuid,
            machine_name,
            endpoints.virtual_machines,
        )
    except Exception:
        log.exception("ERROR buscando VM '%s' (UUID=%s)", machine_name, uuid or "N/A")
        return "ERROR"

    if validate_identity_conflict(
        "virtual_machine",
        machine_name,
        uuid,
        found_by_uuid,
        found_by_name,
    ):
        return "SKIPPED"

    return apply_sync(
        endpoints.virtual_machines,
        payload,
        existing,
        machine_name,
        uuid,
        "VM",
        dry_run,
    )


# ============================================================
# ROLES DE DISPOSITIVO
# ============================================================


def ensure_all_device_roles(
    endpoints: NetBoxEndpoints,
    cfg: dict[str, Any],
    caches: CacheStore,
    dry_run: bool,
) -> None:
    """
    Garantiza que todos los device roles definidos en el YAML
    existen en NetBox (/api/dcim/device-roles/).
    Todos los roles se habilitan para su uso en Virtual Machines.
    Puebla caches['device_roles'] con {nombre_lower: objeto}.
    """
    roles_cfg: list[dict] = cfg.get("device_roles", [])
    for role_def in roles_cfg:
        name = role_def["name"]
        slug = role_def.get("slug") or slugify(name)
        color = role_def.get("color", "9e9e9e")
        key = name.lower()

        if key in caches["device_roles"]:
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

            caches["device_roles"][key] = role_obj
            continue

        if dry_run:
            log.info("[DRY-RUN] Crearía DeviceRole: %s", name)
            caches["device_roles"][key] = {
                "id": 0,
                "name": name,
                "vm_role": True,
            }
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
        caches["device_roles"][key] = obj


def resolve_device_role(
    role_name: str,
    caches: CacheStore,
) -> Any | None:
    """
    Busca un DeviceRole por nombre (insensible a mayúsculas).
    Si el nombre está vacío o no existe, utiliza "Others" como fallback.
    """
    roles = caches["device_roles"]
    if is_empty(role_name):
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
        description="Exporta merged_inventory.csv a NetBox 4.x."
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
    config: dict[str, Any] = load_config(mapping_path)
    EMPTY_VALUES.update(config.get("empty_values", []))

    INVENTORY_COLUMNS: dict[str, str] = config.get("inventory_columns", {})
    # TODO: Hallar una forma mas limpia de acceder a los inventory_columns (con un TypedDict o similar).
    required_columns = (
        "machine_name",
        "machine_type",
        "os",
        "status",
        "role",
        "manufacturer",
        "model",
        "rack",
        "cluster",
        "cores",
        "uuid",
    )
    missing_columns = [
        key for key in required_columns if not INVENTORY_COLUMNS.get(key)
    ]
    if missing_columns:
        log.error(
            "Faltan columnas en 'inventory_columns': %s",
            ", ".join(missing_columns),
        )
        sys.exit(1)

    url, token, verify_ssl = load_env()

    if args.dry_run:
        log.info("Modo DRY-RUN activado. No se modificará NetBox.")

    # ── Conectar ─────────────────────────────────────────────
    nb = build_nb_client(url, token, verify_ssl)

    # ── Validar endpoints requeridos ─────────────────────────
    endpoints = build_netbox_endpoints(nb)

    # ── Garantizar custom fields ─────────────────────────────
    ensure_custom_fields(endpoints, config, args.dry_run)

    # ── Garantizar taxonomía global ──────────────────────────
    site = ensure_site(endpoints, config, args.dry_run)
    cluster_type = ensure_cluster_type(endpoints, config, args.dry_run)

    # ── Inicializar caches ───────────────────────────────────
    caches: CacheStore = {
        "manufacturers": {},
        "device_types": {},
        "platforms": {},
        "racks": {},
        "clusters": {},
        "device_roles": {},
    }

    ensure_all_device_roles(endpoints, config, caches, args.dry_run)

    # ── Leer CSV ─────────────────────────────────────────────
    _headers, rows = read_csv(args.csv)

    machine_type_map: dict[str, str] = config.get("machine_type_map", {})
    net_cfg: dict[str, Any] = config.get("network", {})

    # ── Contadores ───────────────────────────────────────────
    counts = {
        "CREATED": 0,
        "UPDATED": 0,
        "UNCHANGED": 0,
        "SKIPPED": 0,
        "ERROR": 0,
    }

    # ── Procesar filas ───────────────────────────────────────
    for row_num, row in enumerate(rows, start=2):
        tipo_raw = row.get(INVENTORY_COLUMNS.get("machine_type", ""), "").strip()
        nb_type = machine_type_map.get(tipo_raw)

        if nb_type is None:
            log.warning(
                f"Fila %d SKIP: {INVENTORY_COLUMNS.get('machine_type')} '%s' no está en machine_type_map.",
                row_num,
                tipo_raw,
            )
            counts["SKIPPED"] += 1
            continue

        # ── Parsear interfaces ────────────────────────────────
        interfaces = parse_network_interfaces(row, net_cfg)
        if interfaces is None:
            machine_name = row.get(
                INVENTORY_COLUMNS.get("machine_name", ""), f"fila {row_num}"
            )
            log.warning(
                "Interfaces de '%s' tienen longitudes inconsistentes; "
                "se omitirán para esta fila.",
                machine_name,
            )
            interfaces = []

        # ── Sincronizar Device o VM ───────────────────────────
        if nb_type == "device":
            result = sync_device(
                endpoints, row, config, site, cluster_type, caches, args.dry_run
            )
        else:
            result = sync_vm(
                endpoints, row, config, site, cluster_type, caches, args.dry_run
            )

        counts[result] = counts.get(result, 0) + 1

        if result not in ("CREATED", "UPDATED") or not interfaces:
            continue

        if args.dry_run:
            machine_name = row.get(
                INVENTORY_COLUMNS.get("machine_name", ""), f"fila {row_num}"
            )
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
        machine_name = row.get(INVENTORY_COLUMNS.get("machine_name", ""), "").strip()
        uuid = row.get(INVENTORY_COLUMNS.get("uuid", ""), "").strip()

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
