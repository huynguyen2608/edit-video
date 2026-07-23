@echo off
REM ============================================================
REM  Click-de-chay ung dung GUI (Python 3.12).
REM  Uu tien dung virtualenv .venv neu co, khong thi dung py -3.12.
REM ============================================================
cd /d "%~dp0"
title Video Repurpose Studio

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run.py
    goto :end
)

py -3.12 --version >nul 2>&1
if %errorlevel%==0 (
    py -3.12 run.py
    goto :end
)

echo Khong tim thay Python 3.12 hoac .venv.
echo   1) Cai Python 3.12 (https://www.python.org/downloads/release), HOAC
echo   2) Tao moi truong ao:  py -3.12 -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt
pause
goto :eof

:end
REM Neu app thoat kem loi, giu cua so de doc thong bao.
if %errorlevel% neq 0 pause
:eof
