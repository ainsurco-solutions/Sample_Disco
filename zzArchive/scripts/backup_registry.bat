@echo off
setlocal enabledelayedexpansion

rem ===========================================================================
rem  AMIGO -- back up the registry
rem
rem  Double-click this, or run it from a shell. It writes a dated, consistent
rem  snapshot of registry.db beside the original.
rem
rem  WHY THIS EXISTS RATHER THAN A FILE COPY. The registry is the record of
rem  what has been migrated -- losing it mid-wave is an audit gap, not a
rem  restart (ADR-0004). A plain copy of a SQLite file that is being written
rem  can be torn: the copy succeeds, the file is unusable, and nothing says so
rem  until you try to open it. VACUUM INTO takes a consistent snapshot of a
rem  live database instead, which is why this runs SQL rather than copying.
rem
rem  It uses Python's own sqlite3 module. The sqlite3.exe command-line tool is
rem  not installed on Windows by default and asking an operator to install one
rem  to take a backup is how backups stop being taken.
rem
rem  DESTINATION. The snapshot lands beside the registry by default, which is
rem  the wrong place for a backup on its own -- if that disk or folder is the
rem  problem, both copies are gone. Pass a destination folder as the first
rem  argument to put it somewhere else, and prefer somewhere other than the
rem  machine that wrote it. A synced folder or network share is a perfectly
rem  good DESTINATION; it is only running the LIVE registry there that
rem  corrupts (TASK-0059, RSK-0008).
rem
rem      backup_registry.bat
rem      backup_registry.bat "X:\backups"
rem ===========================================================================

rem --- Locate the install, the same way amigo.bat does: this file ships in
rem --- scripts\, so the release root is one level up. AMIGO_HOME wins.
if defined AMIGO_HOME (
    set "RELEASE_ROOT=%AMIGO_HOME%"
) else (
    for %%I in ("%~dp0..") do set "RELEASE_ROOT=%%~fI"
)

set "VENV_ACTIVATE=%RELEASE_ROOT%\.venv\Scripts\activate.bat"

title AMIGO -- backup registry

if not exist "%VENV_ACTIVATE%" (
    echo.
    echo  CANNOT START: no virtualenv found at
    echo      %VENV_ACTIVATE%
    echo.
    echo  Run this from an installed release, or set AMIGO_HOME to the folder
    echo  containing .venv\.
    echo.
    goto :halt
)

call "%VENV_ACTIVATE%" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  CANNOT START: the virtualenv at
    echo      %VENV_ACTIVATE%
    echo  exists but would not activate.
    echo.
    goto :halt
)

rem --- The registry path comes from the same place the application reads it,
rem --- so a backup cannot quietly snapshot a different database than the one
rem --- in use. Reading it here instead of hardcoding registry.db also covers
rem --- the deployments that point MIGRATION_HUB_DATABASE_URL elsewhere.
pushd "%RELEASE_ROOT%"

set "DEST=%~1"

python "%~dp0_backup_registry.py" %DEST:"=%
set "RC=%ERRORLEVEL%"

popd

if not "%RC%"=="0" (
    echo.
    echo  Backup FAILED. Nothing was written.
    echo.
)

:halt
echo.
pause
endlocal
