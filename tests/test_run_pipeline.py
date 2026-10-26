"""Pruebas unitarias del orquestador run_pipeline (subprocess.run siempre mockeado)."""

import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from inventory_pipeline.paths import (
    DEFAULT_HEADER_LIST_PATH,
    DEFAULT_MAPPING_PATH,
    PROJECT_ROOT,
)
from inventory_pipeline.run_pipeline import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    MASTER_SUFFIXES,
    ArtifactPaths,
    PipelineError,
    PipelineOptions,
    PipelineStepError,
    build_apply_steps,
    build_artifact_paths,
    build_export_step,
    build_pre_export_steps,
    confirm_apply,
    discover_single_input,
    main,
    parse_cli_arguments,
    resolve_options,
    run_pipeline,
)

SUBPROCESS_RUN = "inventory_pipeline.run_pipeline.subprocess.run"
PKG = "inventory_pipeline"


def _completed(returncode: int) -> subprocess.CompletedProcess[bytes]:
    """Resultado simulado de un subproceso con el código de salida indicado."""
    return subprocess.CompletedProcess(args=[], returncode=returncode)


def _executed_modules(run_mock: MagicMock) -> list[str]:
    """Extrae, en orden, el módulo ejecutado en cada llamada a subprocess.run."""
    return [call.args[0][2] for call in run_mock.call_args_list]


@pytest.fixture
def input_files(tmp_path: Path) -> tuple[Path, Path]:
    """Un log del job y un maestro reales en disco (contenido irrelevante)."""
    job_log = tmp_path / "job_output.log"
    master = tmp_path / "maestro.ods"
    job_log.write_text("x", encoding="utf-8")
    master.write_bytes(b"x")
    return job_log, master


@pytest.fixture
def options(input_files: tuple[Path, Path]) -> PipelineOptions:
    """Opciones base: sin flags opcionales activados."""
    job_log, master = input_files
    return PipelineOptions(
        job_log=job_log,
        master=master,
        update_master=False,
        prune_network_orphans=False,
        assume_yes=False,
        verbose=False,
    )


@pytest.fixture
def artifacts(options: PipelineOptions, tmp_path: Path) -> ArtifactPaths:
    return build_artifact_paths(options.master, tmp_path / "output")


@pytest.fixture
def run_mock() -> Iterator[MagicMock]:
    """subprocess.run mockeado; por defecto todas las etapas terminan con 0."""
    with patch(SUBPROCESS_RUN, return_value=_completed(0)) as mock:
        yield mock


# ============================================================
# DESCUBRIMIENTO DE ENTRADAS
# ============================================================


class TestDiscoverSingleInput:
    """Valida la autodetección de entradas en input/."""

    def test_ignores_hidden_files_locks_and_other_extensions(
        self, tmp_path: Path
    ) -> None:
        # Escenario real: el maestro abierto en LibreOffice deja un lock
        # ".~lock.<nombre>#" junto a él; no debe contar como segundo candidato.
        (tmp_path / ".gitkeep").touch()
        (tmp_path / ".~lock.maestro.ods#").touch()
        (tmp_path / "notas.txt").touch()
        master = tmp_path / "maestro.ods"
        master.touch()

        found = discover_single_input(
            tmp_path, MASTER_SUFFIXES, "inventario maestro", "--master"
        )

        assert found == master

    def test_suffix_match_is_case_insensitive(self, tmp_path: Path) -> None:
        master = tmp_path / "MAESTRO.XLSX"
        master.touch()
        assert (
            discover_single_input(tmp_path, MASTER_SUFFIXES, "maestro", "--master")
            == master
        )

    def test_no_candidates_fails_with_override_hint(self, tmp_path: Path) -> None:
        (tmp_path / ".gitkeep").touch()
        with pytest.raises(PipelineError, match="No se encontró ningún.*--master"):
            discover_single_input(tmp_path, MASTER_SUFFIXES, "maestro", "--master")

    def test_multiple_candidates_fail_listing_all_of_them(self, tmp_path: Path) -> None:
        # Elegir uno arbitrariamente podría procesar un maestro desactualizado.
        (tmp_path / "viejo.ods").touch()
        (tmp_path / "nuevo.xlsx").touch()
        with pytest.raises(PipelineError, match="más de un") as exc_info:
            discover_single_input(tmp_path, MASTER_SUFFIXES, "maestro", "--master")
        assert "viejo.ods" in str(exc_info.value)
        assert "nuevo.xlsx" in str(exc_info.value)

    def test_missing_directory_fails(self, tmp_path: Path) -> None:
        with pytest.raises(PipelineError, match="No existe el directorio"):
            discover_single_input(
                tmp_path / "no_existe", MASTER_SUFFIXES, "maestro", "--master"
            )


class TestResolveOptions:
    """Valida la resolución de entradas explícitas o autodetectadas."""

    def test_autodetects_both_inputs(self, tmp_path: Path) -> None:
        job_log = tmp_path / "salida.log"
        master = tmp_path / "maestro.xlsx"
        job_log.touch()
        master.touch()

        options = resolve_options(parse_cli_arguments([]), tmp_path)

        assert (options.job_log, options.master) == (job_log, master)

    def test_explicit_paths_skip_discovery(
        self, tmp_path: Path, input_files: tuple[Path, Path]
    ) -> None:
        job_log, master = input_files
        args = parse_cli_arguments(["--job-log", str(job_log), "--master", str(master)])

        # El directorio de entradas no existe: si se intentara autodetectar, fallaría.
        options = resolve_options(args, tmp_path / "input_inexistente")

        assert (options.job_log, options.master) == (job_log, master)

    def test_missing_explicit_file_fails_before_running_anything(
        self, tmp_path: Path, input_files: tuple[Path, Path]
    ) -> None:
        job_log, _ = input_files
        args = parse_cli_arguments(
            ["--job-log", str(job_log), "--master", str(tmp_path / "falta.ods")]
        )
        with pytest.raises(PipelineError, match="No se encontró el inventario maestro"):
            resolve_options(args, tmp_path)

    def test_relative_explicit_paths_are_made_absolute(
        self,
        tmp_path: Path,
        input_files: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Las etapas corren con cwd=PROJECT_ROOT: una ruta relativa al directorio
        # del usuario apuntaría a otro archivo (o a ninguno) dentro del subproceso.
        job_log, master = input_files
        monkeypatch.chdir(tmp_path)
        args = parse_cli_arguments(["--job-log", job_log.name, "--master", master.name])

        options = resolve_options(args, tmp_path)

        assert options.job_log == job_log.resolve()
        assert options.master == master.resolve()
        assert options.job_log.is_absolute()
        assert options.master.is_absolute()

    def test_explicit_master_with_unsupported_extension_fails_fast(
        self, tmp_path: Path, input_files: tuple[Path, Path]
    ) -> None:
        # Sin esta validación, el parseo correría y el error recién aparecería
        # en prepare_master_inventory (paso 2).
        job_log, _ = input_files
        csv_master = tmp_path / "maestro.csv"
        csv_master.touch()
        args = parse_cli_arguments(
            ["--job-log", str(job_log), "--master", str(csv_master)]
        )
        with pytest.raises(PipelineError, match="extensión"):
            resolve_options(args, tmp_path)

    def test_flags_are_mapped(self, input_files: tuple[Path, Path]) -> None:
        job_log, master = input_files
        args = parse_cli_arguments(
            [
                "--job-log",
                str(job_log),
                "--master",
                str(master),
                "--update-master",
                "--prune-network-orphans",
                "-y",
                "--verbose",
            ]
        )
        options = resolve_options(args, master.parent)
        assert options.update_master
        assert options.prune_network_orphans
        assert options.assume_yes
        assert options.verbose


# ============================================================
# CONSTRUCCIÓN DE ETAPAS
# ============================================================


class TestBuildSteps:
    """Valida que cada etapa reciba como entrada la salida de la anterior."""

    def test_pre_export_steps_are_chained(
        self, options: PipelineOptions, artifacts: ArtifactPaths
    ) -> None:
        parse, prepare, merge = build_pre_export_steps(options, artifacts)
        header_list = str(DEFAULT_HEADER_LIST_PATH)

        assert parse.module == f"{PKG}.parse_job_output"
        assert parse.args == (
            str(options.job_log),
            str(artifacts.parsed_csv),
            header_list,
        )
        assert prepare.module == f"{PKG}.prepare_master_inventory"
        assert prepare.args == (
            str(options.master),
            str(artifacts.prepared_csv),
            header_list,
        )
        assert merge.module == f"{PKG}.merge_inventories"
        assert merge.args == (
            "--parsed",
            str(artifacts.parsed_csv),
            "--master",
            str(artifacts.prepared_csv),
            "--output",
            str(artifacts.merged_csv),
            "--header-list",
            header_list,
        )

    def test_dry_run_flag_only_on_dry_run_export(
        self, options: PipelineOptions, artifacts: ArtifactPaths
    ) -> None:
        dry = build_export_step(options, artifacts, dry_run=True)
        real = build_export_step(options, artifacts, dry_run=False)

        assert dry.args == (
            str(artifacts.merged_csv),
            str(DEFAULT_MAPPING_PATH),
            "--dry-run",
        )
        assert "--dry-run" not in real.args

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_optional_flags_reach_both_exports(
        self, options: PipelineOptions, artifacts: ArtifactPaths, dry_run: bool
    ) -> None:
        # Si el prune solo llegara al export real, el dry-run no mostraría
        # las eliminaciones que luego se aplican.
        flagged = replace(options, prune_network_orphans=True, verbose=True)
        step = build_export_step(flagged, artifacts, dry_run=dry_run)
        assert "--prune-network-orphans" in step.args
        assert "--verbose" in step.args

    def test_apply_steps_without_update_master(
        self, options: PipelineOptions, artifacts: ArtifactPaths
    ) -> None:
        steps = build_apply_steps(options, artifacts)
        assert [step.module for step in steps] == [f"{PKG}.export_to_netbox"]

    def test_apply_steps_with_update_master_writes_copy_in_output(
        self, options: PipelineOptions, artifacts: ArtifactPaths
    ) -> None:
        steps = build_apply_steps(replace(options, update_master=True), artifacts)

        assert [step.module for step in steps] == [
            f"{PKG}.export_to_netbox",
            f"{PKG}.update_master_inventory",
        ]
        assert steps[1].args == (
            "--merged",
            str(artifacts.merged_csv),
            "--master",
            str(options.master),
            "--output",
            str(artifacts.updated_master),
        )
        # El maestro original (input/) nunca es el destino de la escritura.
        assert artifacts.updated_master.name == "maestro_updated.ods"
        assert artifacts.updated_master.parent != options.master.parent


# ============================================================
# CONFIRMACIÓN
# ============================================================


class TestConfirmApply:
    """Valida la confirmación entre el dry-run y el export real."""

    def test_assume_yes_never_prompts(self) -> None:
        with patch("builtins.input") as input_mock:
            assert confirm_apply(assume_yes=True, is_interactive=False)
        input_mock.assert_not_called()

    def test_non_interactive_without_yes_aborts(self) -> None:
        # Sin TTY, asumir una respuesta escribiría en NetBox sin querer
        # o colgaría la ejecución esperando una entrada que nunca llega.
        with pytest.raises(PipelineError, match="--yes"):
            confirm_apply(assume_yes=False, is_interactive=False)

    @pytest.mark.parametrize(
        "answer, expected",
        [
            ("s", True),
            (" Sí ", True),
            ("YES", True),
            ("", False),
            ("n", False),
            ("no", False),
            ("quizás", False),
        ],
    )
    def test_interactive_answer(self, answer: str, expected: bool) -> None:
        with patch("builtins.input", return_value=answer):
            assert confirm_apply(assume_yes=False, is_interactive=True) is expected

    def test_closed_stdin_counts_as_rejection(self) -> None:
        with patch("builtins.input", side_effect=EOFError):
            assert not confirm_apply(assume_yes=False, is_interactive=True)


# ============================================================
# EJECUCIÓN DEL PIPELINE
# ============================================================


class TestRunPipeline:
    """Valida el orden de ejecución, el fail-fast y la confirmación."""

    def test_full_run_executes_every_step_in_order(
        self, options: PipelineOptions, tmp_path: Path, run_mock: MagicMock
    ) -> None:
        full = replace(options, update_master=True, assume_yes=True)

        applied = run_pipeline(full, tmp_path / "output", is_interactive=False)

        assert applied
        assert _executed_modules(run_mock) == [
            f"{PKG}.parse_job_output",
            f"{PKG}.prepare_master_inventory",
            f"{PKG}.merge_inventories",
            f"{PKG}.export_to_netbox",
            f"{PKG}.export_to_netbox",
            f"{PKG}.update_master_inventory",
        ]
        first_export, second_export = run_mock.call_args_list[3:5]
        assert "--dry-run" in first_export.args[0]
        assert "--dry-run" not in second_export.args[0]

    def test_steps_run_with_same_interpreter_from_project_root(
        self, options: PipelineOptions, tmp_path: Path, run_mock: MagicMock
    ) -> None:
        run_pipeline(
            replace(options, assume_yes=True), tmp_path / "output", is_interactive=False
        )
        for call in run_mock.call_args_list:
            assert call.args[0][:2] == [sys.executable, "-m"]
            assert call.kwargs["cwd"] == PROJECT_ROOT

    def test_first_failure_stops_the_pipeline(
        self, options: PipelineOptions, tmp_path: Path, run_mock: MagicMock
    ) -> None:
        # prepare_master_inventory falla: merge y los exports no deben ejecutarse.
        run_mock.side_effect = [_completed(0), _completed(3)]

        unattended = replace(options, assume_yes=True)
        output_dir = tmp_path / "output"

        with pytest.raises(PipelineStepError) as exc_info:
            run_pipeline(unattended, output_dir, is_interactive=False)

        assert exc_info.value.returncode == 3
        assert exc_info.value.step.module == f"{PKG}.prepare_master_inventory"
        assert run_mock.call_count == 2

    def test_failed_dry_run_never_reaches_confirmation(
        self, options: PipelineOptions, tmp_path: Path, run_mock: MagicMock
    ) -> None:
        run_mock.side_effect = [_completed(0)] * 3 + [_completed(1)]
        output_dir = tmp_path / "output"

        with (
            patch("builtins.input") as input_mock,
            pytest.raises(PipelineStepError),
        ):
            run_pipeline(options, output_dir, is_interactive=True)

        input_mock.assert_not_called()
        assert run_mock.call_count == 4

    def test_rejection_skips_real_export_and_update_master(
        self, options: PipelineOptions, tmp_path: Path, run_mock: MagicMock
    ) -> None:
        with patch("builtins.input", return_value="n"):
            applied = run_pipeline(
                replace(options, update_master=True),
                tmp_path / "output",
                is_interactive=True,
            )

        assert not applied
        assert run_mock.call_count == 4

    def test_update_master_does_not_run_if_real_export_fails(
        self, options: PipelineOptions, tmp_path: Path, run_mock: MagicMock
    ) -> None:
        # La copia del maestro solo debe reflejar un ciclo consistente con NetBox.
        run_mock.side_effect = [_completed(0)] * 4 + [_completed(1)]

        unattended = replace(options, update_master=True, assume_yes=True)
        output_dir = tmp_path / "output"

        with pytest.raises(PipelineStepError):
            run_pipeline(unattended, output_dir, is_interactive=False)

        assert f"{PKG}.update_master_inventory" not in _executed_modules(run_mock)


class TestMain:
    """Valida los códigos de salida del comando inventory-pipeline."""

    @staticmethod
    def _argv(input_files: tuple[Path, Path], *extra: str) -> list[str]:
        job_log, master = input_files
        return ["--job-log", str(job_log), "--master", str(master), "--yes", *extra]

    @pytest.fixture(autouse=True)
    def _isolated_output_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # main() crea el directorio de salida: se redirige para no tocar output/.
        monkeypatch.setattr(
            "inventory_pipeline.run_pipeline.OUTPUT_DIR", tmp_path / "output"
        )

    def test_success_returns_zero(
        self, input_files: tuple[Path, Path], run_mock: MagicMock
    ) -> None:
        assert main(self._argv(input_files)) == 0
        assert run_mock.call_count == 5

    def test_propagates_exit_code_of_failed_step(
        self, input_files: tuple[Path, Path], run_mock: MagicMock
    ) -> None:
        run_mock.side_effect = [_completed(0), _completed(0), _completed(2)]
        assert main(self._argv(input_files)) == 2

    def test_signal_terminated_step_maps_to_shell_convention(
        self, input_files: tuple[Path, Path], run_mock: MagicMock
    ) -> None:
        # subprocess reporta -N si la etapa murió por la señal N (ej. SIGKILL=9);
        # un exit status negativo no es válido para el proceso del orquestador.
        run_mock.return_value = _completed(-9)
        assert main(self._argv(input_files)) == 137

    def test_missing_inputs_return_failure_without_running_steps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_mock: MagicMock
    ) -> None:
        empty_input = tmp_path / "input"
        empty_input.mkdir()
        monkeypatch.setattr("inventory_pipeline.run_pipeline.INPUT_DIR", empty_input)

        assert main(["--yes"]) == EXIT_FAILURE
        run_mock.assert_not_called()

    def test_keyboard_interrupt_returns_130(
        self, input_files: tuple[Path, Path], run_mock: MagicMock
    ) -> None:
        run_mock.side_effect = KeyboardInterrupt
        assert main(self._argv(input_files)) == EXIT_INTERRUPTED

    def test_non_interactive_without_yes_fails_before_any_step(
        self, input_files: tuple[Path, Path], run_mock: MagicMock
    ) -> None:
        # La falta de confirmación se conoce desde el inicio: no tiene sentido
        # ejecutar parseo, fusión y un dry-run contra NetBox para luego abortar.
        job_log, master = input_files
        with patch("inventory_pipeline.run_pipeline.sys.stdin") as stdin_mock:
            stdin_mock.isatty.return_value = False
            code = main(["--job-log", str(job_log), "--master", str(master)])

        assert code == EXIT_FAILURE
        run_mock.assert_not_called()
