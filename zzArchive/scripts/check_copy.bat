@echo off
setlocal

rem ===========================================================================
rem  AMIGO -- verify this copy against the manifest it shipped with
rem
rem  Double-click this, or run it from a shell. It answers the one question the
rem  transfer route cannot: is this copy the one that was published, and has
rem  anyone edited it since?
rem
rem  NO VIRTUALENV, DELIBERATELY. check_copy.py imports only the standard
rem  library, so this runs on a tree that has been pasted but not yet
rem  installed -- which is exactly when you want it. Requiring a venv would
rem  mean the check could not run until after the step it is meant to gate.
rem
rem  --all IS THE DEFAULT HERE, and is not in the script. The script skips
rem  config-class files because an operator edits them and reporting those as
rem  drift trains people to ignore the output. That is right for a re-check
rem  and wrong for a first setup, where nothing has been edited yet and a
rem  missing pyproject.toml passes silently -- which is how one reached a pip
rem  error instead of this check (2026-09-21). Pass /quick to get the script's
rem  own default back.
rem
rem      check_copy.bat            everything, including config files
rem      check_copy.bat /quick     skip config files
rem ===========================================================================

rem --- This file ships in scripts\, so the tree root is one level up.
for %%I in ("%~dp0..") do set "ROOT=%%~fI"

title AMIGO -- check copy

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo  CANNOT START: python is not on PATH.
    echo.
    echo  This check needs only a plain Python install -- no virtualenv and no
    echo  dependencies. Install Python, or open a shell where python runs.
    echo.
    goto :halt
)

if not exist "%ROOT%\MANIFEST.txt" (
    echo.
    echo  CANNOT CHECK: no MANIFEST.txt at
    echo      %ROOT%
    echo.
    echo  The manifest travels with the code -- it is what this compares
    echo  against. Copy MANIFEST.txt and VERSION.txt from the published tree,
    echo  they sit at its root rather than in scripts\.
    echo.
    goto :halt
)

if /i "%~1"=="/quick" (
    python "%~dp0check_copy.py" --root "%ROOT%"
) else (
    python "%~dp0check_copy.py" --root "%ROOT%" --all
)
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo  ============================================================
    echo   This copy matches what was published.
    echo  ============================================================
) else (
    echo  ============================================================
    echo   PROBLEMS FOUND -- read the list above before going further.
    echo  ============================================================
    echo.
    echo   EMPTY   scaffolded but never pasted. Imports cleanly and fails
    echo           much later, so it is worth fixing now. On a staged
    echo           transfer, files you have not copied yet are expected
    echo           to show here.
    echo   MISSING never arrived.
    echo   DIFFER  content is not what was published.
    echo.
    echo   A file marked "edited here" was changed on this machine. That
    echo   change is lost on the next publish and was made blind, since
    echo   the explanatory comments are stripped from this copy. Move it
    echo   into the AML-MigHub repo instead.
)

:halt
echo.
pause
endlocal
