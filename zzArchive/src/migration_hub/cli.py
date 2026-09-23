from __future__ import annotations

import getpass
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import typer
from dotenv import load_dotenv

from migration_hub.adapters.databridge.client import DatabridgeAdapter
from migration_hub.config.settings import Settings
from migration_hub.core.engine import create_registry_engine
from migration_hub.core.registry import Registry
from migration_hub.core.states import BatchState, FileState
from migration_hub.observability import audit_export, controls
from migration_hub.observability import logging as hub_logging
from migration_hub.orchestration import (
    archiver,
    batches,
    queue_check,
    reaper,
    reconciliation,
    scheduler,
)
from migration_hub.orchestration.auto_migrate import (
    SourceFolderError,
    check_source_folder,
    run_automated_migration,
)
from migration_hub.producers import scanner, validator

load_dotenv()

app = typer.Typer(help=__doc__)

@app.callback()
def _configure_logging() -> None:
    hub_logging.configure(level=os.environ.get("MIGRATION_HUB_LOG_LEVEL", "INFO"))

def _load_settings() -> Settings:
    environment = os.environ.get("MIGRATION_HUB_ENV", "dev")
    settings = Settings.load(environment=environment, config_dir=Path("config"))
    typer.secho(f"environment: {settings.environment}", fg=typer.colors.YELLOW, bold=True)
    return settings

def _registry(settings: Settings) -> Registry:
    engine = create_registry_engine(settings.database_url)
    return Registry(engine)

def _adapter(settings: Settings) -> DatabridgeAdapter:
    engine = create_registry_engine(settings.database_url)
    return DatabridgeAdapter(host=settings.api_host, api_key=settings.api_key(), engine=engine)

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
            instance_name=settings.databridge_instance_name,
            instances=settings.databridge_instances,
        )
    finally:
        adapter.close()

    if (max_files is None or sum(outcomes.values()) < max_files) and batches.state_of(
        registry=registry, batch_id=batch
    ) is BatchState.PAUSED:
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
    source: Path | None = typer.Option(None, "--source"),
    batch: str | None = typer.Option(
        None, "--batch", help="Run over an existing batch instead of a folder (no discovery)."
    ),
    max_files: int | None = typer.Option(None, "--max-files"),
) -> None:
    if source is not None and batch is not None:
        typer.echo("specify --source or --batch, not both", err=True)
        raise typer.Exit(1)

    settings = _load_settings()
    if source is None and batch is None:
        source = settings.source_root
        typer.echo(f"source: {source} (source_root from config/{settings.environment}.json)")

    if source is not None:
        try:
            check_source_folder(source)
        except SourceFolderError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc

    registry = _registry(settings)

    result = run_automated_migration(
        registry=registry,
        adapter_factory=lambda: _adapter(settings),
        source_root=source,
        batch_id=batch,
        pattern=settings.file_pattern,
        max_files=max_files,
        thread_count=max(1, settings.max_concurrent_uploads),
        dry_run=settings.dry_run,
        max_attempts=settings.max_attempts,
        poll_interval_seconds=settings.poll_interval_seconds,
        max_poll_minutes=settings.max_poll_minutes,
        instance_name=settings.databridge_instance_name,
        instances=settings.databridge_instances,
        max_archive_attempts=settings.max_archive_attempts,
        compute_checksum=settings.compute_checksums,
        on_progress=lambda message: typer.echo(message),
    )

    typer.echo("")
    breakdown = ", ".join(f"{count} {state}" for state, count in sorted(result.outcomes.items()))
    retried = f" after {result.passes} passes" if result.passes > 1 else ""
    typer.echo(f"batch {result.batch_id!r}{retried}, files by final state: {breakdown}")
    typer.echo(f"run {result.run_id} status={result.status}")
    if result.signed_off:
        typer.secho(f"signed off automatically as {result.signed_off_by!r}", fg=typer.colors.GREEN)
    elif result.status != "RUNNING":
        typer.secho(
            f"NOT signed off -- run `migration-hub controls history --batch "
            f"{result.batch_id}` to review, then `migration-hub controls sign-off "
            f"--run-id {result.run_id}` once dispositioned",
            fg=typer.colors.YELLOW,
        )

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
def archive(batch: str | None = typer.Option(None, "--batch")) -> None:
    settings = _load_settings()
    registry = _registry(settings)

    adapter = _adapter(settings)
    try:
        session = adapter.open_session()
        archived = archiver.archive_pending(
            registry=registry,
            adapter=adapter,
            batch_id=batch,
            resource_group_id=session.resource_group_id,
            max_wait_minutes=settings.max_poll_minutes,
            poll_interval_seconds=settings.poll_interval_seconds,
            max_archive_attempts=settings.max_archive_attempts,
        )
    finally:
        adapter.close()

    typer.echo(f"archived {archived} file(s)" + (f" in batch {batch!r}" if batch else ""))

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
        if FileState(file.state) not in batches.RETRYABLE_STATES:
            typer.echo(str(batches.NotRetryableError(file_id, file.state)), err=True)
            raise typer.Exit(1)
        batch_id, file_ids = file.batch_id, [file_id]
    else:
        assert batch is not None
        retryable = registry.files_in_states(
            states=sorted(batches.RETRYABLE_STATES), batch_id=batch
        )
        if not retryable:
            typer.echo(f"no FAILED/ABANDONED/ARCHIVE_FAILED file(s) in batch {batch!r}")
            return
        batch_id, file_ids = batch, [f.file_id for f in retryable]

    adapter = _adapter(settings)
    try:
        outcome = batches.retry_files(
            registry,
            batch_id=batch_id,
            file_ids=file_ids,
            actor=actor,
            locate=batches.platform_locator(adapter),
            detail="manual retry via CLI",
            environment=settings.environment,
        )
    finally:
        adapter.close()
    _echo_retry(outcome)

def _echo_retry(outcome: batches.RetryOutcome) -> None:
    if outcome.completed_file_ids:
        typer.echo(
            f"{len(outcome.completed_file_ids)} file(s) already in Data Vault -- "
            "marked COMPLETED, nothing redone"
        )
    if outcome.bridged_file_ids:
        typer.echo(
            f"{len(outcome.bridged_file_ids)} file(s) already on Data Bridge -- "
            "BRIDGED, archive only"
        )
    if outcome.requeued_file_ids:
        typer.echo(
            f"{len(outcome.requeued_file_ids)} file(s) on neither -- requeued to VALIDATED "
            "from the start"
        )
    if outcome.archive_retry_file_ids:
        typer.echo(
            f"{len(outcome.archive_retry_file_ids)} ARCHIVE_FAILED file(s) -- "
            "retrying the archive only"
        )
    if outcome.workers:
        logs = ", ".join(str(w.log_path) for w in outcome.workers)
        started = (
            "the migrate pipeline (upload, archive, close)"
            if outcome.pipeline_started
            else "the archive"
        )
        typer.echo(f"{started} started in the background; output: {logs}")
    if outcome.not_started_reason:
        typer.echo(outcome.not_started_reason, err=True)
    for failed_id, reason in outcome.unchecked.items():
        typer.echo(
            f"file {failed_id}: could not check the platform, left as it was -- {reason}",
            err=True,
        )

@app.command()
def status(batch: str | None = typer.Option(None, "--batch")) -> None:
    settings = _load_settings()
    registry = _registry(settings)
    counts = registry.counts_by_state(batch_id=batch)
    for state in FileState:
        typer.echo(f"  {state.value:<12} {counts[state]}")

    stuck = registry.files_in_states(
        states=(FileState.FAILED, FileState.ABANDONED, FileState.ARCHIVE_FAILED), batch_id=batch
    )
    if stuck:
        typer.echo("")
        typer.echo(f"{len(stuck)} file(s) need attention:")
        for file in stuck:
            typer.echo(f"  [{file.file_id}] {file.source_database} -- {file.state}")
            typer.echo(f"      {file.last_error or '(no error recorded)'}")

def configured_instances(settings: Settings) -> list[str]:
    names = list(dict.fromkeys(settings.databridge_instances.values()))
    if settings.databridge_instance_name and settings.databridge_instance_name not in names:
        names.append(settings.databridge_instance_name)
    return names

_RECOMMENDATION_COLOURS = {
    queue_check.Recommendation.PUSH: typer.colors.GREEN,
    queue_check.Recommendation.HOLD: typer.colors.YELLOW,
    queue_check.Recommendation.INVESTIGATE: typer.colors.RED,
    queue_check.Recommendation.UNKNOWN: typer.colors.MAGENTA,
}

def _by_group(counts: dict[str, int]) -> str:
    return (
        f"{sum(counts.values())} (import {counts.get('import', 0)}, "
        f"archive {counts.get('archive', 0)}, other {counts.get('other', 0)})"
    )

def _echo_queue(snapshot: queue_check.Snapshot) -> None:
    for q in snapshot.instances:
        typer.secho(
            f"{q.instance}: {q.recommendation}",
            fg=_RECOMMENDATION_COLOURS[q.recommendation],
            bold=True,
        )
        if q.read_ok:
            oldest = (
                "none active"
                if q.oldest_active_minutes is None
                else f"{q.oldest_active_minutes} min"
            )
            typer.echo(f"  queued  {_by_group(q.queued)}")
            typer.echo(f"  running {_by_group(q.running)}")
            typer.echo(
                f"  ours active {q.ours_active}; oldest active {oldest}; "
                f"failed in 24 h {q.failed_last_24h}; {q.total_jobs:,} job(s) in the list"
            )
        for reason in q.reasons:
            typer.echo(f"  - {reason}")
    typer.echo(
        f"read at {snapshot.written_at:%Y-%m-%d %H:%M:%S} UTC; next read allowed from "
        f"{snapshot.fresh_until():%H:%M:%S} UTC"
    )

@app.command()
def queue() -> None:
    settings = _load_settings()
    instances = configured_instances(settings)
    if not instances:
        typer.echo("no Data Bridge instance configured -- nothing to check", err=True)
        raise typer.Exit(1)
    if settings.dry_run:
        typer.echo("dry_run is on -- no vendor calls, so no queue read")
        last = queue_check.read_snapshot(settings.queue_snapshot_path)
        if last is not None:
            typer.echo("last result:")
            _echo_queue(last)
        return

    registry = _registry(settings)
    adapter = _adapter(settings)
    try:
        snapshot = queue_check.run_check(
            instances=instances,
            fetch=lambda name: adapter.list_instance_jobs(instance_name=name),
            our_job_ids=registry.known_job_ids(),
            snapshot_path=settings.queue_snapshot_path,
            now=datetime.now(UTC),
            ttl_minutes=settings.queue_check_ttl_minutes,
            thresholds=queue_check.Thresholds(
                hold_active_imports=settings.queue_hold_active_imports,
                investigate_after_minutes=settings.max_poll_minutes,
                investigate_failures_24h=settings.queue_investigate_failures_24h,
            ),
        )
    except queue_check.TooSoonError as exc:
        typer.echo(f"not read again: {exc}")
        typer.echo("last result:")
        snapshot = exc.snapshot
    finally:
        adapter.close()
    _echo_queue(snapshot)

controls_app = typer.Typer(help="Open, close and inspect per-run batch-controls records.")
app.add_typer(controls_app, name="controls")

audit_app = typer.Typer(help="Export a batch's complete audit trail.")
app.add_typer(audit_app, name="audit")

@audit_app.command("export")
def audit_export_command(
    batch: str = typer.Option(..., "--batch"),
    output: Path | None = typer.Option(
        None, "--output", help="Zip to write. Defaults to exports/audit-<batch>-<UTC time>.zip."
    ),
    by: str | None = typer.Option(None, "--by", help="Defaults to the current OS user."),
) -> None:
    settings = _load_settings()
    registry = _registry(settings)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = output or Path("exports") / f"audit-{batch}-{stamp}.zip"
    try:
        result = audit_export.export_batch_audit(
            registry=registry,
            batch_id=batch,
            output_path=path,
            generated_by=by or getpass.getuser(),
            environment=settings.environment,
        )
    except audit_export.UnknownBatchError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"wrote {result.path}: {result.files} file(s), {result.events} event(s), "
        f"{result.api_calls} API call(s), {result.runs} run record(s)"
    )
    typer.echo(f"sha256={result.sha256} (also in {result.path.name}.sha256)")

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
        f"outstanding={resolved.outstanding_count} retry={resolved.retry_count} "
        f"archive_failed={resolved.archive_failed_count} "
        f"still_on_bridge={resolved.bridge_count}"
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
        targets = reconciliation.reconcile_targets(
            registry=registry, adapter=adapter, batch_id=run.batch_id
        )
    finally:
        adapter.close()

    if targets.lost_file_ids:
        typer.echo(
            f"LOST: file(s) {targets.lost_file_ids} are ARCHIVE_FAILED but on neither "
            "Data Bridge nor Data Vault -- investigate before retrying",
            err=True,
        )
    record = controls.close(
        registry=registry,
        run_id=parsed_id,
        reconciled_target_count=targets.target_count,
        reconciled_target_bytes=targets.target_bytes,
        reconciled_bridge_count=targets.bridge_count,
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
