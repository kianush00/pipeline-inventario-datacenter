# GLPI Export Roadmap (`export_to_glpi.py`)

This document is the phased implementation plan for a new pipeline stage that synchronizes `merged_inventory.csv` into a local **GLPI 11.0.x** instance (target version: 11.0.8). It records the architectural decisions, the CSV → GLPI data model, the requirements, and the scope and acceptance criteria of each phase.

Each phase is delivered in one implementation cycle (one request to the agent). Every phase ships its own unit tests in `tests/test_export_to_glpi.py` and goes through the `qa-tester` and `code-reviewer` skills.

---

## Phase Status

| Phase | Scope | Status |
| ----- | ----- | ------ |
| 0 | GLPI preparation (API client, service account, entity) — manual | Pending |
| 1 | Shared export core (`export_core.py`), behavior-preserving refactor | Pending |
| 2 | GLPI client, configuration, preflight and exporter skeleton | Pending |
| 3 | Native taxonomy (dropdowns and locations) | Pending |
| 4 | Computer synchronization (basic inventory MVP) | Pending |
| 5 | Operating system | Pending |
| 6 | Network ports, network names and IP addresses | Pending |
| 7 | Racks | Pending |
| 8 | Virtualization and clusters | Pending |
| 9 | Warranty (Infocom) | Pending |
| 10 | Hardware components (optional) | Pending |
| 11 | Custom fields (Fields plugin, conditional) | Pending |
| 12 | Hardening and closure | Pending |

---

## Context

`export_to_netbox.py` already synchronizes the consolidated CSV into NetBox 4.6+ in production. The organization also runs GLPI, and needs an equivalent stage with the same guarantees: idempotency, `--dry-run`, per-row fault tolerance, exit codes `0`/`1`, and traceable logs.

GLPI is an ITAM/ITSM tool, not a DCIM/IPAM tool. The NetBox exporter is therefore **not ported**. The GLPI stage reuses the target-agnostic core of the pipeline and models the destination on GLPI's **native** objects. Early phases deliver the basic inventory; later phases add network, racks, virtualization, warranty, components and, last, custom fields.

**Decisions agreed with the project owner:**

- Shared utilities are extracted into a new module `export_core.py` (Phase 1, behavior-preserving refactor).
- The native `ComputerType` is fed from the `Tipo de maquina` column.

---

## Verified Assumptions

Planning assumptions were checked against the official GLPI documentation, the GLPI 11.0.0 SQL schema, the GLPI 11 source code, and the current CSV.

| Assumption | Result | Evidence |
| ---------- | ------ | -------- |
| The legacy API (`apirest.php`) is still available and offers more functionality than API v2 | **Confirmed** | Official RESTful API v2 docs |
| The High-Level API v2 uses OAuth2 | **Confirmed, with a decisive caveat**: in GLPI 11 the *client credentials* grant only covers the `inventory` scope. Every other resource requires a real user (*password* or *authorization code* grant) | Official RESTful API v2 docs |
| Rack position is edited on `Item_Rack`, not on `Computer` | **Confirmed**. `glpi_items_racks.position` is `INT`, so fractional positions cannot be stored. The CSV has 3 rows with `.5` positions | GLPI 11.0.0 schema, GLPI forum |
| Manually edited fields get locked against automatic inventory | **Narrowed**: `Lockedfield::isHandled()` only applies to `is_dynamic=1` assets. Computers created through the API with `is_dynamic=0` never get locks | `src/Lockedfield.php`, `src/CommonDBTM.php` (11.0.0) |
| `uuid`, `serial` and `otherserial` are native Computer fields | **Confirmed** in `glpi_computers` | GLPI 11.0.0 schema |
| VMs are stored as `ComputerVirtualMachine` | **Corrected**: in GLPI 11 the class is `ItemVirtualMachine` (`glpi_itemvirtualmachines`, with `itemtype`/`items_id`, `uuid`, `vcpu`, `ram`) | GLPI 11.0.0 schema |
| UUID capitalization may cause duplicates | **Confirmed and measured**: 78 of 286 CSV UUIDs are uppercase. GLPI tables use `utf8mb4_unicode_ci`, which compares case- **and accent-** insensitively | GLPI 11.0.0 schema, CSV profile |
| Legacy listings paginate around 50 records | **Confirmed**: `range` defaults to `0-49`; partial responses return HTTP 206 with `Content-Range` | `apirest.md` |
| Memory unit base must be decided | **Resolved**: `asset_information.sh` computes RAM and disk in GiB (divides by 1024³). GLPI stores MiB, so the correct factor is ×1024 | `asset_information.sh` |
| Model height | `glpi_computermodels.required_units` is `INT`. The CSV has 3 rows with height `0` | GLPI 11.0.0 schema, CSV profile |
| Input sanitization | GLPI 11 no longer HTML-escapes input; strings are stored raw, so string diffs are reliable | GLPI 11.0 upgrade guide |
| Fields plugin | Version 1.22.0 supports GLPI 11.0.x, with an open tab-rendering bug (issue #1030) | Plugin docs, pluginsGLPI/fields |

**Current CSV profile:** 367 rows: 84 dedicated servers, 35 hypervisors and 248 VMs. 241 VMs declare a host. 276 rows have network interfaces.

---

## Architecture Decisions

### ADR-01. Legacy REST API v1 (`/apirest.php`), not native inventory JSON nor API v2

The real choice is "REST CRUD" versus "native inventory submission"; both use JSON payloads.

- **Native inventory (`/front/inventory.php`)** is rejected. It is the agent format: it applies import rules that can merge or duplicate assets, marks everything as `is_dynamic=1` (which enables field locks on any later manual edit), and its schema cannot carry State, the Location tree, rack position, role or environment. The CSV is curated master data, not discovery output.
- **API v2 (`/api.php/v2`)** is deferred. For non-inventory resources it requires storing a user password plus a client secret. Its coverage of `Item_Rack`, `NetworkPort`, `Infocom`, `ItemVirtualMachine` and the Fields plugin is not documented.
- **API v1** authenticates with a revocable `user_token` plus an `App-Token` that can be IP-restricted. It covers every `CommonDBTM` itemtype, plugin itemtypes included, with documented pagination, bulk input and trash semantics (`force_purge=false` by default).
- **Deprecation mitigation:** all HTTP lives in a single adapter class, `GlpiClient`. Domain functions never build URLs, so migrating to API v2 means writing a new adapter, not rewriting the synchronization logic.

### ADR-02. Plain `requests` with a project-owned typed client

- There is no official client comparable to `pynetbox`. The available libraries (`glpi-api`, `glpilib2`, `glpi-python-client`, `python-glpi-utils`) are third-party, with low adoption and no maintenance guarantee. None was verified to cover `Item_Rack`, `ItemVirtualMachine` or the Fields plugin with strict typing.
- `requests` and `urllib3` are already pinned in `requirements.txt` and in use. No new dependency is added.
- The required surface is small: `initSession`, `killSession`, paginated GET, sub-item GET, POST, PUT and DELETE.
- Owning the client allows retrying only idempotent GETs, renewing expired sessions, mapping `["ERROR_X", "message"]` errors to a domain exception, never logging tokens, and testing everything with `MagicMock` without network access.
- Estimated cost: 250–350 lines plus tests.

### ADR-03. VMs as `Computer` plus `ItemVirtualMachine` on the host

- Each VM is a `Computer` with `ComputerType = "Máquina virtual"` (Phase 4), so it can hold an OS, network ports, a state and tickets.
- Phase 8 adds an `ItemVirtualMachine` on the host Computer with the same UUID, and GLPI links both by UUID. The "guest UUID differs from host-reported UUID" risk disappears because the pipeline writes both sides from the same CSV value.

### ADR-04. Identity and normalization

- The primary key is the native `Computer.uuid` field (no custom field), stored in lowercase as in NetBox (see [Architecture](architecture.md#uuid-normalization)).
- Identity resolution reuses the NetBox two-stage rule: a UUID unique in both the CSV and GLPI; otherwise a name unique in both. A name match whose existing UUID is different and non-empty is skipped.
- Local index keys are normalized to mirror `utf8mb4_unicode_ci`: casefolded, accent-stripped, whitespace-collapsed. Without this, "HP" vs "Hp" or "Producción" vs "Produccion" would create duplicates.
- A matching Computer in the trash (`is_deleted=1`) is skipped with a `WARNING`; it is never restored automatically.
- Templates (`is_template=1`) are excluded.
- A Computer with `is_dynamic=1` (agent-managed) is skipped with a `WARNING`, because updating it would create field locks.

### ADR-05. Prefetched indexes instead of per-row searches

All Computers of the entity and all referenced dropdown tables are downloaded once (paginated) and indexed in dictionaries with O(1) lookups. With 367 rows this is about 10 GET requests instead of up to ~730. Every function that creates an object returns the updated index explicitly (no silent mutation).

### ADR-06. `glpi_mapping.yaml` is independent from `netbox_mapping.yaml`

Each target owns its contract, and the value maps differ (NetBox status `active` vs a GLPI `State` name such as "Activo"). `netbox_mapping.yaml` is not modified. Shared logic lives in `export_core.py`, not in configuration.

### ADR-07. Computers are never deleted

As in the NetBox exporter, only optional network pruning exists (`--prune-network-orphans`, Phase 6), and it always moves items to the trash, never purges.

### ADR-08. Native constraints that force skipping data with a warning

| Data | GLPI constraint | Policy |
| ---- | --------------- | ------ |
| Fractional position (`12.5`) | `Item_Rack.position` is `INT` | Rack placement is skipped with a `WARNING`; the Computer is still synchronized |
| Height `0` | `required_units` is `INT`, default `1` | Height is not written and the item is not placed in the rack (`WARNING`) |
| Different heights for the same model | Height lives on `ComputerModel` | Precomputed; on conflict the model is not updated (`WARNING`) |
| Interface up/down status | No administrative equivalent on Computer ports | Not mapped (`DEBUG`) |
| Server role | No native field | Deferred to Phase 11 (Fields plugin) |

---

## CSV → GLPI Data Model

| CSV column | GLPI object / field | Phase |
| ---------- | ------------------- | ----- |
| `Nombre maquina` | `Computer.name` | 4 |
| `UUID` | `Computer.uuid` (lowercase) | 4 |
| `Serial Number` + `Service Tag` (coalesce) | `Computer.serial` | 4 |
| `Nro Inventario` | `Computer.otherserial` | 4 |
| `Descripcion` + `Notas` (concat_dot) | `Computer.comment` | 4 |
| `Estado` | `State` → `states_id` | 3–4 |
| `Tipo de maquina` | `ComputerType` → `computertypes_id` | 3–4 |
| `Marca` | `Manufacturer` → `manufacturers_id` | 3–4 |
| `Modelo` + `P/N` + `Altura (U)` | `ComputerModel` (`name`, `product_number`, `required_units`) | 3–4 |
| `GLPI_LOCATION_NAME` (env) + `Fila` | Root `Location` + child `Location` → `locations_id` | 3–4 |
| `GLPI_ENTITY_ID` (env) | `entities_id` | 2–4 |
| `SO Host` + `Version SO Host` + `Kernel` | `Item_OperatingSystem` (`OperatingSystem`, `OperatingSystemVersion`, `OperatingSystemKernelVersion`) | 5 |
| `Interfaces`, `IP`, `Red IP`, `MAC` | `NetworkPort` (Ethernet) → `NetworkName` → `IPAddress` (+ optional `IPNetwork`) | 6 |
| `Rack` + `Posicion (U)` | `Rack` + `Item_Rack` | 7 |
| `Cluster` + `SO Hipervisor` + `Version SO Hipervisor` | `Cluster` (`ClusterType`, `version`) + `Item_Cluster` (hypervisors) | 8 |
| `Dispositivo Host` + `Cores` + `RAM (GB)` | `ItemVirtualMachine` on the host (`vcpu`, `ram` in MiB, `uuid`) | 8 |
| `Fecha Inicio` + `Fecha Vencimiento` | `Infocom` (`warranty_date`, `warranty_duration` in months) | 9 |
| `CPU Modelo`, `Cores (por socket)`, `Threads (por socket)`, `Sockets`, `RAM (GB)`, `Capacidad visible (GB)` | `Item_DeviceProcessor`, `Item_DeviceMemory`, `Item_Disk` | 10 |
| `Rol`, `Entorno`, `Nodo Rundeck`, `Version BIOS`, `Fecha BIOS`, `RAID`, `Discos`, etc. | Fields plugin container | 11 |

---

## Requirements

### Functional (MoSCoW)

| ID | Requirement | Priority | Phase |
| -- | ----------- | -------- | ----- |
| FR-01 | Read `merged_inventory.csv` and validate the required headers declared in `glpi_mapping.yaml` before contacting GLPI | Must | 2 |
| FR-02 | Validate `glpi_mapping.yaml` with Pydantic: structure, cross-references and an allow-list of target fields (GLPI silently ignores unknown keys) | Must | 2 |
| FR-03 | Authenticate with `App-Token` + `user_token` and always close the session, including on errors | Must | 2 |
| FR-04 | Preflight: GLPI version, entity access, per-itemtype rights, Fields plugin presence and number of dynamic assets | Must | 2 |
| FR-05 | `--preflight-only` option that runs only the preflight and exits | Should | 2 |
| FR-06 | Ensure the native taxonomy (State, Manufacturer, ComputerModel, ComputerType, Location) without duplicates | Must | 3 |
| FR-07 | Create or update Computers by UUID, falling back to name, validating uniqueness in both the CSV and GLPI | Must | 4 |
| FR-08 | Send only changed fields; with no differences the result is `UNCHANGED` and no request is issued | Must | 4 |
| FR-09 | Never modify dynamic or trashed Computers (`SKIP` with `WARNING`) | Must | 4 |
| FR-10 | With `--dry-run`, perform zero writes: only reads and `WOULD CREATE/UPDATE` logs | Must | 2–11 |
| FR-11 | Final summary with CREATED/UPDATED/UNCHANGED/SKIPPED/ERROR; exit `0` without row errors, `1` with at least one | Must | 2 |
| FR-12 | Synchronize the operating system | Should | 5 |
| FR-13 | Synchronize ports, network names and IPs, with optional `--prune-network-orphans` | Should | 6 |
| FR-14 | Synchronize racks and positions | Should | 7 |
| FR-15 | Synchronize clusters, hypervisor membership and VMs on their host | Should | 8 |
| FR-16 | Synchronize warranty data (Infocom) | Could | 9 |
| FR-17 | Synchronize hardware components, enabled through the YAML | Could | 10 |
| FR-18 | Synchronize custom fields when the Fields plugin is installed and enabled in the YAML | Could | 11 |
| FR-19 | Never delete a Computer | Must | all |

### Non-Functional

| ID | Requirement | Acceptance criterion |
| -- | ----------- | -------------------- |
| NFR-01 Idempotency | Re-running with the same input changes nothing | The second run reports 0 CREATED and 0 UPDATED (in-memory fake client test, and a real run in Phase 12) |
| NFR-02 Scalability | Designed for thousands of rows | O(1) lookups; reads grow with the number of pages, not rows; writes only when something changed |
| NFR-03 Typing | Strict static typing | `pyright` with no errors; payloads typed with `pydantic.JsonValue`, no unjustified `Any` |
| NFR-04 Code quality | Uniform style | Clean `ruff check` and `ruff format`; `py_compile` passes |
| NFR-05 Testing | No real network in tests | Per-phase tests in `tests/test_export_to_glpi.py` with `MagicMock`, global imports, full suite green |
| NFR-06 Security | Least privilege | Credentials only from the environment or `.env`; tokens never logged; SSL verified by default; dedicated GLPI profile and IP-restricted App-Token |
| NFR-07 Observability | Useful logs | Spanish log messages with proper levels; `--verbose` shows HTTP method, URL and status; the summary reports the request count |
| NFR-08 Fault tolerance | Isolated failures | A row error never aborts the run; configuration or connection errors abort with exit `1` |
| NFR-09 HTTP resilience | Transient failures | Connect/read timeouts; backoff retries only for GET on 502/503/504; one session renewal on expired token |
| NFR-10 Maintainability | Isolated transport | HTTP only inside `GlpiClient`; pure functions that return the state they modify |
| NFR-11 Documentation | Always in sync | `docs/` updated (in English) in every phase |
| NFR-12 Compatibility | No new dependencies | GLPI 11.0.x (target 11.0.8); same Python version as the rest of the repository |
| NFR-13 No regression | NetBox untouched | After Phase 1, `export_to_netbox.py` behaves identically: same number of passing tests and an identical `--dry-run` log |

---

## Definition of Done (every phase)

1. **Implementation** following `CLAUDE.md` §1–§8 and replicating the patterns of `export_to_netbox.py`: `NamedTuple`/`TypedDict`/Pydantic models, `*_error_wrap` context managers, functions that return caches and counters, early returns, and docstrings explaining the "why".
2. **Phase tests** in `tests/test_export_to_glpi.py`, under a `# FASE N: ...` section with `TestXxx` classes. They use `MagicMock(spec=GlpiClient)` or the in-memory `FakeGlpiClient`, declare all imports at the top of the file, and make no network calls. The only exception is Phase 1, whose tests live in `tests/test_export_core.py` because they cover the shared module.
3. **Static checks** on every touched file, tests included:

   ```bash
   .venv/bin/pyright <file>
   .venv/bin/ruff check <file> --fix
   .venv/bin/ruff format <file>
   .venv/bin/python3 -m py_compile <file>
   ```

4. **`qa-tester` skill** (`.agents/skills/qa-tester/SKILL.md`): edge-case map before writing tests, destructive tests, bugs reported in the Red stage and fixed until Green.
5. **`code-reviewer` skill** (`.agents/skills/code-reviewer/SKILL.md`): adversarial audit of the diff and verification of `docs/`.
6. **Full suite:** `.venv/bin/pytest tests/ -v` green.
7. **Manual `--dry-run`** against GLPI (from Phase 2 on) whenever credentials are available in `.env`.
8. **Documentation** updated in `docs/` and in `README.md` where applicable.
9. **Final report** with an explicit block summarizing the `qa-tester` and `code-reviewer` findings. No commit unless requested.

---

## Phase 0. GLPI Preparation (manual, no code)

Can run in parallel with Phase 1, ideally on a test instance or a clone of GLPI 11.0.8.

1. **Setup > General > API:** enable the REST API and login with external token.
2. **Create an API client** restricted to the pipeline host IP and record its `App-Token`.
3. **Create a service account** (e.g. `svc-inventario`) with a dedicated profile: read, create and update rights on Computer, State, Manufacturer, ComputerModel, ComputerType and Location. Later phases add Rack, network ports, Infocom, Cluster, VM and OS. No purge right. Generate its remote access key (`user_token`) in its preferences.
4. **Record the target entity ID** and choose the name of the datacenter root Location.
5. **Review Setup > Fields unicity:** active rules on Computer (e.g. unique serial) will turn violating creations into row-level errors.
6. **Check whether the servers already exist** as manual records. Same name and no UUID means the pipeline adopts them by name. A different name means a duplicate, so review the first `--dry-run`.
7. **Fields plugin and agents:** no manual check needed. `--preflight-only` (Phase 2) reports both.
8. **Back up** the GLPI database before the first real run.

New variables in `.env` (and in `.env.example`, without secrets):

```bash
GLPI_URL="https://glpi.example.org"     # without /apirest.php
GLPI_APP_TOKEN="<app-token>"
GLPI_USER_TOKEN="<user-token>"
GLPI_VERIFY_SSL="true"
GLPI_ENTITY_ID="0"
GLPI_LOCATION_NAME="Datacenter-1"
```

---

## Implementation Phases

### Phase 1. Shared export core (`export_core.py`), behavior-preserving refactor

**Goal:** let the GLPI exporter reuse target-agnostic logic without duplicating it and without depending on `pynetbox`.

**Moved from `export_to_netbox.py` to `export_core.py`** (NetBox imports it back):

- Exceptions `RowValidationError`, `ConfigValidationError`, `RowSkipCondition` and `FieldParseError`.
- Types `SyncStatus`, `NodeType`, `CastType`, `CsvRow`, `FieldValue`, `SyncCounts`, `CsvIdentityCounts`, `ClassifiedRows` and `RACK_POSITION_MIN`.
- Casts `parse_int`, `_to_float`, `parse_float`, `parse_float_to_int`, `parse_int_gb_to_mb`, `parse_rack_position`, `parse_bool_si_no` and `apply_cast`.
- Base configuration: `CsvColumnDef`, `FieldMappingConfig` and a new `BaseMappingConfig` holding `csv_columns`, `empty_values`, `is_empty`, `map_value`, `map_value_by_source`, `get_column_def_by_source`, `get_required_columns`, `get_all_expected_columns`, `resolve_node_type`, `is_cluster_host`, `is_virtual_machine` and the `machine_type` map validation. `NetBoxMappingConfig` inherits from it.
- Extraction: `concat_dot`, `_extract_raw_csv_value`, `extract_csv_value`, `_extract_concat_dot_value`, `_extract_coalesce_value`, `_extract_raw_source_value`, `_resolve_default_or_empty` and `get_node_type_from_row`.
- Stages 1–3 of `_resolve_field_value` (extraction, empties and map). NetBox keeps `_validate_select_choice` and composes validation and cast on top.
- Loading: a generic `load_yaml_mapping(path, model_cls)` with a `TypeVar` bound to `BaseMappingConfig` (NetBox's `load_config` becomes a thin wrapper), plus `load_dotenv`.
- CSV: `_read_csv`, `_validate_csv_headers` and `read_and_validate_csv`.
- Identity: `count_machine_names`, `count_inventory_uuids` and `_is_identifier_unique`.
- Orchestration: `_classify_rows`, a new `new_sync_counts()`, and `print_summary_and_exit(target_label, ...)` parameterized by target name.

**Not moved yet:** network parsing (Phase 6) and `precompute_cluster_type_map` (Phase 8). They are extracted when GLPI needs them.

**Tests:** the classes covering moved code move to `tests/test_export_core.py`: `TestParseInt`, `TestParseFloat`, `TestParseFloatToInt`, `TestParseRackPosition`, `TestParseIntGbToMb`, `TestParseBoolSiNo`, `TestApplyCast`, `TestApplyCastEdgeCases`, `TestExtractCsvValue`, `TestConcatDot`, `TestExtractConcatDotValue`, `TestGetNodeTypeFromRow`, `TestCountMachineNames`, `TestCountInventoryUuids`, `TestValidateCsvHeaders`, `TestFieldMappingConfig`, `TestResolveDefaultOrEmpty`, `TestExtractRawSourceValue`, `TestClassifyRows` and the generic part of `TestNetBoxMappingConfig`. New: `TestLoadYamlMapping` (generic model, invalid YAML, environment expansion) and `TestPrintSummaryAndExit` (target label and exit code).

**Docs:** `docs/components.md` ("Shared export core" section) and `README.md` (project structure).

**Acceptance criteria:**

- `pytest tests/` passes with the same total number of tests as before the refactor.
- The log of `export_to_netbox.py merged_inventory.csv --dry-run` is identical before and after (empty `diff`).

### Phase 2. GLPI client, configuration and exporter skeleton

**Goal:** a runnable `export_to_glpi.py` that reads and validates the CSV, connects, runs the preflight and prints a summary, still without writing Computers.

**Files:** `export_to_glpi.py`, `glpi_mapping.yaml`, `.env.example`, `tests/test_export_to_glpi.py` and `tests/conftest.py` (`glpi_yaml_path` and `glpi_config` fixtures, `GLPI_*` variables in `mock_env_vars`).

**Components of `export_to_glpi.py`:**

- `GlpiApiError` and the `glpi_error_wrap` context manager, analogous to `NetBoxApiError` and `netbox_error_wrap`.
- `GlpiCredentials(NamedTuple)` and `load_glpi_env()`: validates required variables, requires `GLPI_ENTITY_ID` to be an integer `>= 0`, and warns on `http://` URLs.
- `GlpiRecord(BaseModel, frozen, extra="allow")` with `id` and `name`; `GlpiPayload = dict[str, JsonValue]` and `GlpiPage(NamedTuple)`.
- `GlpiClient` as a context manager:
  - `__enter__` calls `initSession`; `__exit__` always calls `killSession`. A failing `killSession` is logged and never masks the original exception.
  - Methods `get_all(itemtype, params)` (paginated through `range` and `Content-Range`), `get_item`, `get_sub_items`, `create` (returns the id), `update`, `delete(force_purge=False)`, `get_glpi_config`, `get_full_session` and `get_my_entities`.
  - `requests.Session` with an `HTTPAdapter` and `urllib3.Retry` for GET only (502/503/504 with backoff), and `(connect, read)` timeouts.
  - On 401 `ERROR_SESSION_TOKEN_INVALID`, renews the session once and retries.
  - Parses `["ERROR_X", "message"]` errors defensively, falling back to the raw body.
  - DEBUG logging hook with method, URL and status, never headers.
  - Counts requests for the summary.
  - The client knows nothing about `dry_run`; that control lives in the domain write primitives, as in NetBox.
- `parse_content_range(header) -> tuple[int, int, int]` as a pure function.
- `GlpiMappingConfig(BaseMappingConfig)` with `entity`, `location.root_name`, `states`, `computer_types` (canonical `machine_type` → GLPI name) and `node_types.{device,virtual_machine}` with `default_state` and `native_mappings`. Validators:
  - Every `target` must belong to the Computer field allow-list.
  - `default_state` and every value of the `status` map must be declared in `states`.
  - `computer_types` must cover every value of the `machine_type` map.
- `run_preflight(client, config) -> PreflightReport(NamedTuple)`:
  - Logs the GLPI version and warns if it is not 11.x.
  - Raises `ConfigValidationError` if the entity is not accessible.
  - Probes `range=0-0` on every required itemtype and lists all missing rights before aborting.
  - Detects the Fields plugin (`INFO`) and counts Computers with `is_dynamic=1` (`WARNING` if any).
- `main()` with the CLI `export_to_glpi.py <csv> [glpi_mapping.yaml] [--dry-run] [--verbose] [--preflight-only]`, consistent with `export_to_netbox.py`, and `ConfigValidationError`/`GlpiApiError` handling in `__main__`.

**`glpi_mapping.yaml` sketch:**

```yaml
csv_columns:            # same aliases as NetBox, GLPI-specific maps
  machine_name: { source: "Nombre maquina", required: true }
  machine_type: { source: "Tipo de maquina", required: true,
                  map: { "dedicada": "dedicated", "hipervisor": "hypervisor", "vm": "vm" } }
  status: { source: "Estado", map: { "activo": "Activo", "fuera de línea": "Fuera de línea", ... } }
  ...
entity: { id: "${GLPI_ENTITY_ID}", is_recursive: true }
location: { root_name: "${GLPI_LOCATION_NAME}" }
states: ["Activo", "Planificado", "En preparación", "Fallido", "En inventario", "En baja", "Fuera de línea"]
computer_types: { dedicated: "Servidor dedicado", hypervisor: "Hipervisor", vm: "Máquina virtual" }
node_types:
  device:          { default_state: "En inventario", native_mappings: [...] }
  virtual_machine: { default_state: "En preparación", native_mappings: [...] }
empty_values: [...]
```

**Tests:** `TestLoadGlpiEnv`, `TestGlpiMappingConfig` (real YAML, target outside the allow-list, undeclared state, incomplete types), `TestParseContentRange` (valid, missing, malformed), `TestGlpiClientSession` (login, invalid token, `killSession` on exception, failing `killSession`), `TestGlpiClientGetAll` (single page 200, multiple pages 206, empty table, `ERROR_RANGE_EXCEED_TOTAL`, non-JSON body), `TestGlpiClientErrors` (error array, HTML body, timeout, `ConnectionError`), `TestGlpiClientSessionExpiry` (renews once, second 401 fails), `TestRunPreflight` and `TestMainPreflightOnly`. HTTP responses are built as real `requests.Response` objects (status, headers, content), with no new libraries.

**Docs:** `docs/usage_and_config.md` (GLPI section: variables, profile rights, execution) and `docs/components.md` (`export_to_glpi.py` section).

**Acceptance criterion:** `--preflight-only` against the real GLPI reports version, entity, rights, Fields plugin presence and number of dynamic assets.

### Phase 3. Native taxonomy (dropdowns and locations)

**Goal:** ensure, without duplicates, the catalogs a Computer references.

- `normalize_lookup_key(name) -> str`: NFKD without diacritics, casefolded, whitespace-collapsed, mirroring the `utf8mb4_unicode_ci` collation.
- `DropdownCache: TypeAlias = dict[str, GlpiRecord]` and `prefetch_dropdown(client, itemtype) -> DropdownCache`. Pre-existing duplicates under the same normalized key resolve deterministically to the lowest id, with a `WARNING`.
- `ensure_dropdown(client, itemtype, name, extra, cache, dry_run) -> tuple[GlpiRecord, DropdownCache]`: cached get-or-create; returns a mock with id `0` in dry-run. GLPI has no slugs, so NetBox's fallback-slug logic is not ported.
- `ensure_states(...)` seeds the YAML states with Computer visibility. The exact visibility input (`DropdownVisibility`) is verified on the test instance at the start of the phase.
- `ensure_location_tree(...)`: the root is looked up by name with `locations_id=0`; the `Fila` is a child keyed by `(parent_id, name)`. Location and State are created in the configured entity with the YAML `is_recursive` flag.
- `ensure_computer_model(...)` and `_sync_computer_model_attributes(...)` manage `product_number` and `required_units`. `precompute_model_heights(rows, config)` detects conflicting and zero heights.
- `ensure_manufacturer` and `ensure_computer_type` reuse `ensure_dropdown`.
- `TaxonomyStore(BaseModel)` groups the caches, analogous to `CacheStore`.

**Tests:** `TestNormalizeLookupKey`, `TestPrefetchDropdown`, `TestEnsureDropdown` (cache hit without HTTP, miss creates, dry-run without POST, API error), `TestEnsureStates`, `TestEnsureLocationTree` (same `Fila` name under another root), `TestEnsureComputerModel` and `TestPrecomputeModelHeights`.

**Docs:** new `docs/glpi_mapping.md` (`entity`, `location`, `states`, `computer_types` sections).

**Manual check:** a real run on the test instance creates the catalogs; a second run creates nothing.

### Phase 4. Computer synchronization (basic inventory MVP)

**Goal:** the production-ready basic inventory. Every row becomes a Computer with native fields.

- `ComputerIndex(NamedTuple)` with `by_uuid` and `by_name` (lists of `GlpiRecord`). `prefetch_computers(client, entity)` reads active and trashed Computers without templates; `build_computer_index(records)` is pure.
- `_resolve_by_uuid`, `_resolve_by_name` and `_resolve_existing_computer` replicate the NetBox semantics using `_is_identifier_unique` from the core.
- `_guard_ownership(record)` raises `RowSkipCondition` for dynamic or trashed Computers.
- `build_computer_payload(row, node_type, config, taxonomy) -> GlpiPayload`: uses the shared field-resolution stages and adds `entities_id`, `states_id`, `computertypes_id`, `locations_id`, and `manufacturers_id`/`computermodels_id` only when present. VMs do not require manufacturer or model.
- `compute_glpi_diff(record, payload) -> GlpiPayload` normalizes integer vs string ids and `None` vs absent. `apply_update`, `update_if_changed` and `create_record_or_mock` follow the NetBox pattern.
- `_execute_computer_sync(...) -> SyncResult`: CREATED, UPDATED or UNCHANGED; returns the updated index after each creation.
- `_sync_row` with the same exception-to-counter mapping as NetBox, plus `GlpiApiError`. Physical servers and hypervisors are processed before VMs.

**Tests:** `TestBuildComputerIndex`, `TestResolveExistingComputer` (unique UUID, UUID duplicated in CSV or GLPI falling back to name, name with another UUID skipped, creation), `TestGuardOwnership`, `TestBuildComputerPayload` (coalesce conflict, concatenated comment, lowercase UUID, VM without manufacturer), `TestComputeGlpiDiff`, `TestExecuteComputerSync` (create, update, unchanged, dry-run), `TestSyncRowGlpi` and `TestIdempotencyTwoRuns`, which uses an in-memory `FakeGlpiClient` and requires the second run to be all UNCHANGED.

**Docs:** `docs/architecture.md` (GLPI flow and identity), `docs/glpi_mapping.md` (`node_types`) and `README.md` (Quick Start and features).

**Acceptance criteria:** `--dry-run` over the real CSV without unexpected errors; a real run on the test instance followed by a second run with 0 CREATED and 0 UPDATED.

### Phase 5. Operating system

- `OperatingSystem`, `OperatingSystemVersion` and `OperatingSystemKernelVersion` dropdowns through `ensure_dropdown`.
- `prefetch_item_operating_systems` indexed by `items_id`, in a single paginated read filtered by `itemtype=Computer`.
- `sync_operating_system(...)`: creates when there is none, applies the diff when there is one, and warns without changes when there are several or the record is dynamic. An empty OS in the CSV is a no-op.

**Tests:** `TestSyncOperatingSystem` (create, version change, empty OS, ambiguity, dynamic, dry-run).

**Docs:** `docs/glpi_mapping.md`.

### Phase 6. Network ports, network names and IP addresses

- **Extracted to the core:** `NetworkInterfaceData`, `_parse_network_interfaces`, `_parse_single_network_interface`, `_build_interface_cidr`, `_split_ip_and_embedded_prefix`, `_validate_interface_ip`, `_network_from_cidr`, `_sanitize_mac_address` and `parse_row_interfaces`, with their tests moved to `tests/test_export_core.py`.
- `NetworkPort` prefetch (`itemtype=Computer`) indexed by `(items_id, normalized name)`.
- Upsert of `NetworkPort` (`instantiation_type=NetworkPortEthernet`, `name`, normalized `mac`), one `NetworkName` per port, and `IPAddress` under the `NetworkName`. Optional `IPNetwork` driven by `network.sync_ipnetworks` in the YAML.
- `--prune-network-orphans` moves the Computer's ports and IPs absent from the CSV to the trash, only when they are not dynamic.
- `enabled` is not mapped (ADR-08). GLPI does not enforce global IP uniqueness, so NetBox's "IP assigned elsewhere" logic does not apply.

**Tests:** `TestSyncNetworkPorts` (full chain creation, MAC normalization, IP change, removed interface with and without the flag, dynamic port not pruned, dry-run), `TestEnsureIpNetwork` and `TestNetworkDiscrepancy` (misaligned arrays raise a row error without affecting the Computer).

**Docs:** `docs/glpi_mapping.md` (network) and `docs/usage_and_config.md` (flag).

### Phase 7. Racks

- `parse_glpi_rack_position(value) -> int`: reuses the core `parse_rack_position` and requires an integer.
- `ensure_rack(...)` keyed by `(location_id, name)`, with a default `number_units` from the YAML `rack_defaults` (creation only).
- `Item_Rack` prefetch by `items_id`. `sync_item_rack(...)` creates or moves the placement (`racks_id`, `position`, front `orientation`, `hpos` 0).
- Fractional positions, zero heights or racks without position produce a `WARNING` and no placement.
- If GLPI rejects an occupied position, the rack sub-step counts one isolated ERROR; the Computer stays synchronized.
- An empty rack in the CSV never removes an existing placement, as in NetBox.

**Pre-check on the test instance:** U1 at the bottom and the `orientation` values.

**Tests:** `TestParseGlpiRackPosition`, `TestEnsureRack` and `TestSyncItemRack` (create, move, fractional position, zero height, API collision, dry-run).

### Phase 8. Virtualization and clusters

- **Extracted to the core:** `precompute_cluster_type_map` and its tests.
- `ClusterType` from `SO Hipervisor`, and `Cluster` by name with `version` from `Version SO Hipervisor` (only when consistent across rows). `Item_Cluster` for hypervisors only.
- New core cast `int_gib_to_mib` (×1024, see [Verified Assumptions](#verified-assumptions)).
- `sync_item_virtual_machine(...)`: the host is resolved by name through a host cache, like `host_devices` in NetBox. Fields: `itemtype=Computer`, `items_id=<host>`, `name`, `uuid`, `vcpu ← Cores`, `ram ← RAM (GB) × 1024`. Identity is the UUID, or `(host, name)` when the UUID is empty. A VM that moved to another host has its `items_id` updated instead of being duplicated. A missing host produces a `WARNING`.

**Tests:** `TestPrecomputeClusterTypeMap` (moved), `TestParseIntGibToMib`, `TestEnsureCluster`, `TestSyncItemCluster` and `TestSyncItemVirtualMachine` (create, host change, no host, empty UUID, dry-run).

**Manual check:** the host page shows the VM linked to the VM's own Computer.

### Phase 9. Warranty (Infocom)

- New `glpi_mapping.yaml` columns: `warranty_start` (`Fecha Inicio`) and `warranty_end` (`Fecha Vencimiento`).
- New cast `date_dmy` (DD-MM-YYYY to ISO) and the pure function `compute_warranty_months(start, end)`.
- 1:1 `Infocom` upsert with `warranty_date` and `warranty_duration`. When the duration is not a whole number of months, it is rounded and the exact end date is kept in `warranty_info`. An end date before the start date is an error of the warranty sub-step, not of the whole row.

**Tests:** `TestParseDateDmy` (valid dates, 29-02, invalid formats), `TestComputeWarrantyMonths` and `TestSyncInfocom` (create, update, Infocom auto-created by GLPI).

### Phase 10. Hardware components (optional, `components.enabled` in the YAML)

- `DeviceProcessor` (`designation ← CPU Modelo`) with N `Item_DeviceProcessor` instances according to `Sockets`; cores and threads are split per socket. When not divisible, a single instance carries the totals with a `WARNING`.
- A generic `DeviceMemory` with one `RAM (GB) × 1024` instance. An `Item_Disk` named "Capacidad visible" with `totalsize` in MiB.
- Reconciliation creates missing instances and removes extra ones, only among non-dynamic instances.

**Tests:** `TestBuildProcessorInstances` and `TestReconcileComponents`.

### Phase 11. Custom fields (Fields plugin, conditional)

- Runs only when the preflight detected the plugin and the YAML `plugin_fields.enabled` section is active. Enabled without the plugin aborts with `ConfigValidationError`; disabled logs `INFO`.
- **Containers are not created through the API.** Their definition (a Computer container with role, environment, Rundeck node, BIOS, RAID, disks, CPU model and hypervisor OS) is documented in `docs/glpi_mapping.md` for an administrator to create. The script validates containers and fields at startup (fail-fast).
- 1:1 upsert on the container itemtype (`PluginFields<Itemtype><container>`). Plugin dropdown values use get-or-create. Exact names are confirmed with `listSearchOptions` on the test instance, and the installed plugin version is checked for issue #1030.

**Tests:** `TestDetectFieldsPlugin`, `TestValidateFieldsContainers` and `TestSyncPluginFieldsValues`.

### Phase 12. Hardening and closure

- End-to-end test with a small fixture CSV and `FakeGlpiClient`: zero writes in dry-run, idempotency over two real runs.
- Full real run on the test instance measuring time and request count, followed by the first production `--dry-run`.
- Documentation consolidation: `docs/architecture.md` (GLPI branch in the `.drawio` diagram; the `.png` export is manual), `docs/components.md`, `docs/glpi_mapping.md`, `docs/usage_and_config.md`, `docs/development.md`, `README.md` (GLPI 11 badge, features, Quick Start, docs table, structure) and this roadmap (final status per phase).

---

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
| ---- | ---------- | ------ | ---------- |
| GLPI agents also inventory these servers | Low (per owner) | High | Detected by the preflight; dynamic assets are skipped (ADR-04) |
| Servers already entered manually under other names | Medium | Medium | Name adoption only when unique; review of the first `--dry-run` (Phase 0, step 6) |
| Future deprecation of `apirest.php` | Medium, long term | Medium | Isolated `GlpiClient` adapter (ADR-01) |
| Case/accent-insensitive collation creating duplicates | High if ignored | High | `normalize_lookup_key` on every index |
| GLPI silently ignoring unknown payload keys | High if ignored | Medium | Target allow-list in the YAML validator |
| NetBox regression caused by the refactor | Low | High | Same test count and `--dry-run` log `diff` (Phase 1) |
| Session expiry on long runs | Medium | Low | Automatic one-time renewal |
| GLPI unicity rules rejecting creations | Medium | Low | Row-level ERROR carrying the GLPI message; reviewed in Phase 0 |
| Fields plugin missing or faulty | Unknown | Low | Last phase, optional, validated at startup |
| State visibility per itemtype | Medium | Low | Verified on the test instance at the start of Phase 3 |

**Out-of-scope observation:** NetBox's `int_gb_to_mb` cast (×1000) receives GiB values. NetBox displays the same number as the CSV, but the stored MB value is about 2.4% low. This plan does not change it.

---

## Sources

- [GLPI REST API v1 (`apirest.md`, 11.0 branch)](https://github.com/glpi-project/glpi/blob/11.0/bugfixes/apirest.md)
- [RESTful API v2: OAuth2 grants and legacy API status](https://help.glpi-project.org/documentation/modules/configuration/general/api/restful-api-v2)
- [API v2 tutorial (password grant)](https://help.glpi-project.org/tutorials/readme-1/api-v2)
- [GLPI 11.0.0 SQL schema (`install/mysql/glpi-empty.sql`)](https://github.com/glpi-project/glpi/blob/11.0.0/install/mysql/glpi-empty.sql)
- [`Lockedfield.php`, GLPI 11.0.0](https://github.com/glpi-project/glpi/blob/11.0.0/src/Lockedfield.php)
- [GLPI forum: rack position through `Item_Rack`](https://forum.glpi-project.org/viewtopic.php?id=291509)
- [GLPI 11.0 upgrade guide (end of automatic sanitization)](https://glpi-developer-documentation.readthedocs.io/en/latest/upgradeguides/glpi-11.0.html)
- [Fields plugin compatibility](https://help.glpi-project.org/doc-plugins/fields) and [issue #1030](https://github.com/pluginsGLPI/fields/issues/1030)
- [Field locks (user documentation)](https://help.glpi-project.org/documentation/modules/assets/tabs/locks)
