@echo off
chcp 65001 >nul
title AdvvxClips
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar.ps1"
if errorlevel 1 pause
