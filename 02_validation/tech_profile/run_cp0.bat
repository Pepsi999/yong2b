@echo off
rem run_cp0.bat - TECH_PROFILE v0 CP-0 스키마 점검 실행 (작성일: 2026-07-26 KST)
rem 인코딩: CP949 / 64bit venv 사용 (32bit 환경에서 pandas 오류 이력 있음)
rem 아래 VENV 경로를 C:\stock 하위 64bit venv 실제 경로로 맞춘 뒤 실행.

set VENV=C:\stock\venv64
set CODE=035420

call "%VENV%\Scripts\activate.bat"
if errorlevel 1 (
    echo [오류] venv 활성화 실패: %VENV% 경로를 확인하세요.
    pause
    exit /b 1
)

python -c "import struct; assert struct.calcsize('P')*8==64, '32bit Python'" || (
    echo [오류] 32bit Python 입니다. 64bit venv를 지정하세요.
    pause
    exit /b 1
)

pip show PyMySQL >nul 2>&1 || pip install PyMySQL pandas

python "%~dp0cp0_schema_check.py" --code %CODE% %*
pause
