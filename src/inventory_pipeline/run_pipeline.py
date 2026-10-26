"""
run_pipeline.py
===============
Orquestador del pipeline completo. Encadena todas las etapas en un único
comando y se detiene ante la primera que termine con código de salida
distinto de 0:

    1. parse_job_output          input/<job>.log          → output/parsed_job_output.csv
    2. prepare_master_inventory  input/<maestro>.ods|xlsx → output/prepared_master_inventory.csv
    3. merge_inventories         ambos CSV                → output/merged_inventory.csv
    4. export_to_netbox --dry-run
       (confirmación interactiva, u omitida con --yes)
    5. export_to_netbox          aplica los cambios en NetBox
    6. update_master_inventory   solo con --update-master → output/<maestro>_updated.<ext>

Las entradas se autodetectan en input/: debe existir exactamente un *.log y
exactamente un *.ods/*.xlsx (los archivos ocultos, como .gitkeep o los locks
de LibreOffice, se ignoran). --job-log y --master permiten indicarlas
explícitamente.

Cada etapa se ejecuta como subproceso (`python -m inventory_pipeline.<módulo>`)
en lugar de importarse: las etapas terminan con sys.exit() ante errores y
export_to_netbox configura logging a nivel global, por lo que aislarlas
garantiza que cada una se comporte exactamente igual que al ejecutarla a
mano, y que su código de salida sea la única señal de éxito o fallo.

La copia actualizada del maestro se genera al final y solo si el export real
terminó bien, para que refleje un ciclo completo y consistente con NetBox.

Uso:
    inventory-pipeline [--job-log RUTA] [--master RUTA] [--update-master]
                       [--prune-network-orphans] [--yes] [--verbose]

Códigos de salida:
    0   → pipeline completo, o cambios no aplicados por decisión del usuario
    1   → error del orquestador (entradas ausentes o ambiguas, confirmación
          imposible sin terminal interactiva)
    N   → código de salida de la primera etapa que falló
    130 → interrumpido por el usuario (Ctrl+C)
"""

import argparse
import logging
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NamedTuple

from inventory_pipeline.paths import (
    DEFAULT_HEADER_LIST_PATH,
    DEFAULT_MAPPING_PATH,
    INPUT_DIR,
    MERGED_CSV_NAME,
    OUTPUT_DIR,
    PARSED_CSV_NAME,
    PREPARED_CSV_NAME,
    PROJECT_ROOT,
)
from inventory_pipeline.update_master_inventory import SUPPORTED_EXTENSIONS

# ============================================================
# LOGGER
# ============================================================

logging.basicConfig(
    format="%(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stderr,
)
log = logging.getLogger("run_pipeline")

# ============================================================
# CONSTANTES
# ============================================================

PACKAGE: Final[str] = "inventory_pipeline"

JOB_LOG_SUFFIXES: Final[frozenset[str]] = frozenset({".log"})
MASTER_SUFFIXES: Final[frozenset[str]] = frozenset(SUPPORTED_EXTENSIONS)

AFFIRMATIVE_ANSWERS: Final[frozenset[str]] = frozenset({"s", "si", "sí", "y", "yes"})

EXIT_SUCCESS: Final[int] = 0
EXIT_FAILURE: Final[int] = 1
EXIT_INTERRUPTED: Final[int] = 130
SIGNAL_EXIT_BASE: Final[int] = 128

# ============================================================
# ESTRUCTURAS DE DATOS
# ============================================================


class PipelineError(Exception):
    """Error propio del orquestador, previo o ajeno a la ejecución de una etapa."""


class PipelineStep(NamedTuple):
    """Etapa del pipeline: un módulo del paquete y sus argumentos de línea de comandos."""

    name: str
    module: str
    args: tuple[str, ...]


class PipelineStepError(Exception):
    """Una etapa terminó con código de salida distinto de 0."""

    def __init__(self, step: PipelineStep, returncode: int) -> None:
        super().__init__(
            f"La etapa '{step.name}' terminó con código de salida {returncode}."
        )
        self.step = step
        self.returncode = returncode


@dataclass(frozen=True)
class PipelineOptions:
    """Opciones resueltas del pipeline, con las rutas de entrada ya validadas."""

    job_log: Path
    master: Path
    update_master: bool
    prune_network_orphans: bool
    assume_yes: bool
    verbose: bool


class ArtifactPaths(NamedTuple):
    """Rutas de los artefactos que genera cada etapa."""

    parsed_csv: Path
    prepared_csv: Path
    merged_csv: Path
    updated_master: Path


# ============================================================
# ARGUMENTOS Y RESOLUCIÓN DE ENTRADAS
# ============================================================


def parse_cli_arguments(argv: Sequence[str]) -> argparse.Namespace:
    """Interpreta los argumentos de línea de comandos (sin el nombre del programa)."""
    parser = argparse.ArgumentParser(
        prog="inventory-pipeline",
        description=(
            "Ejecuta el pipeline completo: parseo, preparación del maestro, "
            "fusión, export a NetBox (dry-run y real) y, opcionalmente, "
            "actualización del maestro."
        ),
    )
    parser.add_argument(
        "--job-log",
        type=Path,
        default=None,
        help="Salida cruda del job de Rundeck (por defecto: el único *.log en input/).",
    )
    parser.add_argument(
        "--master",
        type=Path,
        default=None,
        help="Inventario maestro .ods/.xlsx (por defecto: el único en input/).",
    )
    parser.add_argument(
        "--update-master",
        action="store_true",
        help=(
            "Al final, genera output/<maestro>_updated.<ext> con el inventario "
            "fusionado (solo si el export real terminó bien)."
        ),
    )
    parser.add_argument(
        "--prune-network-orphans",
        action="store_true",
        help="Se propaga a ambos exports (dry-run y real). Ver export_to_netbox.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Aplica los cambios en NetBox sin pedir confirmación tras el dry-run.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Activa logs DEBUG en el orquestador y en export_to_netbox.",
    )
    return parser.parse_args(argv)


def _is_candidate_input(path: Path, suffixes: frozenset[str]) -> bool:
    """Indica si un archivo de input/ puede ser una entrada del pipeline.

    Los archivos ocultos se descartan: cubren .gitkeep y los locks que
    LibreOffice crea mientras el maestro está abierto (.~lock.<nombre>#).
    """
    return (
        path.is_file()
        and not path.name.startswith(".")
        and path.suffix.lower() in suffixes
    )


def discover_single_input(
    directory: Path,
    suffixes: frozenset[str],
    label: str,
    override_flag: str,
) -> Path:
    """
    Busca en `directory` el único archivo con alguna de las extensiones dadas.

    Falla si no hay ninguno o si hay más de uno: elegir uno arbitrariamente
    podría procesar un inventario desactualizado sin aviso.
    """
    if not directory.is_dir():
        raise PipelineError(f"No existe el directorio de entradas: {directory}")

    candidates = sorted(
        path for path in directory.iterdir() if _is_candidate_input(path, suffixes)
    )
    expected = ", ".join(sorted(suffixes))

    if not candidates:
        raise PipelineError(
            f"No se encontró ningún {label} ({expected}) en {directory}. "
            f"Cópialo allí o indícalo con {override_flag}."
        )
    if len(candidates) > 1:
        listed = "\n".join(f"  - {path.name}" for path in candidates)
        raise PipelineError(
            f"Se encontró más de un {label} ({expected}) en {directory}. "
            f"Deja solo uno o indícalo con {override_flag}:\n{listed}"
        )
    return candidates[0]


def _require_file(
    path: Path, label: str, suffixes: frozenset[str] | None = None
) -> Path:
    """
    Valida una entrada indicada explícitamente (fail-fast) y la retorna absoluta.

    La ruta se resuelve contra el directorio actual del usuario porque las
    etapas corren con cwd=PROJECT_ROOT, donde una ruta relativa apuntaría a
    otro archivo. Si se indican `suffixes`, también se valida la extensión,
    para no ejecutar etapas previas antes de que la etapa que la exige falle.
    """
    if not path.is_file():
        raise PipelineError(f"No se encontró el {label}: {path}")
    if suffixes is not None and path.suffix.lower() not in suffixes:
        expected = ", ".join(sorted(suffixes))
        raise PipelineError(f"El {label} debe tener extensión {expected}: {path}")
    return path.resolve()


def resolve_options(args: argparse.Namespace, input_dir: Path) -> PipelineOptions:
    """
    Resuelve las entradas (explícitas o autodetectadas en `input_dir`) antes de
    ejecutar cualquier etapa, para no dejar el pipeline a medias por una
    entrada faltante descubierta tarde.
    """
    job_log: Path = (
        _require_file(args.job_log, "log del job de Rundeck")
        if args.job_log is not None
        else discover_single_input(
            input_dir, JOB_LOG_SUFFIXES, "log del job de Rundeck", "--job-log"
        )
    )
    master: Path = (
        _require_file(args.master, "inventario maestro", MASTER_SUFFIXES)
        if args.master is not None
        else discover_single_input(
            input_dir, MASTER_SUFFIXES, "inventario maestro", "--master"
        )
    )
    return PipelineOptions(
        job_log=job_log,
        master=master,
        update_master=args.update_master,
        prune_network_orphans=args.prune_network_orphans,
        assume_yes=args.yes,
        verbose=args.verbose,
    )


# ============================================================
# CONSTRUCCIÓN DE ETAPAS
# ============================================================


def build_artifact_paths(master: Path, output_dir: Path) -> ArtifactPaths:
    """Calcula las rutas de los artefactos generados, todos dentro de `output_dir`."""
    return ArtifactPaths(
        parsed_csv=output_dir / PARSED_CSV_NAME,
        prepared_csv=output_dir / PREPARED_CSV_NAME,
        merged_csv=output_dir / MERGED_CSV_NAME,
        updated_master=output_dir / f"{master.stem}_updated{master.suffix}",
    )


def build_pre_export_steps(
    options: PipelineOptions, artifacts: ArtifactPaths
) -> tuple[PipelineStep, ...]:
    """Etapas locales que producen merged_inventory.csv."""
    header_list = str(DEFAULT_HEADER_LIST_PATH)
    return (
        PipelineStep(
            name="Parseo de la salida de Rundeck",
            module=f"{PACKAGE}.parse_job_output",
            args=(str(options.job_log), str(artifacts.parsed_csv), header_list),
        ),
        PipelineStep(
            name="Preparación del inventario maestro",
            module=f"{PACKAGE}.prepare_master_inventory",
            args=(str(options.master), str(artifacts.prepared_csv), header_list),
        ),
        PipelineStep(
            name="Fusión de inventarios",
            module=f"{PACKAGE}.merge_inventories",
            args=(
                "--parsed",
                str(artifacts.parsed_csv),
                "--master",
                str(artifacts.prepared_csv),
                "--output",
                str(artifacts.merged_csv),
                "--header-list",
                header_list,
            ),
        ),
    )


def build_export_step(
    options: PipelineOptions, artifacts: ArtifactPaths, dry_run: bool
) -> PipelineStep:
    """Etapa de exportación a NetBox, en modo dry-run o real."""
    args = [str(artifacts.merged_csv), str(DEFAULT_MAPPING_PATH)]
    if dry_run:
        args.append("--dry-run")
    if options.prune_network_orphans:
        args.append("--prune-network-orphans")
    if options.verbose:
        args.append("--verbose")

    return PipelineStep(
        name="Exportación a NetBox (dry-run)" if dry_run else "Exportación a NetBox",
        module=f"{PACKAGE}.export_to_netbox",
        args=tuple(args),
    )


def build_update_master_step(artifacts: ArtifactPaths, master: Path) -> PipelineStep:
    """Etapa que deposita el inventario fusionado sobre una copia del maestro."""
    return PipelineStep(
        name="Actualización del inventario maestro",
        module=f"{PACKAGE}.update_master_inventory",
        args=(
            "--merged",
            str(artifacts.merged_csv),
            "--master",
            str(master),
            "--output",
            str(artifacts.updated_master),
        ),
    )


def build_apply_steps(
    options: PipelineOptions, artifacts: ArtifactPaths
) -> tuple[PipelineStep, ...]:
    """Etapas que solo se ejecutan tras confirmar los cambios del dry-run."""
    steps = [build_export_step(options, artifacts, dry_run=False)]
    if options.update_master:
        steps.append(build_update_master_step(artifacts, options.master))
    return tuple(steps)


# ============================================================
# EJECUCIÓN
# ============================================================


def build_command(step: PipelineStep) -> list[str]:
    """Comando del subproceso, con el mismo intérprete (y venv) del orquestador."""
    return [sys.executable, "-m", step.module, *step.args]


def _normalize_exit_code(returncode: int) -> int:
    """
    Traduce un código negativo de subprocess (proceso terminado por la señal N)
    a la convención de shell 128 + N; un código negativo no es un exit status
    válido para el proceso del orquestador.
    """
    if returncode < 0:
        return SIGNAL_EXIT_BASE + abs(returncode)
    return returncode


def run_step(step: PipelineStep, position: int, total: int) -> None:
    """Ejecuta una etapa y lanza PipelineStepError si no termina con código 0."""
    command = build_command(step)
    log.info("[%d/%d] Iniciando: %s", position, total, step.name)
    log.debug("Comando: %s", " ".join(command))

    started = time.monotonic()
    result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    elapsed = time.monotonic() - started

    if result.returncode != 0:
        raise PipelineStepError(step, _normalize_exit_code(result.returncode))

    log.info("[%d/%d] Completada: %s (%.1f s)", position, total, step.name, elapsed)


def run_steps(steps: Sequence[PipelineStep], first_position: int, total: int) -> None:
    """Ejecuta las etapas en orden; la primera que falle detiene la secuencia."""
    for offset, step in enumerate(steps):
        run_step(step, first_position + offset, total)


def ensure_confirmation_possible(assume_yes: bool, is_interactive: bool) -> None:
    """
    Falla si la confirmación posterior al dry-run no podrá obtenerse.

    Sin terminal interactiva y sin --yes se aborta en lugar de asumir una
    respuesta: aplicar sin querer escribiría en NetBox, y esperar una
    respuesta que nunca llega colgaría la ejecución. Se evalúa antes de la
    primera etapa porque la condición se conoce desde el inicio.
    """
    if assume_yes or is_interactive:
        return
    raise PipelineError(
        "Se requiere confirmación para aplicar los cambios en NetBox, pero la "
        "entrada estándar no es interactiva. Usa --yes para ejecuciones "
        "desatendidas."
    )


def confirm_apply(assume_yes: bool, is_interactive: bool) -> bool:
    """Decide si se aplican en NetBox los cambios revisados en el dry-run."""
    ensure_confirmation_possible(assume_yes, is_interactive)

    if assume_yes:
        log.info("Confirmación omitida (--yes): se aplicarán los cambios.")
        return True

    try:
        answer = input("¿Aplicar los cambios en NetBox? [s/N]: ")
    except EOFError:
        return False
    return answer.strip().lower() in AFFIRMATIVE_ANSWERS


def run_pipeline(
    options: PipelineOptions, output_dir: Path, is_interactive: bool
) -> bool:
    """
    Ejecuta el pipeline completo.

    Retorna True si los cambios se aplicaron en NetBox, o False si el usuario
    los rechazó tras el dry-run. Lanza PipelineStepError ante la primera
    etapa fallida.
    """
    ensure_confirmation_possible(options.assume_yes, is_interactive)

    artifacts = build_artifact_paths(options.master, output_dir)
    validation_steps = (
        *build_pre_export_steps(options, artifacts),
        build_export_step(options, artifacts, dry_run=True),
    )
    apply_steps = build_apply_steps(options, artifacts)
    total = len(validation_steps) + len(apply_steps)

    log.info("Log del job de Rundeck : %s", options.job_log)
    log.info("Inventario maestro     : %s", options.master)
    log.info("Directorio de salida   : %s", output_dir)

    run_steps(validation_steps, first_position=1, total=total)

    if not confirm_apply(options.assume_yes, is_interactive):
        return False

    run_steps(apply_steps, first_position=len(validation_steps) + 1, total=total)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada del comando `inventory-pipeline`. Retorna el código de salida."""
    args = parse_cli_arguments(sys.argv[1:] if argv is None else argv)
    if args.verbose:
        log.setLevel(logging.DEBUG)

    try:
        options = resolve_options(args, INPUT_DIR)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        applied = run_pipeline(options, OUTPUT_DIR, sys.stdin.isatty())
    except PipelineError as exc:
        log.error("%s", exc)
        return EXIT_FAILURE
    except PipelineStepError as exc:
        log.error("PIPELINE ABORTADO: %s Las etapas siguientes no se ejecutaron.", exc)
        return exc.returncode
    except KeyboardInterrupt:
        log.error("Pipeline interrumpido por el usuario.")
        return EXIT_INTERRUPTED

    if not applied:
        log.warning(
            "Cambios NO aplicados en NetBox por decisión del usuario; "
            "las etapas posteriores al dry-run no se ejecutaron."
        )
        return EXIT_SUCCESS

    log.info("Pipeline completado correctamente. Artefactos en: %s", OUTPUT_DIR)
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
