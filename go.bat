@echo off
chcp 65001 >nul 2>&1
title HERMES OS
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0GO.ps1"
if %ERRORLEVEL% neq 0 pause
