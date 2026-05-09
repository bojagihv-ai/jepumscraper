@echo off
chcp 65001 >nul
echo ==================================================
echo   네이버 로그인 버튼 클릭 테스트 (AHK 직접 실행)
echo ==================================================
echo.
echo [안내] Chrome에 네이버 로그인 페이지가 화면에 보이는 상태인지 확인하세요.
echo.

set "AHK_EXE="
if exist "C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe" set "AHK_EXE=C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe"
if exist "C:\Program Files\AutoHotkey\v2\AutoHotkey.exe" set "AHK_EXE=C:\Program Files\AutoHotkey\v2\AutoHotkey.exe"
if exist "C:\Program Files\AutoHotkey\AutoHotkey64.exe" set "AHK_EXE=C:\Program Files\AutoHotkey\AutoHotkey64.exe"
if exist "C:\Program Files\AutoHotkey\AutoHotkey.exe" set "AHK_EXE=C:\Program Files\AutoHotkey\AutoHotkey.exe"

if "%AHK_EXE%"=="" (
    echo [-] AutoHotkey 실행파일을 찾지 못했습니다.
    pause
    exit /b 1
)

echo [+] 발견: %AHK_EXE%
echo [+] 3초 후 클릭을 시도합니다...
timeout /t 3 /nobreak >nul

set "SCRIPT_V2=C:\JepumScraper\tools\naver_login_click_v2.ahk"
set "SCRIPT_V1=C:\JepumScraper\tools\naver_login_click_v1.ahk"

echo %AHK_EXE% | findstr /i "v2" >nul
if errorlevel 1 (
    echo [+] AHK v1 스크립트 실행 중...
    "%AHK_EXE%" "%SCRIPT_V1%" "" "10" "400"
) else (
    echo [+] AHK v2 스크립트 실행 중...
    "%AHK_EXE%" "%SCRIPT_V2%" "" "10" "400"
)

if %ERRORLEVEL% equ 0 (
    echo.
    echo [성공] 버튼 클릭 완료
) else (
    echo.
    echo [실패] 10초 타임아웃
)
echo ==================================================
pause
