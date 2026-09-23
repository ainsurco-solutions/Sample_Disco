[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string] $Root = (Get-Location).Path,
    [switch] $Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Files = @(
    'src/migration_hub/__init__.py'
    'src/migration_hub/_tls.py'
    'src/migration_hub/adapters/__init__.py'
    'src/migration_hub/adapters/base.py'
    'src/migration_hub/adapters/databridge/__init__.py'
    'src/migration_hub/adapters/databridge/client.py'
    'src/migration_hub/adapters/databridge/errors.py'
    'src/migration_hub/adapters/databridge/session.py'
    'src/migration_hub/adapters/storage/__init__.py'
    'src/migration_hub/adapters/storage/databridge_uploader.py'
    'src/migration_hub/adapters/storage/s3_uploader.py'
    'src/migration_hub/api/__init__.py'
    'src/migration_hub/api/deps.py'
    'src/migration_hub/api/main.py'
    'src/migration_hub/api/routers/__init__.py'
    'src/migration_hub/api/routers/batches.py'
    'src/migration_hub/api/routers/files.py'
    'src/migration_hub/api/routers/health.py'
    'src/migration_hub/cli.py'
    'src/migration_hub/config/__init__.py'
    'src/migration_hub/config/settings.py'
    'src/migration_hub/consumers/__init__.py'
    'src/migration_hub/consumers/tasks.py'
    'src/migration_hub/consumers/worker.py'
    'src/migration_hub/core/__init__.py'
    'src/migration_hub/core/checksum.py'
    'src/migration_hub/core/engine.py'
    'src/migration_hub/core/models.py'
    'src/migration_hub/core/registry.py'
    'src/migration_hub/core/retry.py'
    'src/migration_hub/core/routing.py'
    'src/migration_hub/core/scrub.py'
    'src/migration_hub/core/states.py'
    'src/migration_hub/observability/__init__.py'
    'src/migration_hub/observability/audit.py'
    'src/migration_hub/observability/audit_export.py'
    'src/migration_hub/observability/controls.py'
    'src/migration_hub/observability/endpoints.py'
    'src/migration_hub/observability/logging.py'
    'src/migration_hub/observability/metrics.py'
    'src/migration_hub/observability/redaction.py'
    'src/migration_hub/orchestration/__init__.py'
    'src/migration_hub/orchestration/archiver.py'
    'src/migration_hub/orchestration/auto_migrate.py'
    'src/migration_hub/orchestration/batch_identity.py'
    'src/migration_hub/orchestration/batches.py'
    'src/migration_hub/orchestration/reaper.py'
    'src/migration_hub/orchestration/reconciliation.py'
    'src/migration_hub/orchestration/scheduler.py'
    'src/migration_hub/orchestration/worker_pool.py'
    'src/migration_hub/producers/__init__.py'
    'src/migration_hub/producers/naming.py'
    'src/migration_hub/producers/scanner.py'
    'src/migration_hub/producers/target_list.py'
    'src/migration_hub/producers/validator.py'
    'src/migration_hub/ui/__init__.py'
    'src/migration_hub/ui/app.py'
    'src/migration_hub/ui/pages/__init__.py'
    'migrations/env.py'
    'migrations/README.md'
    'migrations/script.py.mako'
    'migrations/versions/.gitkeep'
    'migrations/versions/2e4122b688e7_drop_staged_path.py'
    'migrations/versions/3405822f52ca_create_registry_schema.py'
    'migrations/versions/4b8c1d5e9a02_move_staged_rows_to_validated.py'
    'migrations/versions/7c3f1a9e2b4d_add_databridge_and_archive_columns.py'
    'migrations/versions/9d2e6f4a1c8b_drop_irp_only_columns.py'
    'migrations/versions/a1d9c7e5b3f2_batch_destination.py'
    'migrations/versions/c5a7e3d91f20_archive_states.py'
    'migrations/versions/e8b2f4a6c1d3_scrub_presigned_signatures.py'
    'alembic.ini'
    'requirements.txt'
    'pyproject.toml'
    '.env.example'
    'config/dev.example.json'
    'config/prod.example.json'
    '.streamlit/config.toml'
    '.streamlit/config.teal.toml'
    'scripts/amigo.bat'
    'scripts/check_credentials.py'
    'scripts/probe_job_queue.py'
    'scripts/check_copy.py'
    'scaffold.ps1'
    'VM-SETUP.txt'
    'scripts/check_copy.bat'
    'scripts/backup_registry.bat'
    'scripts/_backup_registry.py'
    'scripts/upgrade_db.bat'
)

$EmptyDirs = @(
    'config'      # dev.json is created here from the example; it is git-ignored
    'var'         # the SQLite registry's default home (MIGRATION_HUB_DATABASE_URL)
    'logs'
)

if ($Root -match '(^|[\\/])-Root([\\/]|$)' -or $Root -match '.\w:[\\/]') {
    Write-Host ""
    Write-Host "  -Root was not parsed as you intended:" -ForegroundColor Red
    Write-Host "      $Root" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Use a SPACE between the parameter and its value, not a slash:"
    Write-Host '      .\scaffold.ps1 -Root "C:\path\to\folder"'
    Write-Host ""
    Write-Host "  Or omit -Root entirely to scaffold into the current directory."
    Write-Host ""
    exit 1
}

$rootPath = [System.IO.Path]::GetFullPath($Root)
Write-Host "Scaffolding AMIGO into $rootPath" -ForegroundColor Cyan
Write-Host ""

$created = 0
$skipped = 0
$dirs = 0

foreach ($dir in $EmptyDirs) {
    $full = Join-Path $rootPath $dir
    if (-not (Test-Path -LiteralPath $full)) {
        if ($PSCmdlet.ShouldProcess($full, 'Create directory')) {
            New-Item -ItemType Directory -Path $full -Force | Out-Null
        }
        $dirs++
    }
}

foreach ($relative in $Files) {
    $full = Join-Path $rootPath ($relative -replace '/', '\')
    $parent = Split-Path -Parent $full

    if (-not (Test-Path -LiteralPath $parent)) {
        if ($PSCmdlet.ShouldProcess($parent, 'Create directory')) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        $dirs++
    }

    $exists = Test-Path -LiteralPath $full
    if ($exists -and -not $Force) {
        Write-Host ("  skip   {0}" -f $relative) -ForegroundColor DarkGray
        $skipped++
        continue
    }

    if ($PSCmdlet.ShouldProcess($full, $(if ($exists) { 'Truncate file' } else { 'Create file' }))) {
        New-Item -ItemType File -Path $full -Force | Out-Null

        if ($relative -match '__init__\.py$') {
            [System.IO.File]::WriteAllText($full, "`n", (New-Object System.Text.UTF8Encoding $false))
        }
    }
    $label = if ($exists) { 'trunc ' } else { 'create' }
    Write-Host ("  {0} {1}" -f $label, $relative)
    $created++
}

Write-Host ""
Write-Host ("{0} file(s) created, {1} left alone, {2} folder(s) made." -f $created, $skipped, $dirs) -ForegroundColor Green

if ($skipped -gt 0) {
    Write-Host "Existing files were kept. Re-run with -Force to truncate them." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Next:" -ForegroundColor Cyan
Write-Host "  1. Paste each file's contents from the Sample_Disco copy."
Write-Host "  2. Find anything missed:  Get-ChildItem -Recurse -File | Where-Object Length -eq 0"
Write-Host "  3. Check the package imports:"
Write-Host "       python -c `"import sys; sys.path.insert(0,'src'); import migration_hub`""
Write-Host "  4. Copy .env.example to .env and config\dev.example.json to config\dev.json, then fill them in."
Write-Host "  5. python -m alembic upgrade head"
