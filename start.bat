@echo off
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set PY=py -3
) else (
    where python >nul 2>nul
    if errorlevel 1 goto nopython
    set PY=python
)

echo 원생노트를 시작합니다. 잠시 후 브라우저가 열립니다.
echo 사용이 끝나면 이 창을 닫으세요. (창을 닫기 전에는 프로그램이 종료되지 않습니다)
echo.
%PY% -m academy.server --local
echo.
echo 프로그램이 종료되었습니다. 위에 오류 메시지가 있다면 내용을 알려 주세요.
pause
exit /b

:nopython
echo Python이 설치되어 있지 않습니다.
echo https://www.python.org/downloads/ 에서 Python을 설치해 주세요.
echo 설치 첫 화면에서 "Add python.exe to PATH"를 반드시 체크하세요.
echo 설치 후 이 파일을 다시 더블클릭하세요.
pause
