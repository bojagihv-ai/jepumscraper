@echo off
chcp 65001 >nul
echo ================================================
echo  JepumScraper - 패키지 설치
echo ================================================
echo.

echo [1/3] 핵심 패키지 설치 중...
py -m pip install flask playwright Pillow ImageHash numpy beautifulsoup4 ^
                  requests python-dotenv openpyxl lxml aiohttp

echo.
echo [2/3] 고급 패키지 설치 중...
py -m pip install torch transformers fake_useragent DrissionPage pywin32 pyautogui

echo.
echo [3/3] Playwright 브라우저 설치 중...
py -m playwright install chromium

echo.
echo ================================================
echo  설치 완료!
echo  실행: 바탕화면 JepumScraper 바로가기 더블클릭
echo  또는: launcher.bat
echo ================================================
pause
