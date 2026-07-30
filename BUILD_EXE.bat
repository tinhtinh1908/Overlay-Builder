@echo off
setlocal
cd /d "%~dp0"
title DTinh Overlay Builder - EXE Packager
set "DTINH_BUILD_TEMP=%TEMP%\DTinhOverlayBuilder_%RANDOM%_%RANDOM%"
set "DTINH_BUILD_WORK=%DTINH_BUILD_TEMP%\work"
set "DTINH_BUILD_DIST=%DTINH_BUILD_TEMP%\dist"
set "DTINH_BUILD_SPEC=%DTINH_BUILD_TEMP%\spec"

where python >nul 2>nul || (
  echo [ERROR] Chua cai Python 3.
  pause
  exit /b 1
)
python -m pip install --upgrade pyinstaller
if errorlevel 1 (
  echo [ERROR] Khong cai duoc PyInstaller.
  pause
  exit /b 1
)
python -c "compile(open(r'DTinhOverlayBuilder.pyw', encoding='utf-8-sig').read(), r'DTinhOverlayBuilder.pyw', 'exec')"
if errorlevel 1 (
  echo [ERROR] Source Python bi loi cu phap.
  pause
  exit /b 1
)
python -m PyInstaller --noconfirm --clean --onefile --windowed --optimize 2 ^
  --name DTinhOverlayBuilder ^
  --workpath "%DTINH_BUILD_WORK%" ^
  --distpath "%DTINH_BUILD_DIST%" ^
  --specpath "%DTINH_BUILD_SPEC%" ^
  DTinhOverlayBuilder.pyw

if exist "%DTINH_BUILD_DIST%\DTinhOverlayBuilder.exe" (
  copy /y "%DTINH_BUILD_DIST%\DTinhOverlayBuilder.exe" "%~dp0DTinhOverlayBuilder.exe" >nul
  echo [OK] Da tao DTinhOverlayBuilder.exe
  echo [NOTE] File build tam da duoc dat trong %%TEMP%% va se tu dong don.
) else (
  echo [ERROR] Khong tao duoc EXE.
)

if exist "%DTINH_BUILD_TEMP%" rmdir /s /q "%DTINH_BUILD_TEMP%"
pause
