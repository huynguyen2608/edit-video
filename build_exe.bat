@echo off
REM ============================================================
REM  Build ung dung GUI thanh 1 file .exe click-de-chay (Python 3.12).
REM  Ket qua: dist\VideoRepurposeStudio\VideoRepurposeStudio.exe
REM ============================================================
cd /d "%~dp0"

REM Chon interpreter: uu tien .venv, roi py -3.12
set PY=py -3.12
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

echo [1/3] Cai dependencies + PyInstaller (Python 3.12)...
%PY% -m pip install -r requirements.txt pyinstaller || goto :err

echo [2/3] Dang build (--windowed, khong console)...
%PY% -m PyInstaller --noconfirm --clean --windowed --name VideoRepurposeStudio ^
    --add-data "config.example.yaml;." ^
    run.py || goto :err

echo [3/3] Xong!
echo   -> Chay: dist\VideoRepurposeStudio\VideoRepurposeStudio.exe
echo   Luu y: config.yaml / data.xlsx / logs se tao ben canh file .exe khi chay.
pause
goto :eof

:err
echo.
echo BUILD LOI. Kiem tra: da cai Python 3.12 chua? mang co on dinh khong?
pause
