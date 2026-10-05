@echo off
rem start.bat - double-click launcher (pick a quantized GGUF model).
rem Usage: start.bat            -> interactive model menu
rem        start.bat Q5_K_S     -> start a specific quant directly
rem NOTE: keep this file ASCII + CRLF. cmd.exe parses batch files by byte
rem       offset under the OEM code page, so LF-only line endings plus
rem       non-ASCII bytes shift the parser and corrupt the first command.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
pause
