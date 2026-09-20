@echo off
setlocal EnableDelayedExpansion

rem ===========================================================================
rem  AMIGO -- operator menu (TASK-0071)
rem
rem  Double-click this, or run it from a shell. It activates the release's own
rem  virtualenv and drives `migration-hub`, so an operator never has to know
rem  either exists.
rem
rem  DESIGN NOTES, because the safe choices here look like fussy ones:
rem
rem  * Nothing destructive runs without a typed confirmation. An operator will
rem    double-click this twice; a second run must not be able to damage a batch.
rem  * `plan` is offered freely -- it is read-only and registers files already
rem    present. `run`, `retry` and `controls close/sign-off` all confirm first.
rem  * Failures are reported as a sentence naming the file to fix, never as a
rem    Python traceback. An operator cannot act on a stack trace.
rem  * This is NOT a scheduler and does NOT call RatCat. Amlin runs RatCat and
rem    AMIGO reads the share it wrote to (TASK-0065). Pre-staging must FINISH
rem    before discovery runs.
rem ===========================================================================

rem --- Locate the install. This file ships inside a release, so the release
rem --- root is two levels up from scripts\. An explicit AMIGO_HOME wins.
if defined AMIGO_HOME (
    set "RELEASE_ROOT=%AMIGO_HOME%"
) else (
    for %%I in ("%~dp0..") do set "RELEASE_ROOT=%%~fI"
)

set "VENV_ACTIVATE=%RELEASE_ROOT%\.venv\Scripts\activate.bat"
set "INSTALL_ROOT=%RELEASE_ROOT%\..\.."

title AMIGO -- Migration Hub

rem --- Preflight. Every failure below names what to fix, in one sentence.
if not exist "%VENV_ACTIVATE%" (
    echo.
    echo  CANNOT START: no virtualenv found at
    echo      %VENV_ACTIVATE%
    echo.
    echo  This usually means the release was copied by hand rather than
    echo  installed. Run Install-Release.ps1 -- see
    echo  docs\operations\OPERATOR-GUIDE.md section 1.
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
    echo  If the release folder was moved after installation, the virtualenv's
    echo  recorded paths are stale -- reinstall rather than copying it.
    echo.
    goto :halt
)

if not defined MIGRATION_HUB_ENV (
    echo.
    echo  CANNOT START: MIGRATION_HUB_ENV is not set.
    echo.
    echo  It names which config file to load, e.g. "prod" loads config\prod.json.
    echo  Set it in the .env file beside this release, or in the environment.
    echo  See docs\operations\OPERATOR-GUIDE.md section 3.
    echo.
    goto :halt
)

if not defined MOODYS_API_KEY (
    echo.
    echo  CANNOT START: MOODYS_API_KEY is not set.
    echo.
    echo  Put it in the .env file -- never in a config file, and never in
    echo  anything committed to source control.
    echo  See docs\operations\OPERATOR-GUIDE.md section 3.
    echo.
    goto :halt
)

rem --- Warn, but do not block, on dry_run. It is a legitimate state to be in;
rem --- the danger is being in it WITHOUT KNOWING, since a dry run looks
rem --- exactly like a successful one.
set "DRY_RUN_NOTE="
if exist "%INSTALL_ROOT%\config\%MIGRATION_HUB_ENV%.json" (
    findstr /i /c:"\"dry_run\": true" "%INSTALL_ROOT%\config\%MIGRATION_HUB_ENV%.json" >nul 2>&1
    if not errorlevel 1 set "DRY_RUN_NOTE=  *** dry_run is TRUE -- no files will actually be uploaded ***"
)

:menu
cls
echo ===========================================================================
echo   AMIGO -- Migration Hub          environment: %MIGRATION_HUB_ENV%
echo ===========================================================================
if defined DRY_RUN_NOTE echo %DRY_RUN_NOTE%
echo.
echo   Before a wave: RatCat pre-staging must have FINISHED. AMIGO never calls
echo   RatCat -- it reads the share RatCat wrote to.
echo.
echo   --- look, changes nothing ---
echo     1. Status of a batch
echo     2. Control-record history for a batch
echo.
echo   --- run a wave ---
echo     3. Plan      (register files -- read-only, safe to repeat)
echo     4. Validate  (naming, extension, size)
echo     5. Stage     (no-op unless stage_locally is true)
echo     6. Run       (uploads and imports -- CONFIRMS FIRST)
echo.
echo   --- close a wave ---
echo     7. Open a control record
echo     8. Close a run      (reconciles and freezes totals -- CONFIRMS FIRST)
echo     9. Export evidence
echo    10. Sign off a run   (marks the batch DONE -- CONFIRMS FIRST)
echo.
echo   --- recovery ---
echo    11. Reap stale claims (release a dead worker's files)
echo    12. Retry failed files in a batch  (CONFIRMS FIRST)
echo.
echo     0. Quit
echo.
set "CHOICE="
set /p "CHOICE=Choose: "

if "%CHOICE%"=="0" goto :done
if "%CHOICE%"=="1"  goto :status
if "%CHOICE%"=="2"  goto :history
if "%CHOICE%"=="3"  goto :plan
if "%CHOICE%"=="4"  goto :validate
if "%CHOICE%"=="5"  goto :stage
if "%CHOICE%"=="6"  goto :run
if "%CHOICE%"=="7"  goto :ctl_open
if "%CHOICE%"=="8"  goto :ctl_close
if "%CHOICE%"=="9"  goto :ctl_export
if "%CHOICE%"=="10" goto :ctl_signoff
if "%CHOICE%"=="11" goto :reap
if "%CHOICE%"=="12" goto :retry
echo.
echo  "%CHOICE%" is not one of the options.
goto :pause_menu

:status
call :ask_batch || goto :pause_menu
migration-hub status --batch "!BATCH!"
goto :pause_menu

:history
call :ask_batch || goto :pause_menu
migration-hub controls history --batch "!BATCH!"
goto :pause_menu

:plan
call :ask_batch || goto :pause_menu
set "SOURCE="
set /p "SOURCE=Source share (blank uses the configured source_root): "
set "LIMIT="
set /p "LIMIT=Max files (blank for no limit; use 1 for a first test): "
set "ARGS=--batch !BATCH!"
if not "!SOURCE!"=="" set "ARGS=!ARGS! --source "!SOURCE!""
if not "!LIMIT!"=="" set "ARGS=!ARGS! --max-files !LIMIT!"
migration-hub plan !ARGS!
goto :pause_menu

:validate
call :ask_batch || goto :pause_menu
migration-hub validate --batch "!BATCH!"
goto :pause_menu

:stage
call :ask_batch || goto :pause_menu
migration-hub stage --batch "!BATCH!"
goto :pause_menu

:run
call :ask_batch || goto :pause_menu
echo.
echo  This uploads to Moody's and starts imports for batch !BATCH!.
if defined DRY_RUN_NOTE echo %DRY_RUN_NOTE%
call :confirm "Start the run" || goto :pause_menu
migration-hub run --batch "!BATCH!"
goto :pause_menu

:ctl_open
call :ask_batch || goto :pause_menu
migration-hub controls open --batch "!BATCH!" --trigger RUN
goto :pause_menu

:ctl_close
call :ask_run_id || goto :pause_menu
echo.
echo  Closing reconciles against Moody's and FREEZES this run's totals.
echo  It is refused if any file is still in flight -- that is the control
echo  working, not an error to work around.
call :confirm "Close this run" || goto :pause_menu
migration-hub controls close --run-id "!RUN_ID!"
goto :pause_menu

:ctl_export
call :ask_run_id || goto :pause_menu
set "OUT="
set /p "OUT=Output file (blank for results\!RUN_ID!.csv): "
if "!OUT!"=="" set "OUT=%INSTALL_ROOT%\results\!RUN_ID!.csv"
migration-hub controls export --run-id "!RUN_ID!" --output "!OUT!"
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
migration-hub controls sign-off --run-id "!RUN_ID!" --by "!WHO!"
goto :pause_menu

:reap
echo.
echo  Reap releases files still claimed by a worker that died. It checks the
echo  vendor's own job status first, so a finished import is not re-run.
migration-hub reap
goto :pause_menu

:retry
call :ask_batch || goto :pause_menu
echo.
echo  This requeues EVERY failed or abandoned file in !BATCH! and starts a
echo  worker. It is a blind retry -- it does not check whether a file already
echo  landed on the platform.
echo.
echo  If many files failed the same way, STOP and diagnose instead. Retrying
echo  into a systemic fault multiplies it and destroys the evidence.
call :confirm "Retry all failed files in !BATCH!" || goto :pause_menu
migration-hub retry --batch "!BATCH!"
goto :pause_menu

rem --------------------------------------------------------------------------
rem  Helpers. `exit /b 1` makes `call :x || goto` work as a guard.
rem --------------------------------------------------------------------------

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
set /p "RUN_ID=Run id (from 'controls history'): "
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

:halt
pause
endlocal
exit /b 1

:done
endlocal
exit /b 0
