@echo off
rem ===========================================================================
rem  AMIGO -- operator menu (TASK-0071, TASK-0106)
rem
rem  Double-click this. It opens the Migration Hub dashboard and the Reconcile
rem  app, and drives `migration-hub` from a menu, so an operator never has to
rem  know a virtualenv exists.
rem
rem  DESIGN NOTES, because the safe choices here look like fussy ones:
rem
rem  * Nothing destructive runs without a typed confirmation. An operator will
rem    double-click this twice; a second run must not be able to damage a batch.
rem  * Each app opens in its OWN window (`start`). Keep that window open while
rem    runs are going. Double-click this file from Explorer rather than running
rem    it from a VS Code terminal: a terminal can close everything it started.
rem  * .env is read here only for the start-up checks, then dropped again, so
rem    Reconcile never inherits the Hub's settings. Every app, and every
rem    `migration-hub` command, reads its own .env itself.
rem  * This is NOT a scheduler and does NOT call RatCat. Amlin runs RatCat and
rem    AMIGO reads the share it wrote to (TASK-0065). Pre-staging must FINISH
rem    before discovery runs.
rem ===========================================================================

rem --- Locate the install. This file lives in <root>\scripts, so the root is
rem --- one level up. An explicit AMIGO_HOME wins.
if defined AMIGO_HOME (
    set "RELEASE_ROOT=%AMIGO_HOME%"
) else (
    for %%I in ("%~dp0..") do set "RELEASE_ROOT=%%~fI"
)

rem --- Reconcile is its own app in its own folder, beside this one by default
rem --- (e.g. ...\_Projects\POC and ...\_Projects\Reconcile). RECONCILE_HOME wins.
if defined RECONCILE_HOME (
    set "RECONCILE_ROOT=%RECONCILE_HOME%"
) else (
    for %%I in ("%RELEASE_ROOT%\..\Reconcile") do set "RECONCILE_ROOT=%%~fI"
)

set "PYTHON=%RELEASE_ROOT%\.venv\Scripts\python.exe"
set "RECONCILE_PYTHON=%RECONCILE_ROOT%\.venv\Scripts\python.exe"

title AMIGO -- Migration Hub

rem --- Every command reads config\ and .env relative to the working folder.
cd /d "%RELEASE_ROOT%"

if not exist "%PYTHON%" (
    echo.
    echo  CANNOT START: no virtualenv found at
    echo      %PYTHON%
    echo.
    echo  Create it in %RELEASE_ROOT% -- see VM-SETUP.txt.
    echo.
    pause
    exit /b 1
)

rem --- Start-up checks, with .env loaded as Python would load it. Delayed
rem --- expansion is OFF here so a "!" in a value survives. endlocal drops the
rem --- .env values again; only the environment name comes out.
setlocal DisableDelayedExpansion
if exist "%RELEASE_ROOT%\.env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%RELEASE_ROOT%\.env") do (
        if not "%%B"=="" if not defined %%A set "%%A=%%~B"
    )
)
if not defined MIGRATION_HUB_ENV (
    echo.
    echo  CANNOT START: MIGRATION_HUB_ENV is not set.
    echo.
    echo  It names which config file to load, e.g. "prod" loads config\prod.json.
    echo  Set it in %RELEASE_ROOT%\.env
    echo.
    pause
    exit /b 1
)
if not defined MOODYS_API_KEY (
    echo.
    echo  CANNOT START: MOODYS_API_KEY is not set.
    echo.
    echo  Put it in %RELEASE_ROOT%\.env -- never in a config file, and never in
    echo  anything committed to source control.
    echo.
    pause
    exit /b 1
)
set "_ENV_NAME=%MIGRATION_HUB_ENV%"
endlocal & set "AMIGO_ENV=%_ENV_NAME%"

setlocal EnableDelayedExpansion

rem --- Warn, but do not block, on dry_run. It is a legitimate state to be in;
rem --- the danger is being in it WITHOUT KNOWING, since a dry run looks
rem --- exactly like a successful one.
set "CONFIG_FILE=%RELEASE_ROOT%\config\%AMIGO_ENV%.json"
set "DRY_RUN_NOTE="
if exist "%CONFIG_FILE%" (
    findstr /i /r /c:"\"dry_run\" *: *true" "%CONFIG_FILE%" >nul 2>&1
    if not errorlevel 1 set "DRY_RUN_NOTE=  *** dry_run is TRUE -- no files will actually be uploaded ***"
)

:menu
cls
echo ===========================================================================
echo   AMIGO -- Migration Hub          environment: %AMIGO_ENV%
echo ===========================================================================
if defined DRY_RUN_NOTE echo %DRY_RUN_NOTE%
echo.
echo   --- open the apps (each in its own window -- keep it open) ---
echo     D. Migration Hub dashboard
echo     R. Reconcile
echo.
echo   --- look, changes nothing ---
echo     1. Status of a batch
echo     2. Control-record history for a batch
echo    13. Check the Data Bridge queue  (Push / Hold / Investigate)
echo    14. Check this copy against its manifest
echo.
echo   --- run a wave ---   (RatCat pre-staging must have FINISHED first)
echo     3. Plan      (register files -- read-only, safe to repeat)
echo     4. Validate  (naming, extension, size)
echo     5. Migrate   (fully automated: upload/import/verify/archive/close/sign-off -- CONFIRMS FIRST)
echo     6. Run       (uploads and imports one step at a time -- CONFIRMS FIRST)
echo.
echo   --- close a wave ---
echo     7. Open a control record
echo     8. Close a run      (reconciles and freezes totals -- CONFIRMS FIRST)
echo     9. Export evidence
echo    10. Sign off a run   (marks the batch DONE -- CONFIRMS FIRST)
echo.
echo   --- recovery ---
echo    11. Release files held by a stopped worker  (reap)
echo    12. Retry failed files in a batch  (CONFIRMS FIRST)
echo.
echo   --- after copying a new version ---
echo     U. Upgrade the registry  (only between runs -- CONFIRMS FIRST)
echo.
echo     0. Quit
echo.
set "CHOICE="
set /p "CHOICE=Choose: "

if "%CHOICE%"=="0"  goto :done
if /i "%CHOICE%"=="D" goto :hub_app
if /i "%CHOICE%"=="R" goto :reconcile_app
if /i "%CHOICE%"=="U" goto :upgrade_db
if "%CHOICE%"=="1"  goto :status
if "%CHOICE%"=="2"  goto :history
if "%CHOICE%"=="3"  goto :plan
if "%CHOICE%"=="4"  goto :validate
if "%CHOICE%"=="5"  goto :migrate
if "%CHOICE%"=="6"  goto :run
if "%CHOICE%"=="7"  goto :ctl_open
if "%CHOICE%"=="8"  goto :ctl_close
if "%CHOICE%"=="9"  goto :ctl_export
if "%CHOICE%"=="10" goto :ctl_signoff
if "%CHOICE%"=="11" goto :reap
if "%CHOICE%"=="12" goto :retry
if "%CHOICE%"=="13" goto :queue
if "%CHOICE%"=="14" goto :check_copy
echo.
echo  "%CHOICE%" is not one of the options.
goto :pause_menu

:hub_app
echo.
echo  Opening the Migration Hub dashboard in its own window. The browser opens
echo  by itself; if not, use the Local URL that window prints.
echo  Keep that window open while runs are going.
start "AMIGO -- Migration Hub dashboard" /D "%RELEASE_ROOT%" cmd /k ""%PYTHON%" -m streamlit run src\migration_hub\ui\app.py"
goto :pause_menu

:reconcile_app
if not exist "%RECONCILE_ROOT%\app.py" (
    echo.
    echo  CANNOT OPEN RECONCILE: no app.py at
    echo      %RECONCILE_ROOT%
    echo.
    echo  Reconcile is expected beside this folder. If it lives elsewhere, set
    echo  RECONCILE_HOME to its folder and run this again.
    goto :pause_menu
)
if not exist "%RECONCILE_PYTHON%" (
    echo.
    echo  CANNOT OPEN RECONCILE: no virtualenv at
    echo      %RECONCILE_PYTHON%
    echo.
    echo  Create it in %RECONCILE_ROOT% with its own requirements.txt.
    goto :pause_menu
)
echo.
echo  Opening Reconcile in its own window. If the dashboard is already open,
echo  Streamlit picks the next free port -- the window prints the URL.
start "AMIGO -- Reconcile" /D "%RECONCILE_ROOT%" cmd /k ""%RECONCILE_PYTHON%" -m streamlit run app.py"
goto :pause_menu

:upgrade_db
echo.
echo  Brings the registry up to this version's schema (alembic upgrade head).
echo  Run it after copying files that include migrations\versions\, BEFORE
echo  opening the dashboard. It backs the registry up first and shows the
echo  schema revision before and after.
echo.
echo  Only between runs: close the dashboard, and make sure no batch is
echo  migrating -- VM-SETUP.txt (top) says how to check.
call :confirm "Upgrade the registry now" || goto :pause_menu
rem Its own cmd, so it starts from a clean environment -- delayed expansion
rem off, nothing of this menu's -- exactly as when double-clicked. It pauses
rem at its own end, so no second pause here.
cmd /c ""%RELEASE_ROOT%\scripts\upgrade_db.bat""
goto :menu

:status
call :ask_batch || goto :pause_menu
call :hub status --batch "!BATCH!"
goto :pause_menu

:history
call :ask_batch || goto :pause_menu
call :hub controls history --batch "!BATCH!"
goto :pause_menu

:queue
call :hub queue
goto :pause_menu

:check_copy
"%PYTHON%" scripts\check_copy.py
goto :pause_menu

:plan
call :ask_batch || goto :pause_menu
set "SOURCE="
set /p "SOURCE=Source share (blank uses the configured source_root): "
set "LIMIT="
set /p "LIMIT=Max files (blank for no limit; use 1 for a first test): "
set "ARGS=--batch "!BATCH!""
if not "!SOURCE!"=="" set "ARGS=!ARGS! --source "!SOURCE!""
if not "!LIMIT!"=="" set "ARGS=!ARGS! --max-files !LIMIT!"
call :hub plan !ARGS!
goto :pause_menu

:validate
call :ask_batch || goto :pause_menu
call :hub validate --batch "!BATCH!"
goto :pause_menu

:migrate
set "SOURCE="
set /p "SOURCE=Source share (e.g. \\SourceSqlServer\EDM_Backups): "
if "!SOURCE!"=="" (
    echo.
    echo  No source given -- nothing done.
    goto :pause_menu
)
echo.
echo  This discovers, validates, uploads, imports, verifies, archives and closes
echo  every file in !SOURCE! in one go -- and signs off automatically if the run
echo  is CLEAN. A folder always maps to the same batch: new files in a folder
echo  migrated before join that batch.
if defined DRY_RUN_NOTE echo %DRY_RUN_NOTE%
call :confirm "Start the automated migration" || goto :pause_menu
call :hub migrate --source "!SOURCE!"
goto :pause_menu

:run
call :ask_batch || goto :pause_menu
echo.
echo  This uploads to Moody's and starts imports for batch !BATCH!.
if defined DRY_RUN_NOTE echo %DRY_RUN_NOTE%
call :confirm "Start the run" || goto :pause_menu
call :hub run --batch "!BATCH!"
goto :pause_menu

:ctl_open
call :ask_batch || goto :pause_menu
call :hub controls open --batch "!BATCH!" --trigger RUN
goto :pause_menu

:ctl_close
call :ask_run_id || goto :pause_menu
echo.
echo  Closing reconciles against Moody's and FREEZES this run's totals.
echo  It is refused if any file is still in flight -- that is the control
echo  working, not an error to work around.
call :confirm "Close this run" || goto :pause_menu
call :hub controls close --run-id "!RUN_ID!"
goto :pause_menu

:ctl_export
call :ask_run_id || goto :pause_menu
set "OUT="
set /p "OUT=Output file (blank for results\!RUN_ID!.csv): "
if "!OUT!"=="" set "OUT=%RELEASE_ROOT%\results\!RUN_ID!.csv"
call :hub controls export --run-id "!RUN_ID!" --output "!OUT!"
goto :pause_menu

:ctl_signoff
call :ask_run_id || goto :pause_menu
set "WHO="
set /p "WHO=Your name (for the record): "
if "!WHO!"=="" (
    echo.
    echo  A name is required -- sign-off is an audit record.
    goto :pause_menu
)
echo.
echo  Signing off accepts this run's totals as final, and marks the batch DONE
echo  if it is the last outstanding run.
call :confirm "Sign off as !WHO!" || goto :pause_menu
call :hub controls sign-off --run-id "!RUN_ID!" --by "!WHO!"
goto :pause_menu

:reap
echo.
echo  Releases files held by a worker that has stopped -- one whose heartbeat
echo  is older than claim_stale_minutes (10). An upload goes to FAILED for
echo  retry; an import is checked with Data Bridge first, so a finished one is
echo  not re-run. The dashboard's "Release them" button does the same.
call :hub reap
goto :pause_menu

:retry
call :ask_batch || goto :pause_menu
echo.
echo  Retries every FAILED, ABANDONED or ARCHIVE_FAILED file in !BATCH!. Each is
echo  looked up first: already in Data Vault -^> COMPLETED; already on Data
echo  Bridge -^> archived only, never re-uploaded; on neither -^> uploaded again.
echo  A file refused as "Already on Data Bridge" is ADOPTED -- the database
echo  already there becomes this file's copy. Check its last error first.
echo.
echo  If many files failed the same way, STOP and diagnose instead. Retrying
echo  into a systemic fault multiplies it and destroys the evidence.
call :confirm "Retry all failed files in !BATCH!" || goto :pause_menu
call :hub retry --batch "!BATCH!"
goto :pause_menu

rem --------------------------------------------------------------------------
rem  Helpers. `exit /b 1` makes `call :x || goto` work as a guard.
rem --------------------------------------------------------------------------

:hub
rem The release's own interpreter, so no virtualenv needs activating.
"%PYTHON%" -m migration_hub.cli %*
exit /b %errorlevel%

:ask_batch
set "BATCH="
set /p "BATCH=Batch id (e.g. Batch07): "
if "!BATCH!"=="" (
    echo.
    echo  No batch id given -- nothing done.
    exit /b 1
)
exit /b 0

:ask_run_id
set "RUN_ID="
set /p "RUN_ID=Run id (from option 2): "
if "!RUN_ID!"=="" (
    echo.
    echo  No run id given -- nothing done. Use option 2 to list them.
    exit /b 1
)
exit /b 0

:confirm
set "ANSWER="
set /p "ANSWER=%~1? Type YES to proceed: "
if /i not "!ANSWER!"=="YES" (
    echo.
    echo  Not confirmed -- nothing done.
    exit /b 1
)
exit /b 0

:pause_menu
echo.
pause
goto :menu

:done
endlocal
exit /b 0
