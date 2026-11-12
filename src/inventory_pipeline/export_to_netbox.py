"""
export_to_netbox.py
===================
Exports the merged inventory (merged_inventory.csv) to NetBox 4.6+.

Environment variables (also read from the .env file at the project
root; those already defined in the environment take precedence):
    NETBOX_URL        → https://netbox.mycompany.com
    NETBOX_TOKEN      → token with write permissions over
                        dcim, virtualization, ipam, extras, core
    NETBOX_VERIFY_SSL → "true" / "false"  (default: true)
    NETBOX_SITE_NAME  → name of the single Site (referenced by netbox_mapping.yaml)

Usage:
    python -m inventory_pipeline.export_to_netbox merged_inventory.csv \
        [netbox_mapping.yaml] [options]

If the mapping is not given, config/netbox_mapping.yaml is used (see
inventory_pipeline.paths).

Options:
    --dry-run               Shows the operations that would be executed without
                            modifying NetBox. Useful to validate before the
                            first real sync.
    --verbose               Enables DEBUG level logs.
    --prune-network-orphans Reconciles the network of each node with the CSV: removes
                            the interfaces and MACs that no longer exist in it and
                            unassigns their IPs (see "Network and interfaces").

Identity and idempotency (per row, within the configured Site):
    1. By the 'inventory_uuid' custom field (exact match). It is used only if
       the UUID is unique in the CSV and in NetBox; if it belongs to a node of another
       Site, the row is skipped (SKIP) instead of moving or duplicating it.
    2. If the UUID does not resolve, by name within the Site, which must be unique
       in the CSV and in NetBox and must not belong to a node with another UUID.
    This way a name change in the CSV updates the existing object instead of
    creating a duplicate, and rows with ambiguous identifiers never
    clobber each other. Only the fields that changed are sent.

CSV input format:
    - Simple fields: processed as plain strings (with .strip()).
    - Empty values: those that match exactly the values defined in
      `empty_values` of the YAML (e.g. "N/A", "None") are ignored.
    - Records with fewer fields than the header are padded with empty values
      (WARNING); those with extra fields are discarded (ERROR). The messages state the
      physical line of the file where each record starts.
    - Height (U) and P/N belong to the DeviceType, shared by all the devices
      of a model: they are consolidated once per model (first non-empty value) and an
      empty value never erases the one NetBox already has.
    - Network interfaces: The 5 network columns (Interfaces, status, IP,
      IP network, MAC) accept multiple comma-separated values (`,`).
      All of them must have the same number of elements per cell to
      be synchronized correctly.

Network and interfaces:
    - Per interface, Prefix, IP and MAC are synchronized. If an interface changes
      its name while keeping its IP or MAC, the existing one is renamed (no other is created).
    - An IP already assigned to another interface is skipped with a WARNING: NetBox does
      not allow duplicating it.
    - With --prune-network-orphans, IPs with a role assigned in NetBox (secondary,
      vip, etc.) are never unassigned, and an orphan interface that hosts them is
      kept.

Exit codes:
    0 → no errors
    1 → at least one ERROR (row, interface or CSV record discarded)
"""

import argparse
import csv
import hashlib
import ipaddress
import logging
import math
import os
import random
import re
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Final,
    Literal,
    NamedTuple,
    NoReturn,
    TypedDict,
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

from inventory_pipeline.paths import DEFAULT_MAPPING_PATH, DOTENV_PATH

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
# CUSTOM ERRORS
# ============================================================


class RowValidationError(ValueError):
    """Exception raised when the data of a row is explicitly invalid."""


class ConfigValidationError(ValueError):
    """Exception raised when there are critical configuration errors in the script or YAML."""


class RowSkipCondition(Exception):
    """Exception raised to abort early the processing
    of a row silently (SKIP)."""


class ResourceAssignedElsewhere(Exception):
    """Exception raised when a resource that must be unique (e.g. an IP) is already
    assigned to another interface; `record` is the NetBox record that holds it."""

    def __init__(self, record: "NetBoxObject") -> None:
        super().__init__("The resource is already assigned to another interface.")
        self.record = record


class NetBoxApiError(Exception):
    """Exception raised when a persistent API error occurs
    while interacting with Pynetbox (RequestError)."""


class FieldParseError(ValueError):
    """Exception raised when an optional field contains malformed data,
    allowing the upper layer to decide whether to ignore it or abort the row."""


@contextmanager
def netbox_error_wrap(msg: str) -> Generator[None, None, None]:
    """Wraps NetBox API errors into readable exceptions of our domain."""
    try:
        yield
    except RequestError as e:
        raise NetBoxApiError(f"{msg}: {e}") from e


# ============================================================
# TYPE ALIASES AND TYPE STRUCTURES
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
    RACK_POSITION = "rack_position"
    BOOL_YES_NO = "bool_yes_no"
    LOWER = "lower"


# Minimum position (U) that NetBox accepts in Device.position (MinValueValidator(1)).
RACK_POSITION_MIN: Final = 1.0
# Default height (U) with which a DeviceType is created if the CSV does not report it.
DEFAULT_DEVICE_TYPE_U_HEIGHT: Final = 1.0
# Maximum length of DeviceType.part_number in NetBox (CharField max_length=50).
PART_NUMBER_MAX_LENGTH: Final = 50

# `type` aliases (PEP 695) are evaluated lazily: they can reference classes
# defined further down in the module (e.g. MockNetBoxRecord) without quotes.
type NetBoxObject = Record | MockNetBoxRecord
type CsvRow = dict[str, str]
type FieldValue = str | int | float | bool | list[str] | None
type CustomFieldsPayload = dict[str, FieldValue]
type SyncCounts = dict[SyncStatus, int]
type NetBoxPayload = dict[str, Any]


class SiteScopedName(NamedTuple):
    """
    Key of objects whose name is only unique within a Site (Location, Rack, Device).

    Being a named tuple, it is compatible with `(site_id, name)` tuples
    as a dictionary key.
    """

    site_id: int
    name: str


class DeviceTypeKey(NamedTuple):
    """Identity of a hardware model, with manufacturer and model as they come from the CSV."""

    manufacturer: str
    model: str

    @property
    def label(self) -> str:
        """Readable label for logs (e.g. 'Dell / R640')."""
        return f"{self.manufacturer} / {self.model}"


class NumberedRow(NamedTuple):
    """CSV row along with the physical line of the file where it starts."""

    line_number: int
    row: CsvRow


# Type aliases for the NetBox caches
type NameCache = dict[str, NetBoxObject]
type SiteNameCache = dict[SiteScopedName, NetBoxObject]
# Key (manufacturer name *in NetBox*, model): it may differ from the CSV text
# when an existing manufacturer is reused by slug, which is why it is not DeviceTypeKey.
type ManufModelCache = dict[tuple[str, str], NetBoxObject]
type HostDeviceCache = dict[SiteScopedName, int | None]


class DeviceTypeAttributes(NamedTuple):
    """
    Model-level attributes of a DeviceType reported by the CSV.
    `None` means "not reported": it is never used to modify an existing DeviceType.
    """

    u_height: float | None
    part_number: str | None


type DeviceTypeAttrsMap = dict[DeviceTypeKey, DeviceTypeAttributes]


class CsvIdentityCounts(NamedTuple):
    """
    Global count of identities in the CSV, computed only once per run.

    It is used to validate uniqueness (by UUID and by name) before allowing
    a row to update an existing object in NetBox.
    """

    names: Counter[str]
    uuids: Counter[str]


class CsvData(NamedTuple):
    """
    Rows of the input CSV along with the physical line of the file where each
    one starts (parallel: `line_numbers[i]` corresponds to `rows[i]`).
    `rejected_rows` counts the records discarded due to an invalid structure.
    """

    rows: list[CsvRow]
    line_numbers: list[int]
    rejected_rows: int = 0


class SyncResult(NamedTuple):
    """
    Result of the synchronization of a Device or VM.
    Contains: (synchronization status, ID of the object in NetBox, instantiated NetBox object or None if there was an error/skip).
    """

    status: SyncStatus
    obj_id: int
    main_obj: NetBoxObject | None


class ClassifiedRows(NamedTuple):
    """
    Data structure that stores the CSV rows classified
    by their role in the hierarchy (physical vs virtual).
    """

    device_rows: list[NumberedRow]
    vm_rows: list[NumberedRow]


class Ipv4Candidate(NamedTuple):
    """Tuple that represents a primary IPv4 candidate: (ip_id, iface_obj, mac_obj)."""

    ip_id: int
    iface_obj: NetBoxObject
    mac_obj: NetBoxObject | None


class PrunedNetworkMetrics(NamedTuple):
    """Metrics of the processing of orphan networks."""

    pruned_interfaces: int
    pruned_ips: int
    pruned_macs: int
    errors: int


class SingleInterfaceResult(NamedTuple):
    """
    Result of processing a single interface.
    Contains: (Interface, assigned IP or None, assigned MAC or None, boolean indicating whether there were changes).
    """

    iface_obj: NetBoxObject
    ip_obj: NetBoxObject | None
    mac_obj: NetBoxObject | None
    any_changes: bool


class InterfaceSyncResult(NamedTuple):
    """
    Global result of processing a list of interfaces for a node.
    Contains: (number of errors, list of primary IP candidates, boolean indicating whether there were changes).
    """

    errors: int
    ipv4_candidates: list[Ipv4Candidate]
    any_changes: bool


class InterfaceFingerprint(NamedTuple):
    """
    Network identity of an interface, used to recognize it even if it changes its name.
    `ip_hosts` are addresses without a mask (a mask change does not alter the identity).
    """

    ip_hosts: frozenset[str]
    macs: frozenset[str]


class InterfaceRename(NamedTuple):
    """Detected rename of an existing interface in NetBox (e.g. 'ens18' → 'eth0')."""

    old_name: str
    new_name: str
    evidence: str  # Signals that back the match (e.g. "IP 10.0.0.5").


class NetworkInterfaceData(TypedDict):
    """Represents the parsed data structure of a network interface."""

    name: str
    enabled: bool
    mac: str | None
    ip: str | None
    prefix: str | None
    cidr: str | None
    network: str | None


class BaseNodeData(TypedDict):
    """Represents the resolved base structure of a node before synchronizing."""

    machine_name: str
    inventory_uuid: str
    machine_type: str
    payload: NetBoxPayload


# ============================================================
# NETBOX ENDPOINTS
# ============================================================


class NetBoxEndpoints(BaseModel):
    """
    Represents the NetBox endpoints used by the script.

    The class is used to centralize access to the NetBox
    endpoints and to ensure that all the required endpoints are
    available before starting any synchronization
    operation.
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
    prefixes: Endpoint
    mac_addresses: Endpoint


# ============================================================
# YAML CONFIGURATION MODELS
# ============================================================


def _generate_slug_for_model(data: Any) -> Any:
    """Shared helper to auto-generate 'slug' from 'name'."""
    if isinstance(data, dict):
        raw_slug = data.get("slug") or data.get("name")
        if raw_slug:
            data["slug"] = slugify(str(raw_slug))
    return data


class SiteConfig(BaseModel):
    """Configuration of the Site in NetBox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    slug: str | None = None

    @model_validator(mode="before")
    @classmethod
    def generate_slug_if_missing(cls, data: Any) -> Any:
        """
        Automatically generates a slug from the name if the value is empty.
        Returns the generated slug or the original value.
        """
        return _generate_slug_for_model(data)

    @field_validator("name", "slug")
    @classmethod
    def validate_no_unresolved_vars(cls, v: str | None) -> str | None:
        """
        Validates that the string does not contain unresolved variables (marked with @@).
        Returns the original value if it is valid.
        """
        if v is not None and "$" in v and re.search(r"\$\{?\w+\}?", v):
            raise ValueError(
                f"The value contains unresolved environment variables: '{v}'"
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
        Automatically generates a slug from the name if the value is empty.
        Returns the generated slug or the original value.
        """
        return _generate_slug_for_model(data)


class ClusterTypeConfig(BaseModel):
    """Configuration of the ClusterType in NetBox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default: ClusterTypeDefaultConfig


class DeviceRoleConfig(BaseModel):
    """Configuration of a DeviceRole."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    slug: str | None = None
    color: str = Field(default="9e9e9e", pattern=r"^[0-9a-fA-F]{6}$")
    vm_role: bool = Field(default=True)

    @model_validator(mode="before")
    @classmethod
    def generate_slug_if_missing(cls, data: Any) -> Any:
        """
        Automatically generates a slug from the name if the value is empty.
        Returns the generated slug or the original value.
        """
        return _generate_slug_for_model(data)


class ChoiceItemConfig(BaseModel):
    """Individual element of a Choice Set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = Field(min_length=1)
    label: str = Field(min_length=1)


class ChoiceSetConfig(BaseModel):
    """Definition of a Choice Set for Custom Fields of type 'select'."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    choices: list[ChoiceItemConfig] = Field(default_factory=list)


OBJECT_TYPE_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


class CustomFieldConfig(BaseModel):
    """Unified Custom Field definition for custom fields."""

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
    filter_logic: Literal["loose", "exact", "disabled"] | None = None

    @field_validator("object_types")
    @classmethod
    def validate_object_types(cls, v: list[str]) -> list[str]:
        """
        Validates that the object types are in the allowed list.
        Returns the list of object_types if it is valid.
        """
        for ot in v:
            if not OBJECT_TYPE_PATTERN.match(ot):
                raise ValueError(
                    f"Invalid Object Type format: '{ot}'. "
                    "Expected 'app_label.model' (e.g. 'dcim.device')."
                )
        return v

    @model_validator(mode="after")
    def validate_choice_set_if_select(self) -> "CustomFieldConfig":
        """
        Validates that a field of type select has a choice_set defined.
        Returns the current configuration if it is valid.
        """
        if self.type in ("select", "multiselect") and not self.choice_set:
            raise ValueError(
                "Fields of type 'select' or 'multiselect' must define a 'choice_set'."
            )
        return self


class FieldMappingConfig(BaseModel):
    """Mapping definition between CSV columns and a NetBox attribute."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str | list[str]
    target: str = Field(min_length=1)
    required: bool = False
    is_unique: bool = False
    cast: CastType | None = None
    transform: Literal["concat_dot", "coalesce", "product"] | None = None

    @model_validator(mode="after")
    def validate_transform_and_source(self) -> "FieldMappingConfig":
        """
        Validates the transformation and source column configurations.
        Returns the current configuration if it is valid.
        """
        source_list = self.source if isinstance(self.source, list) else [self.source]

        if not source_list:
            raise ValueError(
                f"The 'source' field for target '{self.target}' cannot be empty."
            )

        if any(not s.strip() for s in source_list):
            raise ValueError(
                f"In 'source' (target '{self.target}'), no element can be empty."
            )

        # Transform combines several columns, so it requires a list
        if self.transform and not isinstance(self.source, list):
            raise ValueError(
                f"The transform '{self.transform}' for target '{self.target}' "
                "requires 'source' to be explicitly a list."
            )

        if isinstance(self.source, list) and not self.transform:
            raise ValueError(
                f"If 'source' is a list (target '{self.target}'), "
                "you must explicitly define a 'transform' "
                "(e.g. concat_dot, coalesce, product)."
            )

        return self


class StatusDefaultConfig(BaseModel):
    """Default status configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    default: str = Field(min_length=1)


class NodeMappingConfig(BaseModel):
    """Mapping and fallback configuration for a specific node type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: StatusDefaultConfig
    native_mappings: list[FieldMappingConfig] = Field(default_factory=list)
    custom_mappings: list[FieldMappingConfig] = Field(default_factory=list)


class NodeTypesConfig(BaseModel):
    """Configuration grouped by node type (device vs virtual_machine)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device: NodeMappingConfig
    virtual_machine: NodeMappingConfig

    def get_config(self, node_type: NodeType) -> NodeMappingConfig:
        """Returns the strongly typed mapping configuration according to the node type."""
        if node_type == NodeType.DEVICE:
            return self.device
        return self.virtual_machine


class CsvColumnDef(BaseModel):
    """Definition of a source CSV column."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source: str = Field(min_length=1)
    required: bool = False
    map: dict[str, Any] | None = None


class NetBoxMappingConfig(BaseModel):
    """
    Complete configuration and mapping contract loaded from netbox_mapping.yaml.
    Validates types, value constraints and referential consistency.
    Centralizes access to columns and runtime utility methods such as is_empty().

    The lists and dictionaries of this model (e.g. custom_field_definitions) are
    populated automatically by Pydantic in the load_config() function when deserializing the
    YAML file.
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
        Initializes the empty values and the O(1) indexed map of Custom Fields.
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
        Validates that the cross references within the configuration are correct.
        Returns the complete configuration if it is valid.
        """
        # 1. Validate that the "Others" role exists (case-insensitive) for fallback
        role_names_lower = {r.name.strip().lower() for r in self.device_roles}
        if "others" not in role_names_lower:
            raise ValueError(
                "The 'device_roles' list must include an 'Others' role "
                "as a fallback for unrecognized roles."
            )

        cf_names = {cf.name for cf in self.custom_field_definitions}
        col_def = self.csv_columns.get("machine_type")
        if not col_def or not col_def.map:
            raise ValueError(
                "The 'machine_type' column must have a 'map' configured in csv_columns."
            )

        # 3. Validate custom_mappings vs custom_field_definitions
        for node_type, node_config in [
            (NodeType.DEVICE, self.node_types.device),
            (NodeType.VIRTUAL_MACHINE, self.node_types.virtual_machine),
        ]:
            for cmap in node_config.custom_mappings:
                if cmap.target not in cf_names:
                    raise ValueError(
                        f"The custom_mapping target '{cmap.target}' in '{node_type}' "
                        "is not defined in custom_field_definitions."
                    )

        return self

    def get_required_columns(self) -> set[str]:
        """
        Returns the set of mandatory columns configured in the YAML.
        This list defines the columns that must be *present in the headers*
        of the CSV (row 1). It does not imply that every row must necessarily have a
        *non-empty value* in that column.
        """
        return {v.source for v in self.csv_columns.values() if v.required}

    def get_all_expected_columns(self) -> set[str]:
        """
        Returns the complete catalog of column names expected
        in the CSV.
        """
        return {v.source for v in self.csv_columns.values()}

    def get_column_def_by_source(self, source_name: str) -> CsvColumnDef | None:
        """
        Looks up the column definition from its exact name (source) in the CSV.
        Returns the column definition if it exists, None otherwise.
        """
        for col_def in self.csv_columns.values():
            if col_def.source == source_name:
                return col_def
        return None

    def _apply_map(
        self, col_def: CsvColumnDef | None, value: str, identifier: str, strict: bool
    ) -> Any:
        """
        Applies a mapping dictionary to a given value.
        Returns the mapped value, or raises an exception if strict=True and there is no match.
        """
        if not col_def or not col_def.map:
            return value

        norm_val = value.strip().lower()
        mapped = col_def.map.get(norm_val)
        if mapped is not None:
            return mapped

        # If the value is already a valid target value (fall-through)
        if norm_val in col_def.map.values():
            return norm_val

        if not strict:
            return None

        raise RowValidationError(
            f"The value '{value}' of the {identifier} is not defined in the configured map."
        )

    def map_value(self, alias: str, value: str, strict: bool = True) -> Any:
        """
        Applies the transformation map of the column identified by its alias.
        Returns the transformed value.
        """
        col_def = self.csv_columns.get(alias)
        return self._apply_map(col_def, value, f"column '{alias}'", strict)

    def map_value_by_source(self, source: str, value: str, strict: bool = True) -> Any:
        """
        Same as map_value, but looks up the column by its exact name (source) in the CSV.
        Returns the transformed value.
        """
        col_def = self.get_column_def_by_source(source)
        return self._apply_map(col_def, value, f"source column '{source}'", strict)

    def get_custom_field_def(self, target: str) -> CustomFieldConfig | None:
        """Returns the definition of a Custom Field in O(1) by target name."""
        return self._custom_field_defs_map.get(target)

    def get_all_custom_field_defs(self) -> list[CustomFieldConfig]:
        """Returns the complete list of Custom Field definitions."""
        return list(self._custom_field_defs_map.values())

    def is_empty(self, value: Any) -> bool:
        """
        Determines whether a value is considered empty according to empty_values.
        Returns True if the value is considered empty, False otherwise.
        """
        if value is None:
            return True
        return str(value).strip().lower() in self._empty_values_set

    def is_cluster_host(self, machine_type: str) -> bool:
        """Determines whether a machine_type (already mapped) is a cluster host (hypervisor)."""
        return machine_type == "hypervisor"

    def is_virtual_machine(self, machine_type: str) -> bool:
        """Determines whether a machine_type (already mapped) corresponds to a virtual machine."""
        return machine_type == "vm"

    def resolve_node_type(self, machine_type: str) -> NodeType:
        """
        Resolves the NodeType based on the semantic lists.
        Raises RowValidationError if the field is empty.
        Returns the corresponding NodeType.
        """
        if not machine_type:
            raise RowValidationError("The 'machine_type' field is empty.")

        mapped_type = self.map_value("machine_type", machine_type, strict=True)

        if self.is_virtual_machine(mapped_type):
            return NodeType.VIRTUAL_MACHINE
        return NodeType.DEVICE


# ===========================================================
# DATA CACHE
# ===========================================================


class MockNetBoxRecord(BaseModel):
    """Represents a simulated NetBox object for runs in dry-run mode."""

    model_config = ConfigDict(frozen=True, extra="allow")

    id: int = 0
    name: str = ""
    slug: str = ""
    type: str = ""
    model: str = ""
    vm_role: bool = False
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    # Network and assignments (for IP/MAC simulations)
    address: str | None = None
    prefix: str | None = None
    mac_address: str | None = None
    assigned_object_id: int | None = None
    assigned_object_type: str | None = None


class CacheStore(BaseModel):
    """
    Represents the cache of NetBox objects that is kept
    during the whole execution of the script.

    It includes:
        manufacturers: maps name → Manufacturer
        device_types: maps (manufacturer_id, model) → DeviceType
        platforms: maps name → Platform
        racks: maps "site_name/rack_name" → Rack
        clusters: maps name → Cluster
        device_roles: maps name_lower → DeviceRole
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
# UTILITIES
# ============================================================


def slugify(name: str) -> str:
    """Generates a valid slug identical to the NetBox standard (Django slugify)."""
    # 1. Decompose diacritics and remove non-ascii
    slug = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    # 2. Convert to lowercase, remove characters that are not alphanumeric, spaces or hyphens
    slug = re.sub(r"[^\w\s-]", "", slug.lower())
    # 3. Replace spaces and runs of hyphens with a single hyphen, and clean the edges
    slug = re.sub(r"[-\s]+", "-", slug).strip("-_")
    # 4. NetBox limits slugs to 100 characters.
    return slug[:100]


def generate_fallback_slug(base_slug: str, original_name: str) -> str:
    """Generates a deterministic fallback slug using a short md5 hash."""
    hash_suffix = hashlib.md5(original_name.encode("utf-8")).hexdigest()[:4]
    return f"{base_slug}-{hash_suffix}"


def _is_custom_field_changed(curr_val: Any, new_val: Any) -> bool:
    """Determines whether a custom_fields payload has real changes."""
    if not isinstance(new_val, dict):
        return curr_val != new_val
    if not isinstance(curr_val, dict):
        return True
    return any(curr_val.get(k) != v for k, v in new_val.items())


def _is_relation_changed(curr_val: Any, new_val: Any) -> bool | None:
    """
    Evaluates changes in nested pynetbox objects (FKs or Choices).
    Returns None if the value does not qualify as a manageable relation.
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
    """Evaluates whether a specific field differs from its current value."""
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
    Determines which fields would change in the Record when applying the payload.
    Returns a dictionary with the detected modifications.
    """
    updates: NetBoxPayload = {}

    for key, new_val in payload.items():
        if not hasattr(record, key) or _is_field_changed(
            key, getattr(record, key), new_val
        ):
            updates[key] = new_val

    if updates:
        obj_name = getattr(record, "name", str(record))
        log.debug("Changes detected in %s: %s", obj_name, updates)

    return updates


def format_diff_keys(existing_obj: NetBoxObject, diff: dict[str, Any]) -> list[str]:
    """
    Generates a flat list of modified keys from a dictionary of differences.
    If a key is a dictionary (e.g. 'custom_fields'), it evaluates against the existing
    object to extract only the specific subkeys that changed.
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

        # We extract the subkeys that actually changed
        keys.extend(
            f"{key}.{sub_k}"
            for sub_k, sub_v in new_val.items()
            if curr_val.get(sub_k) != sub_v
        )

    return keys


def parse_int(value: Any) -> int:
    """Converts a value to int, or raises ValueError if it is not convertible."""
    try:
        return int(str(value).strip())
    except (ValueError, TypeError) as e:
        raise ValueError("Not a valid integer.") from e


def _to_float(value: Any) -> float:
    """Tries to parse a value to float, normalizing commas to dots."""
    return float(str(value).strip().replace(",", "."))


def parse_float(value: Any) -> float:
    """Converts a value to float, or raises ValueError if it is not convertible."""
    try:
        return _to_float(value)
    except (ValueError, TypeError) as e:
        raise ValueError("Not a valid decimal number.") from e


def parse_float_to_int(value: Any) -> int:
    """Converts a number (potentially float) to its nearest integer."""
    try:
        val_float = _to_float(value)
        return round(val_float)
    except (ValueError, TypeError) as e:
        raise ValueError("Not a valid numeric value.") from e


def parse_int_gb_to_mb(value: Any) -> int:
    """Converts GB (string/float) to MB (integer) using a 1000 multiplier.
    NetBox expects MB for memory/disk."""
    try:
        gb = _to_float(value)
        return round(gb * 1000)
    except (ValueError, TypeError) as e:
        raise ValueError("Not a valid numeric value.") from e


def parse_rack_position(value: Any) -> float:
    """
    Converts a rack position (U) into a float valid for `Device.position`.

    Design decisions:
    - NetBox stores `position` as a Decimal (1 decimal) and its `clean()` requires
      a minimum of 1 and increments of 0.5. It is validated here to report a clear
      error per row instead of an HTTP 400 from the API.
    - `value * 2` is compared against an integer: multiples of 0.5 are exact
      in floating point, so there are no false negatives due to rounding.
    """
    try:
        position = _to_float(value)
    except (ValueError, TypeError) as e:
        raise ValueError("Not a valid numeric rack position.") from e

    if position < RACK_POSITION_MIN:
        raise ValueError(
            f"The position must be greater than or equal to {RACK_POSITION_MIN}."
        )
    if not (position * 2).is_integer():
        raise ValueError("The position must be a multiple of 0.5 rack units.")
    return position


def parse_u_height(value: Any) -> float:
    """
    Converts the height (U) of a DeviceType into a float valid for NetBox.

    Design decisions:
    - NetBox stores `u_height` as a Decimal (1 decimal) and its `clean()` requires
      increments of 0.5. 0 is valid (0U devices, e.g. child blades of a chassis).
    - It is validated here to report a clear error per row instead of an HTTP 400.
    """
    try:
        u_height = _to_float(value)
    except (ValueError, TypeError) as e:
        raise ValueError("Not a valid numeric height (U).") from e

    if u_height < 0:
        raise ValueError("The height (U) cannot be negative.")
    if not (u_height * 2).is_integer():
        raise ValueError("The height (U) must be a multiple of 0.5 rack units.")
    return u_height


def parse_bool_yes_no(value: Any) -> bool:
    """
    Converts a value to boolean according to specific 'yes/no' rules.
    Raises ValueError if it does not match.
    """
    v = str(value).strip().lower()
    if v in ("yes", "true", "1"):
        return True
    if v in ("no", "false", "0"):
        return False
    raise ValueError("Not 'yes' or 'no'.")


def apply_cast(value: Any, cast_type: CastType, target: str) -> FieldValue:
    """Applies a specific cast to a value according to the field definition."""
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
            case CastType.RACK_POSITION:
                return parse_rack_position(value)
            case CastType.BOOL_YES_NO:
                return parse_bool_yes_no(value)
            case CastType.LOWER:
                return str(value).lower() if value is not None else value
            case _ as unreachable:
                assert_never(unreachable)
    except ValueError as e:
        raise RowValidationError(
            f"Invalid value '{value}' for the field '{target}'. {e}"
        ) from e


def concat_dot(parts: list[str], config: NetBoxMappingConfig) -> str:
    """Concatenates non-empty parts with '. ' as the separator."""
    clean = [p.strip() for p in parts if not config.is_empty(p)]
    return ". ".join(clean)


def _extract_raw_csv_value(
    row: CsvRow,
    col_alias: str,
    config: NetBoxMappingConfig,
    strict: bool = False,
) -> str:
    """
    Extracts and sanitizes a value of the CSV using its alias defined in the YAML.
    If the cell is empty (according to config.is_empty) or the column does not exist or is
    mapped to None, it returns an empty string (""). If 'strict' is True and the
    resulting value is empty, it raises RowValidationError.
    If the alias does not even exist in the configuration, it raises ConfigValidationError.
    """
    if col_alias not in config.csv_columns:
        raise ConfigValidationError(
            f"Critical configuration error: The alias '{col_alias}' requested "
            "by the script does not exist in `csv_columns` of the YAML file."
        )

    col_name = config.csv_columns[col_alias].source
    if not col_name:
        if strict:
            raise RowValidationError(
                f"The mandatory field '{col_alias}' is not mapped in the configuration."
            )
        return ""

    val = row.get(col_name, "").strip()
    val = "" if config.is_empty(val) else val

    if strict and not val:
        raise RowValidationError(
            f"The mandatory field '{col_alias}' is empty in the CSV."
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
    Extracts the value of the CSV and applies the mapping function (map_value)
    if it exists in the configuration.
    """

    def _fallback_value(raw_val: str = "") -> Any:
        if fallback is not None:
            return fallback
        return raw_val

    raw_val = _extract_raw_csv_value(row, col_alias, config, strict_extract)
    if not raw_val:
        return _fallback_value(raw_val)

    col_def = config.csv_columns.get(col_alias)
    if col_def and col_def.map:
        mapped_val = config.map_value(col_alias, raw_val, strict=strict_map)
        if mapped_val is not None:
            return mapped_val
        return _fallback_value(raw_val)
    return raw_val


def get_netbox_object_id(obj: NetBoxObject) -> int:
    """Returns the ID of a NetBox object. If the object is None, it returns 0."""
    obj_id = getattr(obj, "id", None)
    if obj_id is None:
        return 0
    return int(obj_id)


def get_node_type_from_object(obj: Endpoint | NetBoxObject) -> NodeType:
    """
    Returns the node type (virtual_machine or device) from
    an Endpoint or NetBoxObject.
    """
    url = getattr(obj, "url", "")
    name = getattr(obj, "name", "")

    # Useful for pynetbox Endpoints
    if "virtualization" in url or name == "virtual-machines":
        return NodeType.VIRTUAL_MACHINE

    # If it is a NetBoxObject it will have the attribute of the parent
    if hasattr(obj, "virtual_machine"):
        return NodeType.VIRTUAL_MACHINE

    return NodeType.DEVICE


def get_node_type_from_row(row: CsvRow, config: NetBoxMappingConfig) -> NodeType:
    """Extracts the machine type and maps it to the NetBox node type."""
    machine_type_val = extract_csv_value(row, "machine_type", config)
    return config.resolve_node_type(machine_type_val)


def resolve_mapping_path(args_mapping: str | None) -> Path:
    """Resolves the path of the YAML configuration file."""
    if args_mapping:
        return Path(args_mapping)
    return DEFAULT_MAPPING_PATH


def count_machine_names(
    rows: list[CsvRow], config: NetBoxMappingConfig
) -> Counter[str]:
    """Generates a count of the occurrences of machine names in the CSV (ignoring empty ones)."""
    counts = Counter[str]()
    for row in rows:
        val = extract_csv_value(row, "machine_name", config)
        if isinstance(val, str):
            name = val.strip()
            if name:
                counts[name] += 1
    return counts


def count_inventory_uuids(
    rows: list[CsvRow], config: NetBoxMappingConfig
) -> Counter[str]:
    """Generates a count of the occurrences of UUIDs in the CSV, normalized to
    lowercase (ignoring empty ones)."""
    counts = Counter[str]()
    for row in rows:
        uuid = extract_csv_value(row, "inventory_uuid", config).lower()
        if not config.is_empty(uuid):
            counts[uuid] += 1
    return counts


# ============================================================
# CONFIGURATION LOADING
# ============================================================


def load_config(mapping_path: Path) -> NetBoxMappingConfig:
    """
    Loads and validates the YAML mapping file using Pydantic.
    If there are syntax or schema validation errors, it reports them
    in detail and ends the execution in a controlled way.
    """
    log.debug("Loading configuration from: %s", mapping_path)
    if not mapping_path.is_file():
        raise ConfigValidationError(f"Mapping file not found: {mapping_path}")

    # Load the YAML
    try:
        with mapping_path.open("r", encoding="utf-8") as f:
            raw_yaml = f.read()
        # Expands $VAR or ${VAR} syntax using environment variables
        expanded_yaml = os.path.expandvars(raw_yaml)
        raw = yaml.safe_load(expanded_yaml)
    except yaml.YAMLError:
        raise ConfigValidationError(f"YAML syntax error while reading {mapping_path}")

    if not isinstance(raw, dict):
        raise ConfigValidationError(
            f"The mapping file {mapping_path} does not contain a valid YAML dictionary.",
        )

    # Validate the Pydantic schema for the mapping file
    try:
        return NetBoxMappingConfig.model_validate(raw)
    except ValidationError as exc:
        error_msgs = []
        for err in exc.errors():
            loc = " -> ".join(str(p) for p in err.get("loc", []))
            msg = err.get("msg", "")
            inp = err.get("input")
            error_msgs.append(f"[{loc}]: {msg} (received value: {inp!r})")

        full_error = "\n".join(error_msgs)
        raise ConfigValidationError(
            f"Validation error in the YAML mapping file ({mapping_path}):\n{full_error}"
        )


def load_dotenv(env_path: Path | None = None) -> None:
    """
    Loads environment variables from a .env file if it exists.
    It does not overwrite variables that are already defined in the environment.
    """
    if env_path is None:
        env_path = DOTENV_PATH

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
    Loads the environment variables required for the connection with NetBox.

    - NETBOX_URL: URL of the NetBox API (e.g. http://netbox.company.com).
    - NETBOX_TOKEN: Authentication token with write permissions over dcim,
      virtualization, ipam, extras, core.
    - NETBOX_VERIFY_SSL: 'true' if the SSL certificate must be verified
      (default), 'false' to skip the verification.
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
            f"Required environment variables not defined: {missing_str}"
        )

    return url, token, verify_ssl


# ============================================================
# PYNETBOX CLIENT
# ============================================================


def _log_netbox_response(
    response: requests.Response, *args: Any, **kwargs: Any
) -> None:
    """Hook to intercept and log responses of the NetBox API."""
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
    Builds and returns a fully configured NetBox API client.

    The initialization includes: HTTP session configuration (including
    disabling SSL if specified), authentication with the token, and an
    initial connectivity check against the sites endpoint.
    """
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    session = requests.Session()
    session.verify = verify_ssl
    session.hooks["response"].append(_log_netbox_response)

    nb = Api(url, token=token)
    nb.http_session = session

    # Verify connectivity with a lightweight call.
    try:
        nb.dcim.sites.filter(limit=1)
    except Exception as e:
        raise NetBoxApiError(f"Could not connect to NetBox ({url})") from e

    log.info("Connected to NetBox %s", url)
    return nb


def build_netbox_endpoints(nb: Api) -> NetBoxEndpoints:
    """
    Instantiates and packs in a centralized way the references to the
    pynetbox endpoints required by the script into an immutable
    container (NetBoxEndpoints).

    Endpoint resolution in pynetbox operates in client-side
    memory (lazy). It fails with AttributeError if the version of the
    pynetbox library does not expose any of the main client
    applications (for example, 'core' in NetBox 4.x).
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
            prefixes=nb.ipam.prefixes,
            mac_addresses=nb.dcim.mac_addresses,
        )
    except AttributeError as e:
        raise NetBoxApiError(
            "The pynetbox instance does not expose one of the endpoints "
            "required by the script"
        ) from e


# ============================================================
# READ AND VALIDATE CSV
# ============================================================


def _record_start_line(reader_line_num: int, row: dict[str | None, Any]) -> int:
    """
    Computes the physical line where a CSV record starts.

    `csv.reader.line_num` gives the line where the record *ends*; line breaks
    embedded in quoted cells make a record span several lines,
    so they are subtracted to get the first one. Extra fields (which DictReader
    groups in a list under the `None` key) are also considered.
    """
    embedded_newlines = 0
    for value in row.values():
        if isinstance(value, str):
            embedded_newlines += value.count("\n")
        elif isinstance(value, list):
            embedded_newlines += sum(item.count("\n") for item in value)
    return reader_line_num - embedded_newlines


def _normalize_csv_record(
    row: dict[str | None, Any], line_number: int, expected_columns: int
) -> CsvRow | None:
    """
    Validates the structure of a record read by DictReader and normalizes it to `CsvRow`.

    Design decisions:
    - Short record (trailing cells missing): DictReader pads with `None`, which breaks
      the string utilities downstream. It is completed with "" (equivalent to empty) and
      a warning is issued, just like with the columns missing from the header.
    - Long record (extra fields): it usually indicates an unquoted comma that
      shifts the columns, so the values could end up in the wrong
      field (e.g. a UUID in another column). It is discarded instead of guessing.

    Returns the normalized record, or None if it must be discarded.
    """
    extra_fields = row.pop(None, None)
    if extra_fields:
        log.error(
            "ERROR line %d: it has %d fields but the header defines %d "
            "(possible unquoted comma). The record is discarded.",
            line_number,
            expected_columns + len(extra_fields),
            expected_columns,
        )
        return None

    missing_columns = [name for name, value in row.items() if value is None]
    if missing_columns:
        log.warning(
            "Line %d: %d trailing field(s) are missing (%s). They will be treated as empty.",
            line_number,
            len(missing_columns),
            ", ".join(repr(c) for c in missing_columns),
        )

    return {
        str(name): ("" if value is None else str(value)) for name, value in row.items()
    }


def _read_csv(path: Path) -> tuple[list[str], CsvData]:
    """
    Reads merged_inventory.csv.
    Returns a tuple: (headers, CsvData), where each row is a dictionary
    {header: value} and `line_numbers` gives its real line in the file (useful to
    report errors even if there are multiline cells or blank lines).
    Records with an invalid structure are discarded and counted in `rejected_rows`.
    """
    if not path.is_file():
        raise ConfigValidationError(f"Input CSV not found: {path}")

    rows: list[CsvRow] = []
    line_numbers: list[int] = []
    rejected_rows = 0
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        for raw_row in reader:
            line_number = _record_start_line(reader.line_num, raw_row)
            record = _normalize_csv_record(raw_row, line_number, len(headers))
            if record is None:
                rejected_rows += 1
                continue
            rows.append(record)
            line_numbers.append(line_number)

    log.info("CSV read: %d rows, %d columns", len(rows), len(headers))
    return list(headers), CsvData(rows, line_numbers, rejected_rows)


def _validate_csv_headers(
    headers: list[str],
    config: NetBoxMappingConfig,
) -> list[str]:
    """
    Validates that the CSV headers include all the mandatory columns
    configured in 'csv_columns' with the 'required: true' flag within netbox_mapping.yaml.
    It validates the *existence of the column in the header*, not that every row
    must have a non-empty value.
    Raises ConfigValidationError if mandatory columns are missing.
    Returns the list of headers untouched if the validation is successful.
    """
    header_set = set(headers)

    # 1. Validation of critical columns (blocking ERROR)
    missing_required = [
        col for col in sorted(config.get_required_columns()) if col not in header_set
    ]
    if missing_required:
        cols_str = ", ".join(repr(c) for c in missing_required)
        raise ConfigValidationError(
            f"The CSV does not contain the following mandatory columns: {cols_str}"
        )

    # 2. Informational check of expected columns (informational WARNING)
    all_expected = config.get_all_expected_columns()
    missing_optional = [
        col
        for col in sorted(all_expected - config.get_required_columns())
        if col not in header_set
    ]
    if missing_optional:
        log.warning(
            "Mapping columns not found in the CSV (they will be treated as empty): %s",
            ", ".join(repr(c) for c in missing_optional),
        )

    return headers


def read_and_validate_csv(
    csv_path: Path,
    config: NetBoxMappingConfig,
) -> CsvData:
    """
    Orchestrates the reading of the CSV file and the validation of its headers.
    Returns the parsed rows (dictionaries) and their source line, ready to be processed.
    """
    headers, data = _read_csv(csv_path)
    _validate_csv_headers(headers, config)
    return data


# ============================================================
# GET OR CREATE PATTERN (CACHE)
# ============================================================


def _is_collision_error(e: RequestError, fields: tuple[str, ...]) -> bool:
    """Detects whether a RequestError (HTTP 400) is due to a collision on specific fields."""
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
    """Checks whether the RequestError is caused specifically by a duplicate slug."""
    return _is_collision_error(e, ("slug",))


def _is_name_collision(e: RequestError) -> bool:
    """Checks whether the RequestError is caused specifically by a duplicate name."""
    return _is_collision_error(e, ("name", "non_field_errors"))


def _search_in_netbox(
    endpoint: Endpoint,
    filter_kwargs: dict[str, Any],
    create_kwargs: dict[str, Any],
    name: str,
    preventive_slug_search: bool,
) -> Record | None:
    """Looks up an object in NetBox, applying a preventive search by slug if required."""
    results: list[Record] = list(endpoint.filter(**filter_kwargs))
    if results:
        return results[0]

    if preventive_slug_search and "slug" in create_kwargs:
        slug_val = create_kwargs["slug"]
        slug_results: list[Record] = list(endpoint.filter(slug=slug_val))
        if slug_results:
            log.warning(
                "Object '%s' does not exist by its filter fields, but its slug '%s' "
                "matches an existing object in '%s' ('%s'). It is reused.",
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
    """Generates a mock of the object for Dry-Run mode."""
    log.info("[DRY-RUN] WOULD CREATE object in '%s': %s", endpoint.name, name)
    mock_kwargs = create_kwargs.copy()
    return MockNetBoxRecord(id=0, **mock_kwargs)


def _create_with_fallback_slug(
    endpoint: Endpoint,
    original_name: str,
    **kwargs: Any,
) -> Record:
    """
    Tries to create an object in NetBox. If a slug collision occurs (RequestError),
    it generates a deterministic fallback slug and retries the creation.
    Returns the created object.
    """
    slug: str = kwargs.get("slug", "")
    try:
        return cast(Record, endpoint.create(**kwargs))
    except RequestError as e:
        if not _is_slug_collision(e):
            raise

        slug_fallback = generate_fallback_slug(slug, original_name)
        log.warning(
            "Slug '%s' collided when creating an object in '%s' with name '%s'; retrying with '%s'.",
            slug,
            endpoint.name,
            original_name,
            slug_fallback,
        )
        kwargs["slug"] = slug_fallback
        with netbox_error_wrap(
            f"Unable to create object in '{endpoint.name}' with name '{original_name}' "
            "due to a persistent slug collision or a NetBox rejection"
        ):
            return cast(Record, endpoint.create(**kwargs))


def _execute_creation(
    endpoint: Endpoint,
    create_kwargs: dict[str, Any],
    name: str,
    use_fallback_slug: bool,
) -> Record:
    """Executes the creation of the object in NetBox, handling slug fallbacks."""
    if use_fallback_slug:
        obj = _create_with_fallback_slug(
            endpoint,
            name,
            **create_kwargs,
        )
    else:
        obj = cast(Record, endpoint.create(**create_kwargs))
    log.info("CREATED Object in '%s': %s", endpoint.name, name)
    return obj


def _create_record(
    endpoint: Endpoint,
    create_kwargs: NetBoxPayload,
    name: str,
    dry_run: bool,
) -> NetBoxObject:
    """
    Creates an object in NetBox (or its mock in dry-run) without a prior lookup.

    Design decisions:
    - Write primitive shared by the `ensure_*` / `upsert_*` functions that already
      resolved the existence of the object on their own and only need to create it.
    """
    if dry_run:
        return _create_dry_run_mock(endpoint, name, create_kwargs)

    with netbox_error_wrap(f"Error creating object in '{endpoint.name}': {name}"):
        return _execute_creation(endpoint, create_kwargs, name, use_fallback_slug=False)


def apply_update(
    existing_obj: NetBoxObject,
    diff: NetBoxPayload,
    label: str,
    dry_run: bool,
) -> bool:
    """
    Applies in NetBox an already computed diff over an existing object.

    Design decisions:
    - Write primitive for those who compute their own diff (domain-specific
      normalization); those who do not, use `update_if_changed`.
    - The return value of `Record.update` is propagated: pynetbox returns False when it
      sent nothing to the API, and that must be reported as "no changes".

    Returns:
        bool: True if the object was (or would have been) updated.
    """
    diff_keys = format_diff_keys(existing_obj, diff)

    if dry_run:
        log.info("[DRY-RUN] WOULD UPDATE %s - Changes: %s", label, diff_keys)
        return True

    with netbox_error_wrap(f"Error updating {label}"):
        updated = cast(Record, existing_obj).update(diff)

    if not updated:
        log.debug(
            "NetBox applied no changes to %s despite the diff: %s", label, diff_keys
        )
        return False

    log.info("UPDATED %s - Changes: %s", label, diff_keys)
    return True


def update_if_changed(
    existing_obj: NetBoxObject,
    payload: NetBoxPayload,
    label: str,
    dry_run: bool,
) -> tuple[NetBoxObject, bool]:
    """
    Updates in NetBox only the payload fields that differ from the object.

    Design decisions:
    - It sends only the diff (not the full payload) to minimize traffic and
      keep idempotency: without differences no request is made.

    Returns:
        tuple[NetBoxObject, bool]: The object and a boolean indicating whether there were changes.
    """
    diff = check_record_changes(existing_obj, payload)
    if not diff:
        return existing_obj, False

    return existing_obj, apply_update(existing_obj, diff, label, dry_run)


def _handle_concurrency_backoff(
    name: str, endpoint_name: str, attempt: int, max_retries: int
) -> None:
    """Applies exponential backoff after a concurrent collision."""
    sleep_time = 0.5 * (2**attempt) + random.uniform(0, 0.5)
    log.warning(
        "Concurrency collision when creating '%s' in '%s'. "
        "Retrying in %.2fs (attempt %d/%d)...",
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
    Executes a single lookup-or-create attempt.
    Returns the object if it succeeds, or None if there was a concurrent collision and it must be retried.
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
                f"Could not create the object in '{endpoint.name}' with name '{name}'"
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
    Centralized Identity Map (Cache) engine for the Get-or-Create pattern.

    Design decisions:
    - O(1) network efficiency: It avoids avalanches of HTTP requests (N+1 queries) against
      NetBox by keeping an in-memory record of the entities already created or queried.
    - Collision prevention (preventive_slug_search): In NetBox, slugs must be
      strictly unique. If we look up an object by name and do not find it, NetBox
      will reject the creation if the generated slug collides with another object (HTTP 400).
      The preventive search by slug avoids the crash by allowing similar objects to be reused.
    - Explicit state flow: Although the cache is mutated in-place for maximum performance
      (avoiding massive memory copies), it is returned explicitly to keep
      architectural traceability and tell the developer that the state was altered.

    Returns:
        tuple[NetBoxObject, dict[Any, NetBoxObject]]: The final object and the propagated cache.
    """
    if cache_key in cache:
        log.debug("[Cache HIT] Object '%s' found in memory", name)
        return cache[cache_key], cache

    log.debug("[Cache MISS] Looking up '%s' in the API...", name)

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

    raise NetBoxApiError(f"Could not resolve '{name}' after {MAX_RETRIES} attempts.")


# ============================================================
# CUSTOM FIELDS: ensure_custom_fields
# ============================================================


def _normalize_choices(extra_choices: Any) -> list[list[str]]:
    """Converts the options of a choice set into a list of lists of strings."""
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
    """Synchronizes the options (choices) of a choice set.
    Returns the ID of the choice set."""

    def _get_choice_set_id(ch_set: Record) -> int:
        """
        Looks up a Choice Set by its name.
        Returns the ID of the Choice Set found, or raises an exception if it does not exist.
        """
        return cast(int, getattr(ch_set, "id", 0))

    extra_choices: Any = getattr(choice_set, "extra_choices", None)
    current_choices: list[list[str]] = _normalize_choices(extra_choices)

    if current_choices == choices:
        return _get_choice_set_id(choice_set)

    if dry_run:
        log.info("[DRY-RUN] WOULD UPDATE Choice Set: %s", choice_set_name)
        return _get_choice_set_id(choice_set)

    with netbox_error_wrap(f"Error updating Choice Set '{choice_set_name}'"):
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
    """Creates a choice set if it does not exist in NetBox.
    Returns the ID of the choice set and the updated cache.
    """

    def _get_choice_set_choices(choices: list[ChoiceItemConfig]) -> list[list[str]]:
        """
        Looks up a Choice Set by its name and gets its options (choices).
        Returns a list of lists with the values and labels of each option.
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
            f"Error creating Choice Set '{choice_set_name}': {e}"
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
    Creates a custom field if it does not exist in NetBox.
    Returns the Custom Field object and the updated cache.
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

    # NetBox filters partially (loose) by default; the fields used as
    # identifiers (e.g. inventory_uuid) require an exact match.
    if cf_def.filter_logic is not None:
        create_kwargs["filter_logic"] = cf_def.filter_logic

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
        raise ConfigValidationError(f"Error creating custom field '{name}': {e}") from e


def ensure_custom_fields(
    endpoints: NetBoxEndpoints,
    cfg: NetBoxMappingConfig,
    dry_run: bool,
) -> NameCache:
    """
    Ensures that all the custom fields defined in the YAML
    exist in NetBox.

    Besides the custom fields defined in 'custom_fields',
    it processes the special definitions:
        - machine_type
        - environment

    For custom fields of type 'select', it also ensures
    the existence of the associated Choice Set.

    NetBox 4.5+:
    - Object Types are queried through /api/core/object-types/.
    - The /api/extras/object-types/ endpoint was removed in NetBox 4.5.
    - Choice Sets are managed through
    /api/extras/custom-field-choice-sets/.

    Returns a dictionary mapping the name of the Custom Field to its object in NetBox.
    """
    # Get the existing Custom Fields and Choice Sets.
    custom_fields = cast(list[Record], endpoints.custom_fields.all())
    choice_sets = cast(list[Record], endpoints.choice_sets.all())

    cfs_cache: NameCache = {str(cf.name): cf for cf in custom_fields}
    choice_sets_cache: NameCache = {str(ch_set.name): ch_set for ch_set in choice_sets}

    # Get the unified O(1) list of Custom Field definitions.
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

        # Create the Custom Field if it does not exist.
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
# TAXONOMY: ensure_* (GET or CREATE)
# ============================================================


def ensure_site(
    sites_endpoint: Endpoint,
    site_cfg: SiteConfig,
    dry_run: bool,
) -> NetBoxObject:
    """Ensures that the Site defined in the YAML exists in NetBox.
    Returns the created Site object."""
    name = site_cfg.name
    slug = cast(str, site_cfg.slug)

    # We use an ephemeral cache only to reuse the get_or_create_cached logic,
    # although site is really evaluated only once per run in _execute_pipeline.
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
    CSV pre-scan: associates each cluster_name with the technology
    of the hypervisor (hypervisor_os) to determine which ClusterTypes
    to create dynamically.

    It only processes rows of type "Hypervisor". If two hypervisors of the
    same cluster report different OS values, it raises an explicit
    error to force the correction of the master.

    Design decisions:
    - The 'machine_type' mapping is non-strict: an unknown type is not
      a hypervisor and is ignored here. Reporting it as an ERROR per row is
      the responsibility of `_classify_rows`; raising from this global pre-scan
      would abort the whole pipeline because of a single invalid cell.

    Returns a dictionary {cluster_name: hypervisor_os}.
    """
    cluster_type_map: dict[str, str] = {}

    for row in rows:
        machine_type = extract_csv_value(row, "machine_type", config, strict_map=False)
        if not config.is_cluster_host(machine_type):
            continue

        cluster_name = extract_csv_value(row, "cluster_name", config)
        hypervisor_os = extract_csv_value(row, "hypervisor_os", config)

        if not cluster_name or not hypervisor_os:
            continue

        # Check whether there is an OS conflict in the cluster.
        existing_os = cluster_type_map.get(cluster_name)
        if existing_os is not None and existing_os != hypervisor_os:
            raise ConfigValidationError(
                f"OS conflict in the cluster '{cluster_name}': "
                f"one hypervisor reports '{existing_os}' and another '{hypervisor_os}'. "
                "Fix the master inventory."
            )

        cluster_type_map[cluster_name] = hypervisor_os

    if cluster_type_map:
        log.info(
            "Pre-scan: %d cluster(s) associated with a dynamic ClusterType.",
            len(cluster_type_map),
        )

    return cluster_type_map


def parse_device_type_attributes(
    row: CsvRow, config: NetBoxMappingConfig
) -> DeviceTypeAttributes:
    """
    Extracts and validates the DeviceType attributes ('height_u', 'part_number') of a row.

    An empty value is represented as `None` ("not reported"); an explicit 0 is a
    real value. Raises RowValidationError if any reported value is invalid.
    """
    raw_u_height = extract_csv_value(row, "height_u", config)
    u_height: float | None = None
    if raw_u_height:
        try:
            u_height = parse_u_height(raw_u_height)
        except ValueError as e:
            raise RowValidationError(
                f"Invalid value '{raw_u_height}' for 'height_u'. {e}"
            ) from e

    part_number: str | None = extract_csv_value(row, "part_number", config) or None
    if part_number is not None and len(part_number) > PART_NUMBER_MAX_LENGTH:
        raise RowValidationError(
            f"The P/N '{part_number}' exceeds the {PART_NUMBER_MAX_LENGTH} characters "
            "that NetBox allows."
        )

    return DeviceTypeAttributes(u_height, part_number)


class SourcedValue[T](NamedTuple):
    """Consolidated value of an attribute along with the CSV line it comes from."""

    value: T
    source_line: int


def _consolidate_attribute[T](
    model: DeviceTypeKey,
    attribute_label: str,
    current: SourcedValue[T] | None,
    candidate: T | None,
    candidate_line: int,
) -> SourcedValue[T] | None:
    """
    Applies the "first non-empty value" rule to an attribute of a model.

    `current` is the value already chosen, or None if there is no value yet.
    It warns when a later row contradicts the chosen value.
    Returns the value in force after considering the candidate.
    """
    if candidate is None:
        return current
    if current is None:
        return SourcedValue(candidate, candidate_line)

    if candidate != current.value:
        log.warning(
            "Conflict in DeviceType '%s': '%s' = %r (line %d) vs %r (line %d). "
            "%r is used; fix the master inventory.",
            model.label,
            attribute_label,
            current.value,
            current.source_line,
            candidate,
            candidate_line,
            current.value,
        )
    return current


def precompute_device_type_attributes(
    device_rows: list[NumberedRow],
    config: NetBoxMappingConfig,
) -> DeviceTypeAttrsMap:
    """
    CSV pre-scan: consolidates a single height and P/N value per model.

    Design decisions:
    - Height and P/N are attributes of the DeviceType, shared by all the devices of
      the same model. If every row wrote them, the last one would win and the type
      would oscillate on every run. One value per model is chosen before synchronizing.
    - Deterministic rule: the first non-empty value according to the CSV order, evaluated
      per attribute. Conflicts are warned about with their source lines.
    - Rows without manufacturer/model or with invalid values are ignored here: they
      are reported as SKIP/ERROR when synchronized. Raising from this global pre-scan
      would abort the whole pipeline because of a single cell.

    Returns a dictionary {DeviceTypeKey: DeviceTypeAttributes}.
    """
    heights: dict[DeviceTypeKey, SourcedValue[float] | None] = {}
    part_numbers: dict[DeviceTypeKey, SourcedValue[str] | None] = {}

    for line_number, row in device_rows:
        manufacturer = extract_csv_value(row, "manufacturer", config)
        model = extract_csv_value(row, "model", config)
        if not manufacturer or not model:
            continue

        key = DeviceTypeKey(manufacturer, model)
        try:
            attributes = parse_device_type_attributes(row, config)
        except RowValidationError as e:
            # The row will fail with ERROR when synchronized; here only the trace is left
            # (visible with --verbose) so the message is not duplicated.
            log.debug(
                "Line %d ignored in the consolidation of the DeviceType '%s': %s",
                line_number,
                key.label,
                e,
            )
            continue

        heights[key] = _consolidate_attribute(
            key, "Height (U)", heights.get(key), attributes.u_height, line_number
        )
        part_numbers[key] = _consolidate_attribute(
            key, "P/N", part_numbers.get(key), attributes.part_number, line_number
        )

    consolidated: DeviceTypeAttrsMap = {}
    for key in heights.keys() | part_numbers.keys():
        height = heights.get(key)
        part_number = part_numbers.get(key)
        consolidated[key] = DeviceTypeAttributes(
            height.value if height is not None else None,
            part_number.value if part_number is not None else None,
        )
    return consolidated


def _ensure_cluster_type(
    cluster_type_endpoint: Endpoint,
    name: str,
    slug: str,
    cache: dict[Any, NetBoxObject],
    dry_run: bool,
) -> tuple[NetBoxObject, dict[Any, NetBoxObject]]:
    """Ensures that the ClusterType exists in NetBox.
    Returns the tuple (ClusterType, updated cache)"""
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
    Creates the dynamic ClusterTypes in NetBox from the unique
    values of hypervisor_os and stores them in the cache.

    It always ensures the static fallback of the YAML as a backup
    for clusters without OS information.

    Returns a tuple (fallback ClusterType, updated cache).
    """
    # Ensure the static fallback of the YAML.
    fallback_name = fallback_cfg.default.name
    fallback_slug = cast(str, fallback_cfg.default.slug)
    fallback, cache = _ensure_cluster_type(
        endpoints.cluster_types, fallback_name, fallback_slug, cache, dry_run
    )

    # Create dynamic ClusterTypes (one per unique OS).
    # The cache in get_or_create_cached avoids N+1 requests
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
    Synchronizes a single DeviceRole and makes sure it allows VMs.
    Returns a tuple (DeviceRole, updated cache).
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
                "vm_role": role_def.vm_role,
            },
            name=name,
            dry_run=dry_run,
        )
    except NetBoxApiError as e:
        raise ConfigValidationError(f"Error processing DeviceRole '{name}': {e}") from e

    if getattr(obj, "id", 0) != 0:
        current_vm_role = getattr(obj, "vm_role", False)
        if current_vm_role != role_def.vm_role:
            if dry_run:
                log.info(
                    "[DRY-RUN] WOULD UPDATE DeviceRole, vm_role %s -> %s: %s",
                    current_vm_role,
                    role_def.vm_role,
                    name,
                )
            else:
                with netbox_error_wrap(f"Error updating DeviceRole '{name}'"):
                    cast(Record, obj).update({"vm_role": role_def.vm_role})
                    log.info(
                        "UPDATED DeviceRole, vm_role %s -> %s: %s",
                        current_vm_role,
                        role_def.vm_role,
                        name,
                    )

    return obj, device_roles_cache


def ensure_all_device_roles(
    endpoints: NetBoxEndpoints,
    device_roles: list[DeviceRoleConfig],
    device_roles_cache: NameCache,
    dry_run: bool,
) -> tuple[NameCache, NameCache]:
    """
    Ensures that all the device roles defined in the YAML
    exist in NetBox (/api/dcim/device-roles/).
    All the roles are enabled for use in Virtual Machines.
    Returns a tuple (ensured roles, updated cache).
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
    """Ensures that the Platform exists in NetBox.
    Returns the created Platform object and the updated cache."""
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
    """Ensures that the Cluster exists in NetBox.
    Returns the created Cluster object and the updated cache."""
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
    """Ensures that the Manufacturer exists in NetBox.
    Returns the created Manufacturer object and the updated cache."""
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


def _sync_device_type_attributes(
    existing_dt: Record,
    model: str,
    attributes: DeviceTypeAttributes,
    dry_run: bool,
) -> bool:
    """
    Synchronizes the attributes of the DeviceType (u_height and part_number).

    Only the reported attributes (`not None`) are updated: an empty value in the
    CSV never erases or degrades what NetBox already has (e.g. a P/N loaded by hand or
    the 0U height of a child type).
    Returns True if the object was (or would have been) updated, False otherwise.
    """
    current_raw_h = getattr(existing_dt, "u_height", None)
    current_height = float(current_raw_h) if current_raw_h is not None else None
    current_pn = getattr(existing_dt, "part_number", "") or ""

    updates: NetBoxPayload = {}
    if attributes.u_height is not None and current_height != attributes.u_height:
        updates["u_height"] = attributes.u_height
    if attributes.part_number is not None and current_pn != attributes.part_number:
        updates["part_number"] = attributes.part_number

    if not updates:
        return False

    return apply_update(existing_dt, updates, f"DeviceType '{model}'", dry_run)


def ensure_device_type(
    device_types_endpoint: Endpoint,
    manufacturer: NetBoxObject,
    model: str,
    attributes: DeviceTypeAttributes,
    cache: ManufModelCache,
    dry_run: bool,
) -> tuple[NetBoxObject, ManufModelCache]:
    """
    Ensures that the DeviceType exists in NetBox with the consolidated attributes of the model.

    Design decisions:
    - The attributes are synchronized only once per model and run (when resolved
      for the first time); cache hits do not write the type again.
    - On creation, a non-reported value uses the default (height 1.0U, empty P/N).
    - If NetBox rejects the update (e.g. going to 0U with devices mounted in racks,
      or extending the height without space), a warning is issued and the current type is kept:
      the DeviceType exists and is valid, so it must not block the synchronization of the node.

    Returns the DeviceType object and the updated cache.
    """
    manufacturer_id = get_netbox_object_id(manufacturer)

    # The numeric ID is kept only as a fallback.
    manufacturer_name = str(getattr(manufacturer, "name", manufacturer_id))
    key = (manufacturer_name, model)
    already_resolved = key in cache

    if attributes.u_height is None and not already_resolved:
        log.warning(
            "Empty height (U) for the model '%s / %s': if the DeviceType does not exist it "
            "will be created with %.1fU by default; if it already exists, its current height is kept.",
            manufacturer_name,
            model,
            DEFAULT_DEVICE_TYPE_U_HEIGHT,
        )
    u_height = (
        attributes.u_height
        if attributes.u_height is not None
        else DEFAULT_DEVICE_TYPE_U_HEIGHT
    )

    slug = slugify(f"{manufacturer_name} {model}")

    obj, cache = get_or_create_cached(
        endpoint=device_types_endpoint,
        cache=cache,
        cache_key=key,
        filter_kwargs={"model": model, "manufacturer_id": manufacturer_id},
        create_kwargs={
            "model": model,
            "part_number": attributes.part_number or "",
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

    if already_resolved or getattr(obj, "id", 0) == 0:
        return obj, cache

    try:
        _sync_device_type_attributes(cast(Record, obj), model, attributes, dry_run)
    except NetBoxApiError as e:
        log.warning(
            "NetBox refused to update the DeviceType '%s / %s'; the current type is "
            "kept: %s",
            manufacturer_name,
            model,
            e,
        )
    return obj, cache


def ensure_location(
    locations_endpoint: Endpoint,
    name: str,
    site: NetBoxObject,
    cache: SiteNameCache,
    dry_run: bool,
) -> tuple[NetBoxObject, SiteNameCache]:
    """Ensures that the Location (Row) exists in NetBox.
    Returns the created Location object and the updated cache."""
    site_id = get_netbox_object_id(site)
    cache_key = SiteScopedName(site_id, name)

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
    Synchronizes the location (row) of the rack.
    Updates the object in-place if necessary.
    Returns True if the object was (or would have been) updated, False otherwise.
    """
    # check_record_changes resolves the relational reference (Record or id) of 'location'
    _, changed = update_if_changed(
        existing_rack,
        {"location": target_location_id},
        f"rack '{name}'",
        dry_run,
    )
    return changed


def ensure_rack(
    racks_endpoint: Endpoint,
    name: str,
    site: NetBoxObject,
    cache: SiteNameCache,
    dry_run: bool,
    location_id: int | None = None,
) -> tuple[NetBoxObject, SiteNameCache]:
    """Ensures that the Rack exists in NetBox and is associated with the given Location.
    Returns the created/updated Rack object and the updated cache."""
    site_id = get_netbox_object_id(site)
    cache_key = SiteScopedName(site_id, name)

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

    # If it already existed, we make sure it has the right location (Idempotency)
    if getattr(obj, "id", 0) != 0 and location_id is not None:
        _sync_rack_location(cast(Record, obj), name, location_id, dry_run)

    return obj, cache


def ensure_prefix(
    prefixes_endpoint: Endpoint,
    network: str,
    dry_run: bool,
) -> tuple[NetBoxObject, bool]:
    """
    Ensures that the Prefix (network) exists in NetBox.

    Design decisions:
    - NetBox does not assign an IP to a Prefix explicitly: membership is by
      address containment. It is enough for the Prefix to exist for the IP to fall
      within it (the containment validation is done when parsing the CSV).
    - Idempotent: if the Prefix already exists, it is not modified.
    - It does not use `get_or_create_cached` on purpose: that function does not tell whether the
      object was created or already existed, and here that boolean feeds the synchronization
      status of the interface (`any_changes`). Deriving it from the return value would require
      changing the contract of `get_or_create_cached` and of all its consumers. Besides, its
      slug-based mechanisms (preventive search, fallback slug) do not apply: the Prefix
      has no slug. Instead it reuses the `_create_record` primitive.

    Returns:
        tuple[NetBoxObject, bool]: The Prefix and a boolean indicating whether there were changes.
    """
    existing: list[Record] = list(prefixes_endpoint.filter(prefix=network))
    if existing:
        return existing[0], False

    obj = _create_record(
        prefixes_endpoint,
        {"prefix": network, "status": "active"},
        network,
        dry_run,
    )
    return obj, True


# ============================================================
# PAYLOAD CONSTRUCTION
# ============================================================


def _resolve_default_or_empty(
    default: FieldValue,
    is_optional: bool,
    target: str,
) -> FieldValue:
    """Centralizes the fallback policy (default > None > error)."""
    if default is not None:
        return default
    if is_optional:
        return None
    raise RowValidationError(f"The mandatory field '{target}' is empty in the CSV.")


def _extract_concat_dot_value(
    row: CsvRow,
    source: list[str],
    config: NetBoxMappingConfig,
) -> str:
    """Resolves a multi-column field via dot concatenation.

    Returns the concatenation of the values of the source columns.
    If all the source columns are empty, it returns an empty string.
    """
    parts = [row.get(s, "") for s in source]
    return concat_dot(parts, config)


def _extract_coalesce_value(
    row: CsvRow,
    source: list[str],
    config: NetBoxMappingConfig,
) -> str:
    """
    Resolves a multi-column field through coalesce.
    It fails fast if more than one field contains a value.
    Returns the first value found, or an empty string if none has a value.
    """
    values = []
    for s in source:
        val = row.get(s, "").strip()
        if not config.is_empty(val):
            values.append(val)

    # Remove duplicates to avoid a false conflict if both columns have the exact same value
    unique_values = list(dict.fromkeys(values))

    if len(unique_values) > 1:
        raise RowValidationError(
            f"Conflict: Multiple distinct non-null values {unique_values} "
            f"for a coalesce field coming from the columns {source}."
        )

    return unique_values[0] if unique_values else ""


def _extract_product_value(
    row: CsvRow,
    source: list[str],
    config: NetBoxMappingConfig,
) -> str:
    """
    Resolves a multi-column field as the numeric product of its sources.

    It allows deriving totals from "per socket" columns without duplicating the
    data in the inventory (e.g. vcpus = Threads (per socket) x Sockets).
    If any source is empty no partial product is emitted, because it would be
    an incorrect total: it returns an empty string to delegate to the
    defaults/optionals policy. Raises RowValidationError if any source is not
    numeric.
    """
    values = [row.get(s, "").strip() for s in source]
    empty_sources = [
        s for s, val in zip(source, values, strict=True) if config.is_empty(val)
    ]

    if len(empty_sources) == len(source):
        return ""

    if empty_sources:
        log.warning(
            "Incomplete product: empty columns %s of %s. The field is skipped.",
            empty_sources,
            source,
        )
        return ""

    try:
        product = math.prod(parse_float(val) for val in values)
    except ValueError as e:
        raise RowValidationError(
            f"Invalid values {values} for a product field "
            f"coming from the columns {source}. {e}"
        ) from e

    return str(int(product)) if product.is_integer() else str(product)


def _extract_raw_source_value(row: CsvRow, source: str | list[str]) -> str:
    """Extracts the raw value of the source column(s), untransformed."""
    if isinstance(source, list):
        raise TypeError(
            "A raw value cannot be extracted from a list of sources "
            "without using an explicit transformation function."
        )
    return row.get(source, "")


def _validate_select_choice(
    value: FieldValue,
    custom_field_def: CustomFieldConfig | None,
    target: str,
    is_optional: bool,
) -> FieldValue:
    """
    Validates value against choice_set when the Custom Field is of type
    'select' or 'multiselect'. No-op for any other field type.
    Returns the transformed value or raises RowValidationError if it fails.
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

    # Unified handling of fallbacks and errors
    msg_val = (
        f"Values {invalid_val} are not valid"
        if is_multi
        else f"Value '{invalid_val}' is not valid"
    )
    msg_type = " (multiselect)" if is_multi else ""

    log.warning(
        "%s for the Custom Field%s '%s'. Valid options: %s.",
        msg_val,
        msg_type,
        target,
        valid_choices,
    )

    if is_optional:
        return None

    err_prefix = "Invalid values" if is_multi else "Invalid value"
    raise RowValidationError(
        f"{err_prefix} '{invalid_val}' for the required field '{target}'."
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
    Interprets a `FieldMappingConfig` over a CSV row to return the final value.
    It handles concatenations and simple fields. It applies the "Pipe and Filter" pattern,
    delegating each one to a single-responsibility function; it cuts
    early (fail-fast) as soon as a stage determines the final value.
    Returns the transformed value or raises RowValidationError if it fails.
    """
    source = field_def.source
    target = field_def.target

    # 1. Raw Extraction or Transformation
    if isinstance(source, list) and field_def.transform == "concat_dot":
        raw_value = _extract_concat_dot_value(row, source, config)
    elif isinstance(source, list) and field_def.transform == "coalesce":
        raw_value = _extract_coalesce_value(row, source, config)
    elif isinstance(source, list) and field_def.transform == "product":
        raw_value = _extract_product_value(row, source, config)
    else:
        raw_value = _extract_raw_source_value(row, source)

    # 2. Resolution of Empties and Defaults (Centralized)
    if config.is_empty(raw_value):
        return _resolve_default_or_empty(default, is_optional, target)

    # 3. Processing of simple fields (transformed ones skip the dictionary mapping)
    value: FieldValue = raw_value
    if isinstance(source, str):
        value = config.map_value_by_source(source, raw_value, strict=True)

    # 4. Validations and Casts (always applied at the end of the pipeline)
    value = _validate_select_choice(value, custom_field_def, target, is_optional)
    if field_def.cast:
        value = apply_cast(value, field_def.cast, target)

    if value != raw_value:
        log.debug("Mapping %s: '%s' cast to -> '%s'", target, raw_value, value)

    return value


def _assign_if_valid(
    target_payload: dict[str, Any], fd: FieldMappingConfig, val: FieldValue
) -> dict[str, Any]:
    """Sanitizes and assigns the value to the payload only if it is valid.
    Returns the updated payload."""
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
    """Processes a list of mappings and populates the target payload.
    Returns the updated payload."""
    for fd in maps:
        cf_def = None
        default = None
        is_optional = not fd.required

        if is_custom:
            cf_def = config.get_custom_field_def(fd.target)
            if cf_def is None:
                raise ConfigValidationError(
                    f"Critical configuration error: The custom_mapping target "
                    f"'{fd.target}' is not defined in custom_field_definitions."
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
    Dynamically builds the payload to send to the NetBox API,
    based strictly on the mapping rules (DML) defined in the YAML file.

    This function is the core of the script's dynamism, since it abstracts the logic of
    extraction, value mapping, casts and validations, isolating the Python code
    from the business rules specific to the CSV.

    - The `native_maps` are injected at the first level (root) of the payload dictionary.
    - The `custom_maps` are grouped and packed within the "custom_fields" key.
    - The resulting fields with a `None` value (e.g. empty cells where `is_optional=True`)
      are proactively excluded from the payload to avoid erasing pre-existing data
      or violating constraints in NetBox.

    Returns the transformed payload.
    """
    payload: NetBoxPayload = {}
    cf_payload: CustomFieldsPayload = {}

    payload = _process_maps(config, row, native_maps, payload, is_custom=False)
    cf_payload = _process_maps(config, row, custom_maps, cf_payload, is_custom=True)

    if cf_payload:
        payload["custom_fields"] = cf_payload

    log.debug("Payload built: %s", payload)
    return payload


# ============================================================
# GENERAL RESOLVERS (FOR OBJECT SYNCHRONIZATION)
# ============================================================


def _resolve_platform(
    plt_endpoint: Endpoint,
    row: CsvRow,
    plt_cache: NameCache,
    dry_run: bool,
    config: NetBoxMappingConfig,
) -> tuple[int | None, NameCache]:
    """Resolves the Platform from the OS column.
    Returns a tuple with the ID of the Platform and the updated Platform cache."""
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
    Looks up a DeviceRole by name (case-insensitive) and returns its ID.
    If the name is empty or does not exist, it uses "Others" as a fallback.
    Raises RowValidationError if the "Others" fallback does not exist either.
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
            f"The DeviceRole 'Others' does not exist in NetBox to assign as a "
            f"fallback to the machine '{machine_name}'."
        )

    return get_netbox_object_id(role_obj)


def _resolve_netbox_status(
    row: CsvRow, config: NetBoxMappingConfig, node_type: NodeType
) -> str:
    """
    Resolves the NetBox status from the 'Status' column.

    If the value does not exist in status_map, the fallback
    corresponding to the node type is applied.

    Returns the NetBox status.
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
    Resolves the Cluster from the corresponding column.

    It determines the right ClusterType using the precomputed map
    (cluster_name → hypervisor_os → ClusterType). If the cluster does not
    have an associated technology, it uses the generic fallback of the YAML.

    Returns a tuple with the ID of the Cluster and the updated Cluster cache.
    """
    cluster_name = extract_csv_value(row, "cluster_name", config)
    if not cluster_name:
        return None, cluster_cache

    # Resolve the right ClusterType for this cluster.
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


def select_device_type_attributes(
    row: CsvRow,
    key: DeviceTypeKey,
    device_type_attrs: DeviceTypeAttrsMap,
    config: NetBoxMappingConfig,
) -> DeviceTypeAttributes:
    """
    Validates the DeviceType attributes of the row and picks the ones to be synchronized.

    The attributes of the row are always validated (an invalid value is an error of
    the row), but the consolidated ones of the model (`device_type_attrs`) are used, so
    that all the rows of the same model match.
    Raises RowValidationError if any value of the row is invalid.
    """
    row_attributes = parse_device_type_attributes(row, config)
    return device_type_attrs.get(key, row_attributes)


def _resolve_device_type(
    endpoints: NetBoxEndpoints,
    key: DeviceTypeKey,
    attributes: DeviceTypeAttributes,
    caches: CacheStore,
    dry_run: bool,
) -> tuple[int, CacheStore]:
    """
    Ensures the Manufacturer and the DeviceType of the model, with already validated attributes.
    Returns a tuple with the ID of the DeviceType and the updated cache (CacheStore).
    """
    manufacturer_obj, caches.manufacturers = ensure_manufacturer(
        endpoints.manufacturers,
        key.manufacturer,
        caches.manufacturers,
        dry_run,
    )

    device_type, caches.device_types = ensure_device_type(
        endpoints.device_types,
        manufacturer_obj,
        key.model,
        attributes,
        caches.device_types,
        dry_run,
    )
    return get_netbox_object_id(device_type), caches


def _resolve_host_device(
    devices_endpoint: Endpoint,
    row: CsvRow,
    site: NetBoxObject,
    machine_name: str,
    cache: HostDeviceCache,
    config: NetBoxMappingConfig,
) -> tuple[int | None, HostDeviceCache]:
    """
    Resolves and caches the ID of the Device corresponding to the host hypervisor,
    strictly scoped to the configured site.
    Returns a tuple with the ID of the host (or None) and the updated cache.
    """
    host_name_csv = extract_csv_value(row, "host_device", config)
    if not host_name_csv:
        return None, cache

    site_id = get_netbox_object_id(site)
    cache_key = SiteScopedName(site_id, host_name_csv)

    # Only look it up if it does not exist in the cache.
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
                f"ERROR ({machine_name}): the lookup of the host_device '{host_name_csv}' "
                f"in Site (ID: {site_id}) failed: {e}"
            ) from e

    dev_id = cache[cache_key]

    if dev_id is None:
        log.warning(
            "The host device '%s' of the VM '%s' was not found in the site: "
            "the VM will be created without a host assignment.",
            host_name_csv,
            machine_name,
        )

    return dev_id, cache


# ============================================================
# OBJECT SYNCHRONIZATION (DEVICES and VMS)
# ============================================================


def _prepare_base_node(
    node_type: NodeType,
    row: CsvRow,
    config: NetBoxMappingConfig,
    site: NetBoxObject,
    device_roles_cache: NameCache,
    native_maps: list[FieldMappingConfig],
    custom_maps: list[FieldMappingConfig],
) -> BaseNodeData:
    """
    Validates the row and prepares the fields common to device and virtual_machine.

    It does not write to NetBox: it only extracts, maps and validates (casts, maps, choices,
    role and status). This way invalid data aborts the row before creating dependencies.
    Raises RowValidationError if any value is invalid.
    """
    machine_name = extract_csv_value(row, "machine_name", config, strict_extract=True)
    uuid = extract_csv_value(row, "inventory_uuid", config).lower()
    machine_type = extract_csv_value(row, "machine_type", config, strict_extract=True)

    payload = build_payload(row, native_maps, custom_maps, config)
    payload["role"] = _resolve_device_role(row, device_roles_cache, config)
    payload["status"] = _resolve_netbox_status(row, config, node_type)
    payload["site"] = get_netbox_object_id(site)

    return {
        "machine_name": machine_name,
        "inventory_uuid": uuid,
        "machine_type": machine_type,
        "payload": payload,
    }


def _resolve_common_dependencies(
    endpoints: NetBoxEndpoints,
    row: CsvRow,
    payload: NetBoxPayload,
    caches: CacheStore,
    config: NetBoxMappingConfig,
    dry_run: bool,
) -> tuple[NetBoxPayload, CacheStore]:
    """
    Ensures the dependencies common to Devices and VMs (Platform) and adds them
    to the payload. Returns a copy of the payload and the updated cache.
    """
    platform_id, caches.platforms = _resolve_platform(
        endpoints.platforms, row, caches.platforms, dry_run, config
    )
    if platform_id is None:
        return payload, caches
    return {**payload, "platform": platform_id}, caches


def _extract_object_uuid(obj: NetBoxObject) -> str:
    """Gets the normalized `inventory_uuid` of a NetBox object ("" if it has none)."""
    custom_fields: dict[str, Any] = getattr(obj, "custom_fields", {}) or {}
    return str(custom_fields.get("inventory_uuid") or "").strip().lower()


def _find_by_uuid(endpoint: Endpoint, uuid: str) -> list[Record]:
    """
    Looks up objects by `inventory_uuid` with an exact match.

    The NetBox Custom Fields filter is partial (`loose`) by default, so
    the result is filtered again locally to discard UUIDs that only
    contain the searched value.
    """
    candidates: list[Record] = list(endpoint.filter(cf_inventory_uuid=uuid))
    return [obj for obj in candidates if _extract_object_uuid(obj) == uuid]


def _is_identifier_unique(
    identifier_label: str,
    identifier: str,
    node_type: NodeType,
    nb_count: int,
    csv_count: int,
    warn_when_absent: bool,
    consequence: str,
) -> bool:
    """
    Common uniqueness rule for a node identifier (UUID or name): it must
    appear exactly once in NetBox and once in the CSV.

    `warn_when_absent` tells whether the absence in NetBox is expected (UUID of a
    new node: no warning) or already constitutes a failure (match by name).
    `consequence` describes what will happen to the row when it is not unique.
    """
    if nb_count == 1 and csv_count == 1:
        return True
    if warn_when_absent or nb_count > 1 or csv_count > 1:
        log.warning(
            "%s '%s' of %s is not unique (NetBox=%d, CSV=%d). %s",
            identifier_label,
            identifier,
            node_type,
            nb_count,
            csv_count,
            consequence,
        )
    return False


def _is_uuid_safely_unique(
    endpoint: Endpoint,
    existing: list[Record],
    uuid: str,
    csv_uuid_counts: Counter[str],
) -> bool:
    """
    Validates that a UUID identifies exactly one node both in NetBox and in the CSV,
    a necessary condition to update by UUID without clobbering another node.

    Returns True if the UUID appears only once in both systems. It only warns if
    there is real ambiguity (not when the node simply does not exist in NetBox yet).
    """
    return _is_identifier_unique(
        "UUID",
        uuid,
        get_node_type_from_object(endpoint),
        len(existing),
        csv_uuid_counts.get(uuid, 0),
        warn_when_absent=False,
        consequence="Resolution by name will be attempted.",
    )


def _is_name_safely_unique(
    endpoint: Endpoint,
    existing: list[Record],
    machine_name: str,
    csv_name_counts: Counter[str],
) -> bool:
    """
    Validates that a machine name is unique both in NetBox
    and in the CSV, a necessary condition to allow a
    safe update when the match is only by name.

    Returns True if the name is unique in both systems.
    """
    return _is_identifier_unique(
        "Name",
        machine_name,
        get_node_type_from_object(endpoint),
        len(existing),
        csv_name_counts.get(machine_name, 0),
        warn_when_absent=True,
        consequence="SKIP: a unique UUID is required to synchronize.",
    )


def _get_object_site_id(obj: NetBoxObject) -> int | None:
    """Gets the ID of the Site of a NetBox object (None if it has no Site assigned)."""
    site = getattr(obj, "site", None)
    site_id = getattr(site, "id", None)
    return int(site_id) if site_id is not None else None


def _resolve_by_uuid(
    endpoint: Endpoint,
    uuid: str,
    csv_uuid_counts: Counter[str],
    config: NetBoxMappingConfig,
    site_id: int,
) -> list[Record]:
    """
    Stage 1 of the identity resolution: lookup by UUID with uniqueness validation.

    Design decisions:
    - The UUID is a global identity, so it is searched across all Sites. If the
      node exists but belongs to another Site, updating it would move it (and clobber
      data outside the scope of the pipeline) and creating it would duplicate its UUID: the
      row is skipped for human review.

    Returns the object (in a single-element list) if the UUID is unique in NetBox and in the
    CSV; an empty list if it could not be resolved this way.
    Raises RowSkipCondition if the node belongs to another Site.
    """
    if config.is_empty(uuid):
        return []

    existing = _find_by_uuid(endpoint, uuid)
    if not _is_uuid_safely_unique(endpoint, existing, uuid, csv_uuid_counts):
        return []

    object_site_id = _get_object_site_id(existing[0])
    if object_site_id is not None and object_site_id != site_id:
        log.warning(
            "SKIP: the UUID '%s' already exists in a node of Site ID=%d, different from the "
            "configured Site (ID=%d). It is neither modified nor duplicated.",
            uuid,
            object_site_id,
            site_id,
        )
        raise RowSkipCondition("The UUID belongs to a node of another Site.")
    return existing


def _resolve_by_name(
    endpoint: Endpoint,
    machine_name: str,
    uuid: str,
    csv_name_counts: Counter[str],
    config: NetBoxMappingConfig,
    site_id: int,
) -> list[Record]:
    """
    Stage 2 of the identity resolution: lookup by name with uniqueness validation.

    Design decisions:
    - The search is scoped to the configured Site: names are only unique within
      a Site in NetBox, so a namesake from another Site is not the same node and
      must never be updated. A Site that does not exist yet (ID 0, dry-run) has no nodes.
    - An object found by name whose UUID matches (case-insensitively) is
      accepted: it covers UUIDs stored with another capitalization and duplicate UUIDs whose
      rows were already separated by name in a previous run.
    - An object with a different, non-empty UUID is not updated: overwriting it
      would impersonate the identity of another node and requires human review.

    Returns an empty list if it does not exist (it must be created) or the object to update.
    Raises RowSkipCondition if a safe match cannot be guaranteed.
    """
    if site_id == 0:
        return []

    existing: list[Record] = list(endpoint.filter(name=machine_name, site_id=site_id))
    if not existing:
        return []

    has_uuid = not config.is_empty(uuid)
    if has_uuid:
        uuid_matches = [obj for obj in existing if _extract_object_uuid(obj) == uuid]
        if len(uuid_matches) == 1:
            return uuid_matches

    if not _is_name_safely_unique(endpoint, existing, machine_name, csv_name_counts):
        raise RowSkipCondition("Uniqueness by name cannot be guaranteed.")

    existing_uuid = _extract_object_uuid(existing[0])
    if has_uuid and existing_uuid and existing_uuid != uuid:
        log.warning(
            "SKIP '%s': it exists by name but with another UUID (NetBox=%s, CSV=%s).",
            machine_name,
            existing_uuid,
            uuid,
        )
        raise RowSkipCondition("The name belongs to a node with a different UUID.")

    return existing


def _resolve_existing_object(
    endpoint: Endpoint,
    machine_name: str,
    uuid: str,
    csv_counts: CsvIdentityCounts,
    config: NetBoxMappingConfig,
    site_id: int,
) -> list[Record]:
    """
    Resolves the existing object in NetBox in two stages: unique UUID and, if it is
    not resolved, unique name within the configured Site.

    Design decisions:
    - Blindly updating on an ambiguous identifier would cause cross data corruption
      between nodes; the pipeline never modifies nodes of another Site.
    - It only reads NetBox: it runs before creating dependencies, so that a row that
      ends in SKIP leaves no writes.

    Returns an empty list if the node must be created, or the object to update.
    Raises RowSkipCondition if no stage can guarantee a safe match.
    """
    by_uuid = _resolve_by_uuid(endpoint, uuid, csv_counts.uuids, config, site_id)
    if by_uuid:
        return by_uuid

    return _resolve_by_name(
        endpoint, machine_name, uuid, csv_counts.names, config, site_id
    )


def _execute_sync(
    endpoint: Endpoint,
    payload: NetBoxPayload,
    existing: list[Record],
    machine_name: str,
    raw_uuid: str,
    dry_run: bool,
) -> SyncResult:
    """
    Executes the final synchronization operation (CREATE, UPDATE, UNCHANGED) against NetBox.

    Design decisions:
    - Delegated trust: If a row reaches this function with the `existing` list populated,
      we assume it already passed the strict prior uniqueness validation.
    - Idempotency and Optimization: Before issuing an UPDATE to the API, we evaluate
      the differences (diff) locally. If there are no real changes, it is classified
      as UNCHANGED to save bandwidth and processing time.

    Returns:
        SyncResult: Structured result with the status (CREATED/UPDATED/UNCHANGED),
        the ID of the final object in NetBox, and the instance of the object (mock in the
        case of creations under dry-run).
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

        with netbox_error_wrap(f"Error creating {node_type} '{machine_name}'"):
            obj = cast(Record, endpoint.create(**payload))
        obj_id = get_netbox_object_id(obj)
        log.info("CREATED %s: %s (ID=%d)", node_type, machine_name, obj_id)
        return SyncResult(SyncStatus.CREATED, obj_id, obj)

    existing_obj = existing[0]
    existing_id = get_netbox_object_id(existing_obj)

    # Evaluation of differences to optimize network usage
    _, changed = update_if_changed(
        existing_obj,
        payload,
        f"{node_type} '{machine_name}' (UUID={uuid})",
        dry_run,
    )
    if changed:
        return SyncResult(SyncStatus.UPDATED, existing_id, existing_obj)

    prefix = "[DRY-RUN] " if dry_run else ""
    log.info(
        "%sUNCHANGED %s: %s (UUID=%s)",
        prefix,
        node_type,
        machine_name,
        uuid,
    )
    return SyncResult(SyncStatus.UNCHANGED, existing_id, existing_obj)


def sync_device(
    endpoints: NetBoxEndpoints,
    row: CsvRow,
    config: NetBoxMappingConfig,
    site: NetBoxObject,
    cluster_type_map: dict[str, str],
    device_type_attrs: DeviceTypeAttrsMap,
    fallback_cluster_type: NetBoxObject,
    caches: CacheStore,
    csv_counts: CsvIdentityCounts,
    dry_run: bool,
) -> tuple[SyncResult, CacheStore]:
    """
    Orchestrates the full synchronization of a row classified as Device or Hypervisor.

    Design decisions:
    - Three phases: (1) validate the row without writing, (2) resolve its identity (read
      only) and (3) write dependencies and node. This way a row that ends in SKIP or
      in a data ERROR leaves no Platform, Location, Rack or DeviceType written.
    - Strict requirements (Fail-Fast): 'manufacturer' and 'model' are mandatory fields
      in the NetBox hierarchy; it aborts immediately if they are missing.
    - API constraints: NetBox requires that if a device has a 'position' (U-Location),
      it must also have a 'face' (front/rear). It is injected preventively.
    - Hypervisors: The physical devices that act as hypervisors must be assigned
      to an explicit Cluster in order to host Virtual Machines later.

    Returns:
        tuple[SyncResult, CacheStore]: The final result and the purely injected cache state.
    """
    # ── PHASE 1: VALIDATION (no writes) ──────────────────────
    manufacturer = extract_csv_value(row, "manufacturer", config)
    model = extract_csv_value(row, "model", config)
    if not manufacturer or not model:
        raise RowSkipCondition(
            "Missing 'manufacturer' or 'model'. Required for Device."
        )

    node_cfg = config.node_types.get_config(NodeType.DEVICE)
    base = _prepare_base_node(
        NodeType.DEVICE,
        row,
        config,
        site,
        caches.device_roles,
        node_cfg.native_mappings,
        node_cfg.custom_mappings,
    )
    machine_name = base["machine_name"]
    uuid = base["inventory_uuid"]
    machine_type = base["machine_type"]
    payload = base["payload"]

    # NetBox API compensation: 'face' is mandatory if 'position' exists.
    if payload.get("position") is not None and "face" not in payload:
        payload["face"] = "front"

    device_type_key = DeviceTypeKey(manufacturer, model)
    device_type_attributes = select_device_type_attributes(
        row, device_type_key, device_type_attrs, config
    )

    # ── PHASE 2: IDENTITY (read only) ────────────────────────
    existing = _resolve_existing_object(
        endpoints.devices,
        machine_name,
        uuid,
        csv_counts,
        config,
        get_netbox_object_id(site),
    )

    # ── PHASE 3: WRITES (dependencies and node) ──────────────
    payload, caches = _resolve_common_dependencies(
        endpoints, row, payload, caches, config, dry_run
    )

    # Cluster for hypervisors.
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
            log.info(
                "Hypervisor '%s' without an assigned cluster: the column '%s' is empty.",
                machine_name,
                config.csv_columns["cluster_name"].source,
            )

    # Location (Row).
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

    # DeviceType (and its Manufacturer).
    device_type_id, caches = _resolve_device_type(
        endpoints, device_type_key, device_type_attributes, caches, dry_run
    )
    payload["device_type"] = device_type_id

    # ── CREATE / UPDATE / UNCHANGED ──────────────────────────
    sync_res = _execute_sync(
        endpoints.devices, payload, existing, machine_name, uuid, dry_run
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
    csv_counts: CsvIdentityCounts,
    dry_run: bool,
) -> tuple[SyncResult, CacheStore]:
    """
    Orchestrates the full synchronization of a row classified as Virtual Machine.

    Design decisions:
    - Three phases, as in `sync_device`: validate without writing, resolve identity
      (read only) and write dependencies and node.
    - Strict requirements (Fail-Fast): In NetBox, every VM MUST belong to a Cluster.
      The record is discarded immediately if 'cluster_name' is absent.
    - Host Device (Optional): An attempt is made to associate the VM with the specified
      physical hypervisor (Device). This allows mapping the virtualized topology onto the real hardware.

    Returns:
        tuple[SyncResult, CacheStore]: The final result and the purely injected cache state.
    """
    # ── PHASE 1: VALIDATION (no writes) ──────────────────────
    cluster_name = extract_csv_value(row, "cluster_name", config)
    if not cluster_name:
        raise RowSkipCondition("Missing 'cluster_name'. Required for Virtual Machine.")

    node_cfg = config.node_types.get_config(NodeType.VIRTUAL_MACHINE)
    base = _prepare_base_node(
        NodeType.VIRTUAL_MACHINE,
        row,
        config,
        site,
        caches.device_roles,
        node_cfg.native_mappings,
        node_cfg.custom_mappings,
    )
    machine_name = base["machine_name"]
    uuid = base["inventory_uuid"]
    payload = base["payload"]

    # ── PHASE 2: IDENTITY (read only) ────────────────────────
    existing = _resolve_existing_object(
        endpoints.virtual_machines,
        machine_name,
        uuid,
        csv_counts,
        config,
        get_netbox_object_id(site),
    )

    # ── PHASE 3: WRITES (dependencies and node) ──────────────
    payload, caches = _resolve_common_dependencies(
        endpoints, row, payload, caches, config, dry_run
    )

    # Cluster (guaranteed non-empty by the phase 1 validation).
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
    payload["cluster"] = cluster_id

    # Device of the host hypervisor (scoped to site and cached; read only).
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

    # ── CREATE / UPDATE / UNCHANGED ──────────────────────────
    sync_res = _execute_sync(
        endpoints.virtual_machines, payload, existing, machine_name, uuid, dry_run
    )
    return sync_res, caches


# ============================================================
# PARSING OF INTERFACES and IPs
# ============================================================


def _validate_interface_ip(ip_raw: str, name: str) -> str:
    """Validates that a string is a correct IP address."""
    try:
        ipaddress.ip_address(ip_raw)
        return ip_raw
    except ValueError as e:
        raise FieldParseError(
            f"IP '{ip_raw}' on interface '{name}' is not a valid IP address."
        ) from e


def _split_ip_and_embedded_prefix(ip_raw: str) -> tuple[str, str | None]:
    """
    Splits an IP from its embedded prefix (CIDR notation, e.g. '10.0.0.5/24').
    Returns (ip, prefix) or (ip, None) if the IP does not include a prefix.
    """
    address, separator, prefix = ip_raw.partition("/")
    if not separator:
        return address.strip(), None
    return address.strip(), prefix.strip() or None


def _build_interface_cidr(ip_val: str, pfx_val: str, name: str) -> str:
    """
    Validates IP and 'IP network' by building a valid CIDR address.

    'IP network' accepts a full network ('192.168.1.0/24', '192.168.1.0/255.255.255.0'),
    a prefix alone ('24') or a mask alone ('255.255.255.0'). When the full network is
    given, the IP must belong to it; otherwise FieldParseError is raised.
    The /0 prefix (default route) is rejected, since it is not an interface network.
    """
    try:
        ip_obj = ipaddress.ip_address(ip_val)
        declared_network = (
            ipaddress.ip_network(pfx_val.strip(), strict=False)
            if "/" in pfx_val
            else None
        )
        mask_or_prefix = (
            str(declared_network.prefixlen)
            if declared_network is not None
            else pfx_val.strip()
        )
        interface = ipaddress.ip_interface(f"{ip_obj}/{mask_or_prefix}")
    except ValueError as e:
        raise FieldParseError(
            f"Invalid prefix or CIDR '{pfx_val}' for IP '{ip_val}' on interface '{name}'."
        ) from e

    if declared_network is not None and ip_obj not in declared_network:
        raise FieldParseError(
            f"IP '{ip_val}' does not belong to the 'IP network' '{pfx_val}' on interface '{name}'."
        )
    if interface.network.prefixlen == 0:
        raise FieldParseError(
            f"Prefix /0 is not allowed for IP '{ip_val}' on interface '{name}'."
        )
    return str(interface)


def _ip_host(cidr: str) -> str:
    """Returns the host address of a CIDR, without a mask (e.g. '10.0.0.5/24' → '10.0.0.5')."""
    return str(ipaddress.ip_interface(cidr).ip)


def _network_from_cidr(cidr: str) -> str:
    """Derives the canonical network (e.g. '192.168.1.0/24') from a host CIDR."""
    return str(ipaddress.ip_interface(cidr).network)


def _sanitize_mac_address(mac_raw: str, name: str) -> str:
    """Validates and formats a MAC into its standard form (AA:BB:CC:DD:EE:FF)."""
    cleaned = re.sub(r"[^a-fA-F0-9]", "", mac_raw)

    if len(cleaned) != 12:
        raise FieldParseError(
            f"MAC '{mac_raw}' on interface '{name}' is not valid (expected 12 hex)."
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
    """Parses a single interface, isolating the IP validation logic."""
    if config.is_empty(name):
        raise RowValidationError("The name of a network interface cannot be empty.")

    def get_raw_value_or_none(raw_value: str) -> str | None:
        """
        Filters the raw value.
        Returns the original value if it is not empty, or None otherwise.
        """
        return raw_value if not config.is_empty(raw_value) else None

    enabled = bool(status_map.get(status_raw.lower().strip(), True))
    ip_val = get_raw_value_or_none(ip_raw)
    pfx_val = get_raw_value_or_none(pfx_raw)
    mac_val = get_raw_value_or_none(mac_raw)

    cidr = None
    try:
        if ip_val:
            # The "IP network" column takes precedence over a prefix embedded in "IP".
            ip_val, embedded_prefix = _split_ip_and_embedded_prefix(ip_val)
            ip_val = _validate_interface_ip(ip_val, name)
            effective_prefix = pfx_val or embedded_prefix
            if effective_prefix:
                cidr = _build_interface_cidr(ip_val, effective_prefix, name)
            else:
                log.warning(
                    "IP '%s' on interface '%s' without 'IP network' or prefix: it will not be assigned in NetBox.",
                    ip_val,
                    name,
                )
        elif pfx_val:
            log.warning(
                "'IP network' '%s' on interface '%s' without 'IP': it is discarded (NetBox requires a host).",
                pfx_val,
                name,
            )

        if mac_val:
            mac_val = _sanitize_mac_address(mac_val, name)
    except FieldParseError as e:
        raise RowValidationError(str(e)) from e

    network = _network_from_cidr(cidr) if cidr else None
    return {
        "name": name,
        "enabled": enabled,
        "mac": mac_val,
        "ip": ip_val,
        "prefix": pfx_val,
        "cidr": cidr,
        "network": network,
    }


def _parse_network_interfaces(
    row: CsvRow,
    config: NetBoxMappingConfig,
) -> list[NetworkInterfaceData]:
    """
    Parses the network columns of the CSV and returns a list of interfaces.
    Raises RowValidationError if the arrays have different lengths.

    Validation rules:
      - The "Interfaces" column (names) defines the number of interfaces.
      - The other columns must have exactly the same number of
        comma-separated elements, or be completely empty.
      - A completely empty column means that no interface
        has that data (e.g. all IPs are N/A or blank).
      - If a column has data but its number of elements differs
        from the number of interfaces, the row is discarded for interfaces
        (the Device/VM is synchronized anyway).

    "IP network" column (prefix):
      It contains the network or mask associated with each IP. It is combined with the
      "IP" column to build the CIDR that NetBox requires in its API.

      Supported formats:
        - Network/Prefix: "136.12.34.128/26"          → "26" is extracted
        - Network/Mask:   "192.168.1.0/255.255.255.0" → "255.255.255.0" is extracted
        - Prefix alone:   "26"                        → used directly
        - Mask alone:     "255.255.255.0"             → used directly

      In all cases, the script normalizes to the canonical CIDR format
      before sending it to NetBox. When the full network is given, the IP
      must belong to it (otherwise the interfaces row is
      rejected). From the CIDR the network (Prefix) is also derived, which
      is synchronized in NetBox before assigning the IP.
    """

    def split_col(col_name: str) -> list[str]:
        """
        Splits a comma-separated CSV cell.
        Returns a list of strings with whitespace removed.
        """
        raw = row.get(col_name, "")
        return [v.strip() for v in raw.split(",")] if not config.is_empty(raw) else []

    # Validate names
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
            inconsistencies.append(f"'{col_name}' has {len(lst)} elements")

    if inconsistencies:
        ifaces_col = config.csv_columns["iface_names"].source
        raise RowValidationError(
            f"Network element mismatch: {max_len} interfaces were declared in '{ifaces_col}', "
            f"but " + ", ".join(inconsistencies) + ". Check the commas."
        )

    def fill_if_empty(lst: list[str]) -> list[str]:
        """
        Makes sure the list has the required size if it is empty.
        Returns the original list if it has elements, or a list of empty strings of size max_len.
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
    Structurally parses the interface columns for the current row.

    Design decisions:
    - Gap visibility: If no interfaces are detected, a warning is issued.
      This indicates a possible gap in the structure of the CSV or a device that
      will lose its telemetry (in-band/out-of-band) in NetBox.

    Returns:
        list[NetworkInterfaceData]: List of validated dictionaries with network data.
    """
    interfaces = _parse_network_interfaces(row, config)
    if not interfaces:
        ifaces_col = config.csv_columns["iface_names"].source
        log.warning(
            "SKIP interfaces of '%s': column '%s' is empty or omitted.",
            machine_name,
            ifaces_col,
        )
    return interfaces


# ============================================================
# INTERFACE SYNCHRONIZATION
# ============================================================


def _get_assigned_object_type(node_type: NodeType) -> str:
    """Returns the ContentType associated with the network interface according to the node type."""
    return (
        "dcim.interface"
        if node_type == NodeType.DEVICE
        else "virtualization.vminterface"
    )


def _find_foreign_assignment(records: list[Record]) -> Record | None:
    """Returns the first record assigned to some interface, or None if there is none."""
    return next(
        (r for r in records if getattr(r, "assigned_object_id", None) is not None),
        None,
    )


def _describe_assignment(record: NetBoxObject) -> str:
    """
    Describes which interface and node a record is assigned to (for logs).

    It uses the nested `assigned_object` object that NetBox includes in IPs and MACs; if
    any data is missing, it degrades to the ID so traceability is not lost.
    """
    assigned_object = getattr(record, "assigned_object", None)
    iface_name = getattr(assigned_object, "name", None)
    if iface_name is None:
        return f"object ID={getattr(record, 'assigned_object_id', '?')}"

    parent = getattr(assigned_object, "device", None) or getattr(
        assigned_object, "virtual_machine", None
    )
    parent_name = getattr(parent, "name", None)
    if parent_name is None:
        return f"interface '{iface_name}'"
    return f"interface '{iface_name}' of node '{parent_name}'"


def _assign_network_resource(
    endpoint: Endpoint,
    resource_name: str,
    search_kwargs: dict[str, Any],
    create_kwargs: dict[str, Any],
    value_str: str,
    iface_obj: NetBoxObject,
    dry_run: bool,
    unique_resource: bool = False,
) -> tuple[NetBoxObject, bool]:
    """
    Centralizes the polymorphic assignment of network resources (IP or MAC) to an interface.

    Design decisions:
    - Polymorphic interface: Both IP addresses and MACs in NetBox are assigned
      through ContentTypes (`assigned_object_type`, `assigned_object_id`).
    - Recycling (avoid global duplication): Before creating a new resource, it is checked
      whether a free (orphan) one with the same value already exists, and it is reassigned to the
      interface. This prevents collisions and excessive use of space in the NetBox IPAM.
    - Unique resources (`unique_resource=True`, IPs): NetBox rejects (HTTP 400) creating a
      duplicate of an IP already assigned to another interface, so it is detected with the
      records already queried and `ResourceAssignedElsewhere` is raised instead of creating.
      MACs can be legitimately shared and do not use this rule.

    Returns:
        tuple[NetBoxObject, bool]: The NetBox object (IP/MAC) and a boolean indicating whether there were changes.

    Raises:
        ResourceAssignedElsewhere: if `unique_resource` and the resource occupies another interface.
    """
    node_type = get_node_type_from_object(iface_obj)
    assigned_type = _get_assigned_object_type(node_type)
    existing_records: list[Record] = list(endpoint.filter(**search_kwargs))

    # 1. Check whether it is already assigned to this interface
    for record in existing_records:
        current_id = getattr(record, "assigned_object_id", None)
        current_type = getattr(record, "assigned_object_type", None)
        if current_id == iface_obj.id and str(current_type) == assigned_type:
            return record, False

    # 2. Look for a free one to reassign
    unassigned = next(
        (r for r in existing_records if getattr(r, "assigned_object_id", None) is None),
        None,
    )

    if unassigned:
        if dry_run:
            log.info(
                "[DRY-RUN] WOULD UPDATE free %s %s (assignment to object %s)",
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
        with netbox_error_wrap(f"Error updating free {resource_name} {value_str}"):
            unassigned.update(
                {
                    "assigned_object_type": assigned_type,
                    "assigned_object_id": iface_obj.id,
                }
            )
            log.info("UPDATED free %s reassigned: %s", resource_name, value_str)
            return unassigned, True

    # 3. A unique resource assigned to another interface cannot be duplicated
    if unique_resource:
        foreign_record = _find_foreign_assignment(existing_records)
        if foreign_record is not None:
            raise ResourceAssignedElsewhere(foreign_record)

    # 4. Create a new resource
    if dry_run:
        log.info(
            "[DRY-RUN] WOULD CREATE new %s %s (assigned to object %s)",
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

    with netbox_error_wrap(f"Error creating {resource_name} {value_str}"):
        obj = cast(
            Record,
            endpoint.create(
                assigned_object_type=assigned_type,
                assigned_object_id=iface_obj.id,
                **create_kwargs,
            ),
        )

    log.info("%s CREATED and assigned: %s", resource_name, value_str)
    return obj, True


def _assign_ip(
    ip_addresses_endpoint: Endpoint,
    cidr: str,
    iface_obj: NetBoxObject,
    dry_run: bool,
) -> tuple[NetBoxObject | None, bool]:
    """
    Creates or updates an IP address in NetBox and assigns it to the interface.

    Design decisions:
    - NetBox (ENFORCE_GLOBAL_UNIQUE) does not allow the same IP on two interfaces, regardless
      of their up/down status (e.g. a node and its replica). In that case the IP is
      skipped with a WARNING that identifies the owner and the first processed node wins;
      it is not a synchronization error.

    Returns:
        tuple[NetBoxObject | None, bool]: The IP and whether there were changes; (None, False) if
        the IP belongs to another interface and was skipped.
    """
    try:
        return _assign_network_resource(
            endpoint=ip_addresses_endpoint,
            resource_name="IP",
            search_kwargs={"address": cidr},
            create_kwargs={"address": cidr, "status": "active"},
            value_str=cidr,
            iface_obj=iface_obj,
            dry_run=dry_run,
            unique_resource=True,
        )
    except ResourceAssignedElsewhere as e:
        log.warning(
            "IP %s is already assigned to %s; it is skipped on the interface '%s' "
            "(NetBox does not allow repeating an IP).",
            cidr,
            _describe_assignment(e.record),
            getattr(iface_obj, "name", "?"),
        )
        return None, False


def _assign_mac(
    mac_addresses_endpoint: Endpoint,
    mac_val: str,
    iface_obj: NetBoxObject,
    dry_run: bool,
) -> tuple[NetBoxObject, bool]:
    """Creates or updates a MAC address in NetBox and assigns it to the interface."""
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
    Executes the creation or update logic of an interface.
    Returns a tuple with (Interface, whether there were changes).
    """
    label = f"interface '{name}' (object {obj_id})"
    if not existing_obj:
        return _create_record(iface_endpoint, payload, label, dry_run), True

    return update_if_changed(existing_obj, payload, label, dry_run)


def _sync_single_interface(
    iface_data: NetworkInterfaceData,
    obj_id: int,
    iface_endpoint: Endpoint,
    ifaces_cache: NameCache,
    endpoints: NetBoxEndpoints,
    dry_run: bool,
) -> tuple[SingleInterfaceResult, NameCache]:
    """
    Synchronizes an individual interface and assigns it its IP and MAC.
    Returns a tuple: (SingleInterfaceResult, updated cache).
    """
    name = iface_data["name"]
    payload: NetBoxPayload = {"name": name, "enabled": iface_data["enabled"]}

    if get_node_type_from_object(iface_endpoint) == NodeType.DEVICE:
        payload["device"] = obj_id
        payload["type"] = "other"  # generic type; adjustable
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
    prefix_changed = False
    if cidr := iface_data.get("cidr"):
        if network := iface_data.get("network"):
            _, prefix_changed = ensure_prefix(endpoints.prefixes, network, dry_run)
        ip_obj, ip_changed = _assign_ip(
            endpoints.ip_addresses, cidr, iface_obj, dry_run
        )

    mac_obj = None
    mac_changed = False
    if mac := iface_data.get("mac"):
        mac_obj, mac_changed = _assign_mac(
            endpoints.mac_addresses, mac.upper(), iface_obj, dry_run
        )

    any_changes = iface_changed or prefix_changed or ip_changed or mac_changed
    return SingleInterfaceResult(iface_obj, ip_obj, mac_obj, any_changes), ifaces_cache


def _prune_orphan_interface(
    name: str,
    iface_obj: NetBoxObject,
    csv_iface_names: set[str],
    dry_run: bool,
) -> tuple[int, int]:
    """
    If the name of the interface is not found in the CSV, it is considered
    an orphan and is deleted.

    Returns:
        tuple[int, int]: (number of interfaces deleted, number of errors)
    """
    if name in csv_iface_names:
        return 0, 0
    if dry_run:
        log.info("[DRY-RUN] WOULD DELETE network orphan (interface) '%s'", name)
        return 1, 0
    try:
        cast(Record, iface_obj).delete()
        log.info("DELETED network orphan (interface): %s", name)
        return 1, 0
    except RequestError as e:
        log.error("Error deleting orphan interface '%s': %s", name, e)
        return 0, 1


def _get_ip_role(ip_obj: NetBoxObject) -> str | None:
    """
    Returns the role of a NetBox IP (e.g. 'secondary'), or None if it has no role.

    pynetbox exposes the role as a choice object (`.value`); a plain string is also
    accepted so as not to depend on the exact shape of the response.
    """
    role = getattr(ip_obj, "role", None)
    value = getattr(role, "value", role)
    return str(value) if value else None


def _is_protected_ip(ip_obj: NetBoxObject) -> bool:
    """
    Tells whether an IP must be excluded from the pruning of network orphans.

    The pipeline creates its IPs without a role: an IP with a role (secondary, vip, vrrp, loopback,
    etc.) was curated by hand in NetBox. Since the CSV accepts a single IP per interface,
    those IPs never appear in it and the pruning would unassign them on every run.
    """
    return _get_ip_role(ip_obj) is not None


def _find_protected_ips(
    ip_endpoint: Endpoint, assigned_type: str, iface_id: int
) -> list[str] | None:
    """
    Returns the addresses with an assigned role of an interface, or None if NetBox
    could not be queried (the caller must treat it as "not verifiable").
    """
    try:
        iface_ips = list(
            ip_endpoint.filter(
                assigned_object_type=assigned_type, assigned_object_id=iface_id
            )
        )
    except RequestError as e:
        log.error("Error querying the IPs of the interface ID=%d: %s", iface_id, e)
        return None
    return [
        f"{getattr(ip_obj, 'address', '?')} ({_get_ip_role(ip_obj)})"
        for ip_obj in iface_ips
        if _is_protected_ip(ip_obj)
    ]


def _keep_orphan_interface(
    ip_endpoint: Endpoint, assigned_type: str, name: str, iface_id: int
) -> bool:
    """
    Decides whether an orphan interface must be kept instead of deleted.

    Design decisions:
    - In NetBox, deleting an interface cascade-deletes its IPs: if any of them has a role
      (curated by hand), deleting the interface would destroy precisely what is protected.
    - Fail-safe: if it cannot be verified, the interface is kept (deleting is
      irreversible; keeping it only postpones the pruning to the next run).
    """
    protected = _find_protected_ips(ip_endpoint, assigned_type, iface_id)
    if protected is None:
        log.warning(
            "The orphan interface '%s' is kept: it could not be verified whether it has IPs "
            "with an assigned role.",
            name,
        )
        return True
    if protected:
        log.warning(
            "The orphan interface '%s' is kept: it has IPs with an assigned role %s and "
            "deleting it would cascade-delete them.",
            name,
            protected,
        )
        return True
    return False


def _prune_orphan_ips(
    ip_endpoint: Endpoint,
    assigned_type: str,
    iface_id: int,
    csv_ips: set[str],
    dry_run: bool,
) -> tuple[int, int]:
    """
    Unassigns IPs that are no longer found in the CSV.

    IPs with an assigned role are kept (see `_is_protected_ip`).
    Returns:
        tuple[int, int]: (number of IPs unassigned, number of errors)
    """
    p_ips = errors = 0
    try:
        iface_ips = list(
            ip_endpoint.filter(
                assigned_object_type=assigned_type, assigned_object_id=iface_id
            )
        )
        for ip_obj in iface_ips:
            address = str(getattr(ip_obj, "address", ""))
            if not address or address in csv_ips:
                continue
            if _is_protected_ip(ip_obj):
                log.info(
                    "The IP '%s' (role '%s') is kept: it was assigned manually in "
                    "NetBox and is not unassigned.",
                    address,
                    _get_ip_role(ip_obj),
                )
                continue
            if dry_run:
                log.info(
                    "[DRY-RUN] WOULD UNASSIGN network orphan (IP) '%s'",
                    address,
                )
                p_ips += 1
                continue
            cast(Record, ip_obj).update(
                {"assigned_object_type": None, "assigned_object_id": None}
            )
            log.info("UNASSIGNED network orphan (IP): %s", address)
            p_ips += 1
    except RequestError as e:
        log.error("Error looking up/unassigning orphan IPs: %s", e)
        errors += 1
    return p_ips, errors


def _prune_orphan_macs(
    mac_endpoint: Endpoint,
    assigned_type: str,
    iface_id: int,
    csv_macs: set[str],
    dry_run: bool,
) -> tuple[int, int]:
    """
    Deletes MACs that are no longer found in the CSV.
    Returns:
        tuple[int, int]: (number of MACs deleted, number of errors)
    """
    p_macs = errors = 0
    try:
        iface_macs = list(
            mac_endpoint.filter(
                assigned_object_type=assigned_type, assigned_object_id=iface_id
            )
        )
        for mac_obj in iface_macs:
            mac_addr = str(getattr(mac_obj, "mac_address", "")).upper()
            if not mac_addr or mac_addr in csv_macs:
                continue
            if dry_run:
                log.info(
                    "[DRY-RUN] WOULD DELETE network orphan (MAC) '%s'",
                    mac_addr,
                )
                p_macs += 1
                continue
            cast(Record, mac_obj).delete()
            log.info("DELETED network orphan (MAC): %s", mac_addr)
            p_macs += 1
    except RequestError as e:
        log.error("Error looking up/deleting orphan MACs: %s", e)
        errors += 1
    return p_macs, errors


def _prune_network_orphans(
    endpoints: NetBoxEndpoints,
    node_type: NodeType,
    ifaces_cache: NameCache,
    csv_iface_names: set[str],
    csv_ips: set[str],
    csv_macs: set[str],
    dry_run: bool,
) -> PrunedNetworkMetrics:
    """
    Deletes (prunes) from NetBox the orphan interfaces and MACs, and unassigns IPs.

    IPs with an assigned role are never unassigned, and an orphan interface that hosts
    any of them is kept (with its other IPs and MACs subject to the normal pruning).
    Returns PrunedNetworkMetrics.
    """
    errors = p_ifaces = p_ips = p_macs = 0
    assigned_type = _get_assigned_object_type(node_type)

    for name, iface_obj in ifaces_cache.items():
        iface_id = getattr(iface_obj, "id", 0)

        # PHASE A: Interfaces (except orphans that host protected IPs)
        keep_interface = name not in csv_iface_names and _keep_orphan_interface(
            endpoints.ip_addresses, assigned_type, name, iface_id
        )
        if not keep_interface:
            del_count, err_count = _prune_orphan_interface(
                name, iface_obj, csv_iface_names, dry_run
            )
            p_ifaces += del_count
            errors += err_count
            if del_count > 0:
                continue

        if iface_id == 0:
            continue

        # PHASE B: IPs (Unassign)
        del_ips, err_ips = _prune_orphan_ips(
            endpoints.ip_addresses, assigned_type, iface_id, csv_ips, dry_run
        )
        p_ips += del_ips
        errors += err_ips

        # PHASE C: MACs (Delete)
        del_macs, err_macs = _prune_orphan_macs(
            endpoints.mac_addresses, assigned_type, iface_id, csv_macs, dry_run
        )
        p_macs += del_macs
        errors += err_macs

    return PrunedNetworkMetrics(p_ifaces, p_ips, p_macs, errors)


def _get_interface_endpoint_and_filter(
    endpoints: NetBoxEndpoints, obj_id: int, node_type: NodeType
) -> tuple[Endpoint, dict[str, Any]]:
    """Determines the right interface endpoint and filter according to the node type."""
    if node_type == NodeType.DEVICE:
        return endpoints.device_interfaces, {"device_id": obj_id}
    return endpoints.vm_interfaces, {"virtual_machine_id": obj_id}


def _fetch_interfaces_cache(
    iface_endpoint: Endpoint,
    iface_filter: dict[str, Any],
    obj_id: int,
) -> NameCache:
    """Gets the existing interfaces of a node from NetBox."""
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
    """Executes the synchronization of a list of interfaces and collects assigned IPs.
    Returns a tuple: (InterfaceSyncResult, updated cache)."""
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
            log.error("API ERROR synchronizing interface: %s", e)
            errors += 1

    return InterfaceSyncResult(errors, ipv4_candidates, any_changes), ifaces_cache


def _log_interface_deltas(
    obj_id: int,
    interfaces: list[NetworkInterfaceData],
    ifaces_cache: dict[str, NetBoxObject],
    prune_network_orphans: bool,
    renamed: int,
) -> None:
    """Computes and logs the deltas of the interfaces before synchronizing."""
    csv_names = {iface["name"] for iface in interfaces}
    cache_names = set(ifaces_cache.keys())
    to_create = csv_names - cache_names
    to_update = csv_names & cache_names
    to_prune = cache_names - csv_names if prune_network_orphans else set()

    log.debug(
        "Interface deltas (obj_id=%s) -> Create: %d | Update: %d "
        "(renamed: %d) | Delete: %d",
        obj_id,
        len(to_create),
        len(to_update),
        renamed,
        len(to_prune),
    )


# ============================================================
# DETECTION OF INTERFACE RENAMES
# ============================================================


def _fetch_orphan_fingerprints(
    endpoints: NetBoxEndpoints,
    node_type: NodeType,
    obj_id: int,
    orphans: dict[str, NetBoxObject],
) -> dict[str, InterfaceFingerprint]:
    """
    Gets from NetBox the network identity (assigned IPs and MACs) of the orphan interfaces.

    Design decisions:
    - Two queries per node (IPs and MACs filtered by `device_id`/`virtual_machine_id`),
      grouped locally by interface: constant cost instead of one query per interface.
    - It is only invoked when the node has both orphan and new interfaces at once.
    """
    node_filter = (
        {"device_id": obj_id}
        if node_type == NodeType.DEVICE
        else {"virtual_machine_id": obj_id}
    )
    name_by_id = {get_netbox_object_id(obj): name for name, obj in orphans.items()}
    ip_hosts: dict[str, set[str]] = {name: set() for name in orphans}
    macs: dict[str, set[str]] = {name: set() for name in orphans}

    for ip_record in endpoints.ip_addresses.filter(**node_filter):
        owner = name_by_id.get(getattr(ip_record, "assigned_object_id", None) or 0)
        address = str(getattr(ip_record, "address", "") or "")
        if owner is not None and address:
            ip_hosts[owner].add(_ip_host(address))

    for mac_record in endpoints.mac_addresses.filter(**node_filter):
        owner = name_by_id.get(getattr(mac_record, "assigned_object_id", None) or 0)
        mac_address = str(getattr(mac_record, "mac_address", "") or "")
        if owner is not None and mac_address:
            macs[owner].add(mac_address.upper())

    return {
        name: InterfaceFingerprint(frozenset(ip_hosts[name]), frozenset(macs[name]))
        for name in orphans
    }


def _csv_interface_fingerprint(iface: NetworkInterfaceData) -> InterfaceFingerprint:
    """Builds the network identity (host IP and MAC) of a CSV interface."""
    cidr = iface["cidr"]
    mac = iface["mac"]
    return InterfaceFingerprint(
        ip_hosts=frozenset([_ip_host(cidr)]) if cidr else frozenset(),
        macs=frozenset([mac.upper()]) if mac else frozenset(),
    )


def _csv_interface_fingerprints(
    interfaces: list[NetworkInterfaceData], names: set[str]
) -> dict[str, InterfaceFingerprint]:
    """Builds the network identity of the CSV interfaces listed in `names`."""
    return {
        iface["name"]: _csv_interface_fingerprint(iface)
        for iface in interfaces
        if iface["name"] in names
    }


def _unique_signal_owners(
    fingerprints: dict[str, InterfaceFingerprint],
) -> dict[str, str]:
    """
    Indexes each identity signal (host IP or MAC) to the only interface that owns it.

    Signals present in more than one interface (e.g. the MAC shared by a bond and
    its members, or by VLAN subinterfaces) are discarded: they identify none.
    Returns {signal: interface_name}; signals are prefixed with their type.
    """
    owners: dict[str, set[str]] = {}
    for name, fingerprint in fingerprints.items():
        signals = {f"IP {ip}" for ip in fingerprint.ip_hosts} | {
            f"MAC {mac}" for mac in fingerprint.macs
        }
        for signal in signals:
            owners.setdefault(signal, set()).add(name)
    return {
        signal: next(iter(names)) for signal, names in owners.items() if len(names) == 1
    }


def _warn_ambiguous_renames(side_label: str, candidates: dict[str, set[str]]) -> None:
    """Warns only once for each interface whose signals point to several interfaces."""
    for name, matches in sorted(candidates.items()):
        if len(matches) > 1:
            log.warning(
                "Ambiguous rename: the %s interface '%s' matches %s. "
                "It is not renamed; review the master inventory.",
                side_label,
                name,
                sorted(matches),
            )


def detect_interface_renames(
    orphans: dict[str, InterfaceFingerprint],
    newcomers: dict[str, InterfaceFingerprint],
) -> list[InterfaceRename]:
    """
    Detects interfaces that changed their name by comparing their network identity.

    Design decisions:
    - `orphans` are interfaces that exist in NetBox but not in the CSV; `newcomers`, those
      that are in the CSV but not in NetBox. A rename keeps the IP and/or the MAC.
    - Only the signals unique on each side count (see `_unique_signal_owners`).
    - The pair must be 1:1: each orphan with a single candidate and vice versa. If the
      signals point to different interfaces, it is not renamed (a warning is issued): guessing
      could assign the identity of one interface to another.

    Returns the accepted renames, sorted by previous name.
    """
    orphan_owner = _unique_signal_owners(orphans)
    newcomer_owner = _unique_signal_owners(newcomers)

    # Signals shared by exactly one orphan and one new interface.
    evidence: dict[tuple[str, str], list[str]] = {}
    for signal, orphan_name in orphan_owner.items():
        newcomer_name = newcomer_owner.get(signal)
        if newcomer_name is not None:
            evidence.setdefault((orphan_name, newcomer_name), []).append(signal)

    candidates_by_orphan: dict[str, set[str]] = {}
    candidates_by_newcomer: dict[str, set[str]] = {}
    for orphan_name, newcomer_name in evidence:
        candidates_by_orphan.setdefault(orphan_name, set()).add(newcomer_name)
        candidates_by_newcomer.setdefault(newcomer_name, set()).add(orphan_name)

    _warn_ambiguous_renames("existing", candidates_by_orphan)
    _warn_ambiguous_renames("new", candidates_by_newcomer)

    return [
        InterfaceRename(orphan_name, newcomer_name, ", ".join(sorted(signals)))
        for (orphan_name, newcomer_name), signals in sorted(evidence.items())
        if len(candidates_by_orphan[orphan_name]) == 1
        and len(candidates_by_newcomer[newcomer_name]) == 1
    ]


def _resolve_interface_renames(
    endpoints: NetBoxEndpoints,
    node_type: NodeType,
    obj_id: int,
    interfaces: list[NetworkInterfaceData],
    ifaces_cache: NameCache,
) -> list[InterfaceRename]:
    """
    Detects the interface renames of a node that exists in NetBox.

    It only queries NetBox when there are both orphan and new interfaces at once, so that
    a node without name changes adds no requests.
    """
    csv_names = {iface["name"] for iface in interfaces}
    orphan_names = ifaces_cache.keys() - csv_names
    newcomer_names = csv_names - ifaces_cache.keys()
    if obj_id == 0 or not orphan_names or not newcomer_names:
        return []

    orphans = {name: ifaces_cache[name] for name in orphan_names}
    orphan_fingerprints = _fetch_orphan_fingerprints(
        endpoints, node_type, obj_id, orphans
    )
    newcomer_fingerprints = _csv_interface_fingerprints(interfaces, newcomer_names)
    renames = detect_interface_renames(orphan_fingerprints, newcomer_fingerprints)
    for rename in renames:
        log.info(
            "Interface rename detected in object %s: '%s' -> '%s' (match: %s).",
            obj_id,
            rename.old_name,
            rename.new_name,
            rename.evidence,
        )
    return renames


def rekey_interfaces_cache(
    cache: NameCache, renames: list[InterfaceRename]
) -> NameCache:
    """
    Returns a new cache where each renamed interface sits under its new name.

    When re-indexing, the upsert of the interface finds the existing object and issues a
    single PATCH with the new name: its ID, IPs, MACs, cables and history are kept.
    """
    new_name_by_old = {rename.old_name: rename.new_name for rename in renames}
    return {new_name_by_old.get(name, name): obj for name, obj in cache.items()}


def _sync_interfaces_for_object(
    endpoints: NetBoxEndpoints,
    obj_id: int,
    node_type: NodeType,
    interfaces: list[NetworkInterfaceData],
    args: argparse.Namespace,
) -> InterfaceSyncResult:
    """Synchronizes interfaces and their IPs for a Device or VM.
    Returns: InterfaceSyncResult."""
    iface_endpoint, iface_filter = _get_interface_endpoint_and_filter(
        endpoints, obj_id, node_type
    )
    ifaces_cache = _fetch_interfaces_cache(iface_endpoint, iface_filter, obj_id)

    renames = _resolve_interface_renames(
        endpoints, node_type, obj_id, interfaces, ifaces_cache
    )
    ifaces_cache = rekey_interfaces_cache(ifaces_cache, renames)

    _log_interface_deltas(
        obj_id, interfaces, ifaces_cache, args.prune_network_orphans, len(renames)
    )

    sync_res, ifaces_cache = _process_interfaces_sync(
        interfaces, obj_id, iface_endpoint, ifaces_cache, endpoints, args.dry_run
    )
    errors, ipv4_candidates, any_changes = sync_res

    if args.prune_network_orphans and obj_id != 0:
        csv_names = {iface["name"] for iface in interfaces}
        csv_ips = {cidr for iface in interfaces if (cidr := iface["cidr"])}
        csv_macs = {mac.upper() for iface in interfaces if (mac := iface["mac"])}

        metrics = _prune_network_orphans(
            endpoints,
            node_type,
            ifaces_cache,
            csv_names,
            csv_ips,
            csv_macs,
            args.dry_run,
        )
        errors += metrics.errors
        any_changes |= (
            metrics.pruned_interfaces + metrics.pruned_ips + metrics.pruned_macs
        ) > 0

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
    Generic helper to assign primary resources (primary IP, primary MAC).
    Returns True if it was assigned successfully (or simulated in dry-run), False otherwise.
    """
    current_primary = getattr(target_obj, field_name, None)
    current_primary_id = (
        getattr(current_primary, "id", None) if current_primary else None
    )

    if current_primary_id == resource_id:
        return False

    if dry_run:
        log.info(
            "[DRY-RUN] WOULD UPDATE %s %s to %s",
            resource_type_label,
            resource_val,
            log_context,
        )
        return True

    try:
        cast(Record, target_obj).update({field_name: resource_id})
        log.info(
            "%s assigned to %s: %s",
            resource_type_label.capitalize(),
            log_context,
            resource_val,
        )
        return True
    except RequestError as e:
        log.error("Error updating %s on %s: %s", resource_type_label, log_context, e)
        return False


def _assign_primary_ipv4(
    main_obj: NetBoxObject,
    primary_id: int,
    machine_name: str,
    dry_run: bool,
) -> bool:
    """
    Sets the given IPv4 address as the primary IP (primary_ip4) of the device/VM.
    Returns True if it was assigned successfully (or simulated in dry-run), False otherwise.
    """
    return _assign_primary_resource(
        target_obj=main_obj,
        field_name="primary_ip4",
        resource_id=primary_id,
        resource_val=f"(ID={primary_id})",
        log_context=f"node '{machine_name}'",
        dry_run=dry_run,
        resource_type_label="primary IP",
    )


def _assign_primary_mac(
    iface_obj: NetBoxObject,
    mac_obj: NetBoxObject | None,
    dry_run: bool,
) -> bool:
    """
    Assigns the MAC as primary on the interface if it is not already.
    Returns True if it was assigned successfully (or simulated in dry-run), False otherwise.
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
        log_context=f"interface '{iface_name}'",
        dry_run=dry_run,
        resource_type_label="primary MAC",
    )


def process_interfaces_and_ips(
    endpoints: NetBoxEndpoints,
    obj_id: int,
    node_type: NodeType,
    interfaces: list[NetworkInterfaceData],
    args: argparse.Namespace,
    main_obj: NetBoxObject | None,
    machine_name: str,
) -> tuple[int, bool]:
    """
    Orchestrates the synchronization of child interfaces and manages the assignment of primary networks.

    Design decisions:
    - Safe primary assignment: If (and only if) there is a single valid IPv4/MAC candidate
      for the whole node, it is automatically assigned as the primary IP/MAC of the device.
      If there are several, NetBox requires manual resolution to avoid destructive ambiguities.

    Returns:
        tuple[int, bool]: A tuple with (number_of_errors, boolean_there_were_real_changes).
    """
    iface_errors, ipv4_candidates, ifaces_changed = _sync_interfaces_for_object(
        endpoints,
        obj_id,
        node_type,
        interfaces,
        args,
    )
    primary_ip_changed = False
    primary_mac_changed = False

    if len(ipv4_candidates) == 1:
        ip_id, iface_obj, mac_obj = ipv4_candidates[0]

        if main_obj is not None:
            primary_ip_changed = _assign_primary_ipv4(
                main_obj, ip_id, machine_name, args.dry_run
            )

        primary_mac_changed = _assign_primary_mac(iface_obj, mac_obj, args.dry_run)

    any_changes = ifaces_changed or primary_ip_changed or primary_mac_changed
    return iface_errors, any_changes


# ============================================================
# MAIN
# ============================================================


def _classify_rows(
    rows: list[CsvRow],
    line_numbers: list[int],
    config: NetBoxMappingConfig,
    counts: SyncCounts,
) -> tuple[ClassifiedRows, SyncCounts]:
    """
    Classifies the CSV rows, separating physical devices (Devices)
    from virtual machines (VMs).

    Design decisions:
    - Dependency order: In NetBox, a VM belongs to a Cluster, which
      usually depends on a Host Device. To avoid the failure of circular
      or broken dependencies, we guarantee that all the physical hardware (Devices)
      is synchronized strictly in a phase prior to the VMs.
    - Fault tolerance: It catches validation errors (e.g. missing OS)
      on the fly, logging the error and bumping the counter without breaking the pipeline.
    - Discarded rows are not returned: they take no part in the synchronization
      phases nor, in `main`, in the identity uniqueness count.
    - `line_numbers` carries the physical line of each row in the file (not its index),
      so that the logs point to the right place even with multiline cells.

    Returns:
        tuple[ClassifiedRows, SyncCounts]: The separated rows and the mutated counters.
    """
    device_rows: list[NumberedRow] = []
    vm_rows: list[NumberedRow] = []

    for row_num, row in zip(line_numbers, rows, strict=True):
        try:
            node_type = get_node_type_from_row(row, config)
            if node_type == NodeType.DEVICE:
                device_rows.append(NumberedRow(row_num, row))
            else:
                vm_rows.append(NumberedRow(row_num, row))
        except RowValidationError as e:
            machine_name = (
                extract_csv_value(row, "machine_name", config) or f"line {row_num}"
            )
            log.error("ERROR line %d ('%s'): %s", row_num, machine_name, e)
            counts[SyncStatus.ERROR] += 1

    log.info(
        "Classification: %d device(s), %d VM(s), %d type error(s).",
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
    device_type_attrs: DeviceTypeAttrsMap,
    fallback_cluster_type: NetBoxObject,
    caches: CacheStore,
    csv_counts: CsvIdentityCounts,
    dry_run: bool,
) -> tuple[SyncResult, CacheStore]:
    """
    Acts as a polymorphic dispatcher to synchronize nodes (Device or VM).

    Design decisions:
    - Abstraction: It hides from the main pipeline the complexity of the different
      hierarchies required by physical and virtual nodes.
    - Cache propagation: When a Device is created/updated, its ID is immediately stored
      in `caches.host_devices`. This way, if a subsequent VM in the same CSV
      declares it as its hypervisor, we avoid a costly network trip (API call) by injecting
      the cached ID directly.

    Returns:
        tuple[SyncResult, CacheStore]: The final result and the propagated cache.
    """
    if node_type == NodeType.DEVICE:
        sync_res, caches = sync_device(
            endpoints,
            row,
            config,
            site,
            cluster_type_map,
            device_type_attrs,
            fallback_cluster_type,
            caches,
            csv_counts,
            dry_run,
        )
        site_id = get_netbox_object_id(site)
        caches.host_devices[SiteScopedName(site_id, machine_name)] = sync_res[1]
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
            csv_counts,
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
    device_type_attrs: DeviceTypeAttrsMap,
    fallback_cluster_type: NetBoxObject,
    caches: CacheStore,
    csv_counts: CsvIdentityCounts,
    counts: SyncCounts,
    args: argparse.Namespace,
) -> tuple[SyncCounts, CacheStore]:
    """
    Unit of Work to atomically process a whole row of the CSV.

    Design decisions:
    - Fault tolerance: It proactively catches all the domain
      exceptions (Skip, API Error, Validation), guaranteeing that a corrupt node does not
      break the whole ETL pipeline, allowing the rest of the rows to be processed.
    - `row_num` is the real line of the CSV where the row starts.
    - Status escalation: If the parent object (Device/VM) had no changes (UNCHANGED),
      but any of its child interfaces was created or updated, the final status
      of the node is escalated to UPDATED so that the execution reports reflect
      the global change of the entity.

    Returns:
        tuple[SyncCounts, CacheStore]: The mutated metrics accumulator and the propagated cache.
    """
    machine_name = extract_csv_value(row, "machine_name", config) or f"line {row_num}"

    try:
        sync_res, caches = _process_node_sync(
            machine_name,
            row,
            node_type,
            endpoints,
            config,
            site,
            cluster_type_map,
            device_type_attrs,
            fallback_cluster_type,
            caches,
            csv_counts,
            args.dry_run,
        )
        result, obj_id, main_obj = sync_res
    except ConfigValidationError as e:
        raise ConfigValidationError(
            f"Configuration error on line {row_num}: {e}"
        ) from e
    except RowSkipCondition as e:
        log.warning("SKIP line %d: %s", row_num, e)
        counts[SyncStatus.SKIPPED] += 1
        return counts, caches
    except (NetBoxApiError, RowValidationError, RequestError) as e:
        log.error("API or validation ERROR on line %d: %s", row_num, e)
        counts[SyncStatus.ERROR] += 1
        return counts, caches
    except Exception:
        log.exception(
            "Unexpected ERROR while processing line %d ('%s')",
            row_num,
            machine_name,
        )
        counts[SyncStatus.ERROR] += 1
        return counts, caches

    counts[result] += 1

    # ── Validate ID for interfaces (Fail-Fast) ────────────
    if not obj_id:
        if not args.dry_run:
            log.warning("SKIP interfaces of '%s': the object has no ID.", machine_name)
        return counts, caches

    # ── Parse interfaces ──────────────────────────────────
    try:
        interfaces = parse_row_interfaces(row, machine_name, config)
    except RowValidationError as e:
        log.error(
            "Validation error in interfaces of '%s': %s. They will be skipped.",
            machine_name,
            e,
        )
        counts[SyncStatus.ERROR] += 1
        return counts, caches
    except Exception:
        log.exception("Unexpected ERROR while parsing interfaces of '%s'", machine_name)
        counts[SyncStatus.ERROR] += 1
        return counts, caches

    # ── Synchronize the interfaces of the object ──────────
    try:
        iface_errors, any_changes = process_interfaces_and_ips(
            endpoints,
            obj_id,
            node_type,
            interfaces,
            args,
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
            "Unexpected ERROR while synchronizing interfaces of '%s'", machine_name
        )
        counts[SyncStatus.ERROR] += 1

    return counts, caches


def _print_summary_and_exit(
    total_rows: int,
    counts: SyncCounts,
    dry_run: bool,
) -> NoReturn:
    """
    Prints the summary of the operation and ends the execution.
    Ends with sys.exit().
    """
    summary = (
        "\n" + "=" * 50 + "\n"
        "NetBox export summary\n" + "=" * 50 + "\n"
        f"  Total rows processed   : {total_rows}\n"
        f"  Created                : {counts[SyncStatus.CREATED]}\n"
        f"  Updated                : {counts[SyncStatus.UPDATED]}\n"
        f"  Unchanged              : {counts[SyncStatus.UNCHANGED]}\n"
        f"  Skipped (SKIP)         : {counts[SyncStatus.SKIPPED]}\n"
        f"  Errors                 : {counts[SyncStatus.ERROR]}\n" + "=" * 50
    )

    if dry_run:
        summary += "\n(DRY-RUN mode: no changes were made in NetBox)"

    log.info(summary)

    sys.exit(0 if counts[SyncStatus.ERROR] == 0 else 1)


def main() -> None:
    """
    Main entry point for the synchronization of the inventory with NetBox.
    """
    parser = argparse.ArgumentParser(
        description="Exports merged_inventory.csv to NetBox 4.6+"
    )
    parser.add_argument(
        "csv",
        type=Path,
        help="Path to the merged CSV (merged_inventory.csv).",
    )
    parser.add_argument(
        "mapping",
        type=Path,
        nargs="?",
        default=None,
        help="Path to netbox_mapping.yaml (default: config/netbox_mapping.yaml).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Shows the operations without modifying NetBox.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enables DEBUG level logs.",
    )
    parser.add_argument(
        "--prune-network-orphans",
        action="store_true",
        help="Deletes the NetBox interfaces that no longer exist in the CSV for that node.",
    )

    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    if args.dry_run:
        log.info("DRY-RUN mode enabled. NetBox will not be modified.")

    # ── Load environment variables from .env if it exists ───
    load_dotenv()

    # ── Load configuration ───────────────────────────────────
    mapping_path = resolve_mapping_path(args.mapping)
    config: NetBoxMappingConfig = load_config(mapping_path)

    # ── Read and Validate CSV (Fail-Fast) ────────────────────
    csv_data = read_and_validate_csv(args.csv, config)

    # ── Load credentials ─────────────────────────────────────
    url, token, verify_ssl = load_env()

    # ── Connect ──────────────────────────────────────────────
    nb: Api = build_nb_client(url, token, verify_ssl)

    # ── Validate required endpoints ──────────────────────────
    endpoints: NetBoxEndpoints = build_netbox_endpoints(nb)

    # ── Ensure custom fields ─────────────────────────────────
    ensure_custom_fields(endpoints, config, args.dry_run)

    # ── Ensure global taxonomy ───────────────────────────────
    site: NetBoxObject = ensure_site(endpoints.sites, config.site, args.dry_run)

    # ── Initialize caches ────────────────────────────────────
    caches: CacheStore = CacheStore()

    # ── Counters ─────────────────────────────────────────────
    counts: SyncCounts = {
        SyncStatus.CREATED: 0,
        SyncStatus.UPDATED: 0,
        SyncStatus.UNCHANGED: 0,
        SyncStatus.SKIPPED: 0,
        SyncStatus.ERROR: 0,
    }

    # Records with an invalid structure were already discarded when reading the CSV
    # and must be reflected in the summary and in the exit code.
    counts[SyncStatus.ERROR] += csv_data.rejected_rows

    # ── Classify rows by node type ────────────────────────────
    classified_rows, counts = _classify_rows(
        csv_data.rows, csv_data.line_numbers, config, counts
    )
    device_rows = classified_rows.device_rows
    vm_rows = classified_rows.vm_rows

    # ── Global identity count (name and UUID) ────────────────
    # Used to validate uniqueness before allowing updates on
    # existing objects. It is computed only over the classified rows.
    classified_only = [numbered.row for numbered in (*device_rows, *vm_rows)]
    csv_counts = CsvIdentityCounts(
        names=count_machine_names(classified_only, config),
        uuids=count_inventory_uuids(classified_only, config),
    )

    # ── Pre-scan: associate clusters with their technology ───
    device_rows_only = [numbered.row for numbered in device_rows]
    cluster_type_map = precompute_cluster_type_map(device_rows_only, config)
    fallback_cluster_type, caches.cluster_types = ensure_dynamic_cluster_types(
        endpoints,
        cluster_type_map,
        config.cluster_type,
        caches.cluster_types,
        args.dry_run,
    )

    # ── Pre-scan: DeviceType attributes consolidated per model ──
    device_type_attrs = precompute_device_type_attributes(device_rows, config)

    # ── Ensure local taxonomy ───────────────────────────────
    _, caches.device_roles = ensure_all_device_roles(
        endpoints, config.device_roles, caches.device_roles, args.dry_run
    )

    # ── Phase 1: Synchronize Devices ─────────────────────────
    log.info("── Phase 1: Synchronizing %d Device(s) ──", len(device_rows))
    for row_num, row in device_rows:
        counts, caches = _sync_row(
            row_num,
            row,
            NodeType.DEVICE,
            endpoints,
            config,
            site,
            cluster_type_map,
            device_type_attrs,
            fallback_cluster_type,
            caches,
            csv_counts,
            counts,
            args,
        )

    # ── Phase 2: Synchronize VMs ─────────────────────────────
    log.info("── Phase 2: Synchronizing %d VM(s) ──", len(vm_rows))
    for row_num, row in vm_rows:
        counts, caches = _sync_row(
            row_num,
            row,
            NodeType.VIRTUAL_MACHINE,
            endpoints,
            config,
            site,
            cluster_type_map,
            device_type_attrs,
            fallback_cluster_type,
            caches,
            csv_counts,
            counts,
            args,
        )

    # ── Summary ──────────────────────────────────────────────
    _print_summary_and_exit(len(csv_data.rows), counts, args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except ConfigValidationError as e:
        log.critical("CONFIGURATION ERROR. ABORTING PIPELINE: %s", e)
        sys.exit(1)
    except NetBoxApiError as e:
        log.critical("API ERROR. ABORTING PIPELINE: %s", e)
        sys.exit(1)
