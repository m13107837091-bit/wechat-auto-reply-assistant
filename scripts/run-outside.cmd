@echo off
rem Launch the desktop shell from OUTSIDE the workspace.
rem Why: an exe under the DSH workspace gets a reduced token, so WebView2 cannot
rem      create any of its data dirs and the window never opens. See docs/desktop.md.
rem
rem Usage:
rem   run-outside.cmd                   -> copy the debug exe outside, then run it
rem   run-outside.cmd -Mode target-dir  -> CARGO_TARGET_DIR outside + npm run tauri dev
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-outside.ps1" %*
exit /b %ERRORLEVEL%
