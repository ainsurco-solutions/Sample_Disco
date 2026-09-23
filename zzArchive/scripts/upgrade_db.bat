@echo off
setlocal

rem ===========================================================================
rem  AMIGO -- bring the registry schema up to date with the code
rem
rem  Double-click this, or run it from a shell. It runs
rem
rem      python -m alembic upgrade head
rem
rem  from the tree root, which is where alembic.ini and .env live. Run it after
rem  every transfer that carries a file under migrations\versions\ -- the
rem  Sample_Disco commit page shows whether one did. Skipping it does not fail
rem  at the upgrade; it fails later, at the first command that touches a column
rem  the database does not have yet ("table migration_file has no column named
rem  ..."), which reads like a bad copy and is not.
rem
rem  IT BACKS UP FIRST. Some migrations rewrite rows rather than only adding
rem  columns -- e8b2f4a6c1d3 strips presigned-URL signatures out of stored
rem  transactions, and there is no downgrade that puts them back. So this
rem  takes the same consistent snapshot as backup_registry.bat before touching
rem  anything. If the snapshot cannot be taken (a first install with no
rem  registry yet, or a non-SQLite registry) it asks before carrying on.
rem
rem      upgrade_db.bat
rem      upgrade_db.bat "X:\backups"     snapshot somewhere other than beside
rem                                      the registry
rem ===========================================================================

rem --- Locate the install, the same way amigo.bat does: this file ships in
rem --- scripts\, so the release root is one level up. AMIGO_HOME wins.
if defined AMIGO_HOME (
    set "RELEASE_ROOT=%AMIGO_HOME%"
) else (
    for %%I in ("%~dp0..") do set "RELEASE_ROOT=%%~fI"
)

set "VENV_ACTIVATE=%RELEASE_ROOT%\.venv\Scripts\activate.bat"

title AMIGO -- upgrade registry schema

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

if not exist "%RELEASE_ROOT%\alembic.ini" (
    echo.
    echo  CANNOT START: no alembic.ini at
    echo      %RELEASE_ROOT%
    echo.
    echo  It sits at the tree root beside migrations\ -- copy it from the
    echo  published tree. Run check_copy.bat to see what else is missing.
    echo.
    goto :halt
)

rem --- alembic finds alembic.ini, and the registry URL finds .env, relative to
rem --- the current directory -- so run from the root, not from scripts\.
pushd "%RELEASE_ROOT%"

echo.
echo  --- 1. Snapshot the registry ------------------------------------------
echo.
rem --- Destination handling is the same as backup_registry.bat, including why
rem --- %~1 is tested rather than stripping quotes from a variable.
if "%~1"=="" (
    python "%~dp0_backup_registry.py"
) else (
    python "%~dp0_backup_registry.py" "%~1"
)
if errorlevel 1 (
    echo.
    echo  No snapshot was taken -- see the message above.
    echo  On a first install with no registry yet, that is expected.
    echo.
    choice /c YN /n /m "  Upgrade WITHOUT a backup? [Y/N] "
    if errorlevel 2 (
        echo.
        echo  Stopped. Nothing was changed.
        popd
        goto :halt
    )
)

echo.
echo  --- 2. Schema before ---------------------------------------------------
echo.
python -m alembic current

echo.
echo  --- 3. Upgrade to head -------------------------------------------------
echo.
python -m alembic upgrade head
set "RC=%ERRORLEVEL%"

echo.
echo  --- 4. Schema after ----------------------------------------------------
echo.
python -m alembic current

popd

echo.
if "%RC%"=="0" (
    echo  ============================================================
    echo   Registry schema is at head.
    echo  ============================================================
) else (
    echo  ============================================================
    echo   UPGRADE FAILED -- read the error above.
    echo  ============================================================
    echo.
    echo   The snapshot from step 1, if one was taken, is the registry
    echo   as it was before this ran. Do not start a migration against
    echo   a half-upgraded registry.
)

:halt
echo.
pause
endlocal
