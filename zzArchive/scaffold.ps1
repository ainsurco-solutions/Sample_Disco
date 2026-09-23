<#
.SYNOPSIS
    Create AMIGO's folder structure and empty files on a machine that cannot pull.

.DESCRIPTION
    The client machine receives this code by hand-copying it out of the public
    Sample_Disco repo. Creating dozens of files across nested folders by hand, in
    the right places, is where that goes wrong -- a file in the wrong folder fails
    as an ImportError, and a missing __init__.py fails as one too, both a long way
    from the person who made the mistake.

    The count is deliberately not written here: the manifest below is generated
    and grows, and a number in prose goes stale silently. The run reports it.

    This builds the empty shape so the only manual step left is pasting contents
    into files that already exist in the right place.

    The manifest is GENERATED, not hand-written. push_amigo_to_demo.py emits it
    with -EmitScaffold, from the same walk that decides what gets published, so
    the two cannot drift. Regenerate rather than editing the list below.

    Safe to re-run: an existing file is left alone unless -Force is given, so a
    half-finished paste is never destroyed by running this again.

.PARAMETER Root
    Where to build the tree. Defaults to the current directory.

.PARAMETER Force
    Truncate files that already exist. Off by default -- the whole point is that
    re-running must not discard work already pasted in.

.PARAMETER WhatIf
    Report what would be created without creating it.

.EXAMPLE
    .\scaffold.ps1 -Root C:\AMIGO -WhatIf
    Show the tree that would be created.

.EXAMPLE
    .\scaffold.ps1 -Root C:\AMIGO
    Create it. Then paste each file's contents from the Sample_Disco copy.

.NOTES
    After pasting, verify the shape before trusting it:

        cd C:\AMIGO
        python -c "import sys; sys.path.insert(0,'src'); import migration_hub"

    An empty file left unpasted imports cleanly and fails later, so also check
    for leftovers:

        Get-ChildItem -Recurse -File | Where-Object Length -eq 0
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string] $Root = (Get-Location).Path,
    [switch] $Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- BEGIN GENERATED MANIFEST (push_amigo_to_demo.py --emit-scaffold) --------
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
    'src/migration_hub/ui/pages/ui_ux_mockup.py'
    'migrations/env.py'
    'migrations/README.md'
    'migrations/script.py.mako'
    'migrations/versions/.gitkeep'
    'migrations/versions/2e4122b688e7_drop_staged_path.py'
    'migrations/versions/3405822f52ca_create_registry_schema.py'
    'migrations/versions/4b8c1d5e9a02_move_staged_rows_to_validated.py'
    'migrations/versions/7c3f1a9e2b4d_add_databridge_and_archive_columns.py'
    'migrations/versions/9d2e6f4a1c8b_drop_irp_only_columns.py'
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
    'scripts/check_copy.py'
    'scaffold.ps1'
    'VM-SETUP.txt'
    'scripts/check_copy.bat'
    'scripts/backup_registry.bat'
    'scripts/_backup_registry.py'
    'scripts/upgrade_db.bat'
)
# --- END GENERATED MANIFEST -------------------------------------------------

# Directories the runtime needs but no published file lives in. Without these
# the first run fails on a missing path rather than on anything informative.
$EmptyDirs = @(
    'config'      # dev.json is created here from the example; it is git-ignored
    'var'         # the SQLite registry's default home (MIGRATION_HUB_DATABASE_URL)
    'logs'
)

# A -Root that still carries the parameter name, or an embedded drive letter, was
# not parsed the way it was meant. `-Root/"C:\path"` is the one that catches
# people: PowerShell takes the whole `-Root/C:\path` as a single positional value,
# joins it onto the current directory, and the first New-Item then fails with
# "filename, directory name, or volume label syntax is incorrect" -- 150 lines from
# the real mistake, and only on the real run, because -WhatIf never touches the
# disk and so reports a cheerful success. Fail here instead, naming the fix.
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
        # Never truncate silently. Re-running after a partial paste must not
        # throw away what was already pasted in.
        Write-Host ("  skip   {0}" -f $relative) -ForegroundColor DarkGray
        $skipped++
        continue
    }

    if ($PSCmdlet.ShouldProcess($full, $(if ($exists) { 'Truncate file' } else { 'Create file' }))) {
        # -Force on New-Item truncates an existing file, which is exactly what
        # is wanted here and exactly what must not happen without -Force above.
        New-Item -ItemType File -Path $full -Force | Out-Null

        # A package marker is published as one byte -- a single newline -- so a
        # scaffolded zero-byte file differs from the manifest and check_copy.py
        # reports it as EMPTY. That reading is correct by its own rules and
        # useless in practice: EMPTY is meant to say "created but never pasted",
        # and for an __init__.py that is already its finished state. Writing the
        # newline here makes a correctly scaffolded tree verify clean, so the
        # EMPTY column keeps meaning what it says.
        #
        # Only the empty markers. Any __init__.py with real content in it --
        # migration_hub/__init__.py is 5 lines -- must still be pasted, and
        # seeding it here would hide that behind a one-byte file.
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
