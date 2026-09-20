from __future__ import annotations

import getpass
import os
from pathlib import Path
from uuid import UUID

import typer
from dotenv import load_dotenv

from migration_hub.adapters.irp.client import IrpAdapter
from migration_hub.config.settings import Settings
from migration_hub.core.engine import create_registry_engine
from migration_hub.core.registry import Registry
from migration_hub.core.states import BatchState, FileState
from migration_hub.observability import controls
from migration_hub.orchestration import batches, reaper, reconciliation, scheduler
from migration_hub.producers import scanner, stager, validator

load_dotenv()

app = typer.Typer(help=__doc__)

def _load_settings() -> Settings:
    environment = os.environ.get("MIGRATION_HUB_ENV", "dev")
    settings = Settings.load(environment=environment, config_dir=Path("config"))
    typer.secho(f"environment: {settings.environment}", fg=typer.colors.YELLOW, bold=True)
    return settings

def _registry(settings: Settings) -> Registry:
    engine = create_registry_engine(settings.database_url)
    return Registry(engine)

def _adapter(settings: Settings) -> IrpAdapter:
    engine = create_registry_engine(settings.database_url)
    return IrpAdapter(
        host=settings.api_host,
        api_key=settings.api_key(),
        entitlement=settings.entitlement,
        engine=engine,
    )

@app.command()
def plan(
    batch: str = typer.Option(..., "--batch"),
    source: Path | None = typer.Option(None, "--source"),
    max_files: int | None = typer.Option(None, "--max-files"),
) -> None:
    settings = _load_settings()
    registry = _registry(settings)
    source_root = source or settings.source_root
    count = scanner.register(
        registry=registry,
        source_root=source_root,
        batch_id=batch,
        pattern=settings.file_pattern,
        max_files=max_files,
        max_concurrency=settings.max_concurrent_uploads,
    )
    typer.echo(f"registered {count} new file(s) into batch {batch!r} from {source_root}")

@app.command()
def validate(batch: str = typer.Option(..., "--batch")) -> None:
    settings = _load_settings()
    registry = _registry(settings)
    results = validator.validate_batch(
        registry=registry, batch_id=batch, compute_checksum=settings.compute_checksums
    )
    ok = sum(1 for r in results.values() if r.ok)
    typer.echo(f"validated {len(results)} file(s): {ok} VALIDATED, {len(results) - ok} REJECTED")
    for file_id, result in results.items():
        if not result.ok:
            typer.echo(f"  file_id={file_id}: {'; '.join(result.failures)}")

@app.command()
def stage(batch: str = typer.Option(..., "--batch")) -> None:
    settings = _load_settings()
    registry = _registry(settings)
    staged = stager.stage_batch(
        registry=registry,
        staging_root=settings.staging_root,
        batch_id=batch,
        stage_locally=settings.stage_locally,
    )
    where = "into staging" if settings.stage_locally else "in place (no copy)"
    typer.echo(f"staged {staged} file(s) {where} for batch {batch!r}")

@app.command()
def run(
    batch: str = typer.Option(..., "--batch"),
    max_files: int | None = typer.Option(None, "--max-files"),
) -> None:
    settings = _load_settings()
    registry = _registry(settings)

    registry.ensure_batch(batch_id=batch, max_concurrency=settings.max_concurrent_uploads)
    if (
        not settings.dry_run
        and batches.state_of(registry=registry, batch_id=batch) is not BatchState.PAUSED
    ):
        batches.resume(registry=registry, batch_id=batch)

    adapter = _adapter(settings)
    try:
        outcomes = batches.run_worker_loop(
            registry=registry,
            adapter=adapter,
            batch_id=batch,
            worker_id=f"cli-{os.getpid()}",
            dry_run=settings.dry_run,
            max_attempts=settings.max_attempts,
            poll_interval_seconds=settings.poll_interval_seconds,
            max_poll_minutes=settings.max_poll_minutes,
            max_files=max_files,
            on_outcome=lambda outcome, total: typer.echo(
                f"file -> {outcome} (running total: {total})"
            ),
        )
    finally:
        adapter.close()

    if (
        max_files is None or sum(outcomes.values()) < max_files
    ) and batches.state_of(registry=registry, batch_id=batch) is BatchState.PAUSED:
        typer.echo(f"batch {batch!r} is PAUSED -- stopping before the next claim")

    processed = sum(outcomes.values())
    breakdown = ", ".join(f"{count} {state}" for state, count in sorted(outcomes.items()))
    typer.echo(f"run complete: {processed} file(s) processed this invocation ({breakdown})")
    if outcomes.get(FileState.ABANDONED) or outcomes.get(FileState.FAILED):
        typer.echo(
            f"WARNING: {outcomes.get(FileState.FAILED, 0)} FAILED, "
            f"{outcomes.get(FileState.ABANDONED, 0)} ABANDONED this invocation -- "
            f"check `migration-hub status --batch {batch}` for last_error"
        )

@app.command()
def migrate(
    batch: str = typer.Option(..., "--batch"),
    source: Path | None = typer.Option(None, "--source"),
    max_files: int | None = typer.Option(None, "--max-files"),
) -> None:
    typer.secho(f"[1/4] discovering files for batch {batch!r}", bold=True)
    plan(batch=batch, source=source, max_files=max_files)

    typer.secho(f"[2/4] validating batch {batch!r}", bold=True)
    validate(batch=batch)

    typer.secho(f"[3/4] staging batch {batch!r}", bold=True)
    stage(batch=batch)

    typer.secho(f"[4/4] running batch {batch!r}", bold=True)
    run(batch=batch, max_files=max_files)

@app.command()
def reap() -> None:
    settings = _load_settings()
    registry = _registry(settings)

    adapter = _adapter(settings)
    try:
        resolved = reaper.reap_stale_claims(
            registry=registry,
            adapter=adapter,
            timeout_minutes=settings.claim_timeout_minutes,
            max_attempts=settings.max_attempts,
        )
    finally:
        adapter.close()

    typer.echo(f"reaped {resolved} stale claim(s)")

@app.command()
def schedule() -> None:
    settings = _load_settings()

    def _echo(name: str, result: int) -> None:
        typer.echo(f"{name} -> {result}")

    scheduler.run_forever(
        poll_interval_seconds=settings.scheduler_poll_interval_seconds,
        reap_interval_seconds=settings.scheduler_reap_interval_seconds,
        on_result=_echo,
    )

@app.command()
def retry(
    file_id: int | None = typer.Option(None, "--file-id"),
    batch: str | None = typer.Option(None, "--batch"),
    by: str | None = typer.Option(None, "--by", help="Defaults to the current OS user."),
) -> None:
    if (file_id is None) == (batch is None):
        typer.echo("specify exactly one of --file-id or --batch", err=True)
        raise typer.Exit(1)

    settings = _load_settings()
    registry = _registry(settings)
    actor = by or getpass.getuser()

    if file_id is not None:
        file = registry.get(file_id)
        if file is None:
            typer.echo(f"no file {file_id}", err=True)
            raise typer.Exit(1)
        try:
            outcome = batches.retry_files(
                registry,
                batch_id=file.batch_id,
                file_ids=[file_id],
                actor=actor,
                detail="manual retry via CLI",
                worker_count=1,
                max_files_per_worker=1,
            )
        except batches.NotRetryableError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"requeued file {file_id} to STAGED; worker pid={outcome.workers[0].pid}")
        return

    retryable = registry.files_in_states(states=[FileState.FAILED, FileState.ABANDONED], batch_id=batch)
    if not retryable:
        typer.echo(f"no FAILED/ABANDONED file(s) in batch {batch!r}")
        return
    outcome = batches.retry_files(
        registry,
        batch_id=batch,
        file_ids=[f.file_id for f in retryable],
        actor=actor,
        detail="manual retry via CLI",
        worker_count=max(1, settings.max_concurrent_uploads),
        max_files_per_worker=None,
        environment=settings.environment,
    )
    pids = ", ".join(str(w.pid) for w in outcome.workers)
    typer.echo(f"requeued {len(outcome.requeued_file_ids)} file(s) to STAGED; worker(s) pid={pids}")

@app.command()
def status(batch: str | None = typer.Option(None, "--batch")) -> None:
    settings = _load_settings()
    registry = _registry(settings)
    counts = registry.counts_by_state(batch_id=batch)
    for state in FileState:
        typer.echo(f"  {state.value:<12} {counts[state]}")

controls_app = typer.Typer(help="Open, close and inspect per-run batch-controls records.")
app.add_typer(controls_app, name="controls")

def _echo_record(
    record: controls.ControlRecord, *, totals: controls.ControlTotals | None = None
) -> None:
    resolved = totals if totals is not None else record.totals
    live_note = " (live)" if totals is not None else ""
    typer.echo(f"run_id={record.run_id} batch={record.batch_id} run_seq={record.run_seq}")
    typer.echo(
        f"  status={record.status.value} trigger={record.trigger.value} "
        f"initiated_by={record.initiated_by}"
    )
    typer.echo(
        f"  totals{live_note}: source={resolved.source_count}/{resolved.source_bytes}B "
        f"target={resolved.target_count}/{resolved.target_bytes}B "
        f"completed={resolved.completed_count} abandoned={resolved.abandoned_count} "
        f"rejected={resolved.rejected_count} skipped={resolved.skipped_count} "
        f"outstanding={resolved.outstanding_count} retry={resolved.retry_count}"
    )
    typer.echo(f"  balances={resolved.balances} (unaccounted={resolved.unaccounted})")

@controls_app.command("open")
def controls_open(
    batch: str = typer.Option(..., "--batch"),
    trigger: controls.RunTrigger = typer.Option(controls.RunTrigger.RUN, "--trigger"),
    by: str | None = typer.Option(None, "--by", help="Defaults to the current OS user."),
) -> None:
    settings = _load_settings()
    registry = _registry(settings)
    record = controls.open_run(
        registry=registry, batch_id=batch, trigger=trigger, initiated_by=by or getpass.getuser()
    )
    live = controls.derive(registry=registry, run_id=record.run_id)
    _echo_record(record, totals=live)

@controls_app.command("close")
def controls_close(run_id: str = typer.Option(..., "--run-id")) -> None:
    settings = _load_settings()
    registry = _registry(settings)
    parsed_id = UUID(run_id)
    run = registry.get_batch_run(parsed_id)
    if run is None:
        typer.echo(f"no batch_run with run_id={run_id}", err=True)
        raise typer.Exit(1)

    adapter = _adapter(settings)
    try:
        confirmed_count, confirmed_bytes = reconciliation.reconcile_target_count(
            registry=registry, adapter=adapter, batch_id=run.batch_id
        )
    finally:
        adapter.close()

    record = controls.close(
        registry=registry,
        run_id=parsed_id,
        reconciled_target_count=confirmed_count,
        reconciled_target_bytes=confirmed_bytes,
    )
    _echo_record(record)

@controls_app.command("abort")
def controls_abort(
    run_id: str = typer.Option(..., "--run-id"),
    reason: str = typer.Option(..., "--reason"),
) -> None:
    settings = _load_settings()
    registry = _registry(settings)
    record = controls.abort(registry=registry, run_id=UUID(run_id), reason=reason)
    _echo_record(record)

@controls_app.command("sign-off")
def controls_sign_off(
    run_id: str = typer.Option(..., "--run-id"),
    by: str | None = typer.Option(None, "--by", help="Defaults to the current OS user."),
) -> None:
    settings = _load_settings()
    registry = _registry(settings)
    record = controls.sign_off(registry=registry, run_id=UUID(run_id), by=by or getpass.getuser())
    _echo_record(record)

    ok, reasons = batches.exit_criteria_met(registry=registry, batch_id=record.batch_id)
    if ok:
        batches.mark_done(registry=registry, batch_id=record.batch_id)
        typer.echo(f"batch {record.batch_id!r} marked DONE")
    else:
        typer.echo(f"batch {record.batch_id!r} not yet done: {'; '.join(reasons)}")

@controls_app.command("export")
def controls_export(
    run_id: str = typer.Option(..., "--run-id"),
    output: Path = typer.Option(..., "--output"),
) -> None:
    settings = _load_settings()
    registry = _registry(settings)
    digest = controls.export(registry=registry, run_id=UUID(run_id), output_path=str(output))
    typer.echo(f"wrote {output} (sha256={digest})")

@controls_app.command("verify")
def controls_verify(run_id: str = typer.Option(..., "--run-id")) -> None:
    settings = _load_settings()
    registry = _registry(settings)
    ok, divergences = controls.verify(registry=registry, run_id=UUID(run_id))
    if ok:
        typer.echo("verify OK -- no divergence from the stored record")
        return
    typer.secho("verify FAILED:", fg=typer.colors.RED, bold=True)
    for line in divergences:
        typer.echo(f"  {line}")
    raise typer.Exit(1)

@controls_app.command("history")
def controls_history(batch: str = typer.Option(..., "--batch")) -> None:
    settings = _load_settings()
    registry = _registry(settings)
    runs = controls.history(registry=registry, batch_id=batch)
    if not runs:
        typer.echo(f"no runs recorded for batch {batch!r}")
        return
    for record in runs:
        live = (
            controls.derive(registry=registry, run_id=record.run_id)
            if record.status is controls.RunStatus.RUNNING
            else None
        )
        _echo_record(record, totals=live)
        typer.echo("")

def main() -> int:
    app()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
