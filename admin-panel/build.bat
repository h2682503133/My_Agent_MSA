@echo off
echo ========================================
echo   MyAgent Admin Panel - Build
echo ========================================
echo.

cd /d "%~dp0"

echo [1/2] Restoring NuGet packages...
dotnet restore
if %ERRORLEVEL% neq 0 (
    echo [FAIL] dotnet restore failed
    pause
    exit /b 1
)

echo [2/2] Publishing (single-file executable)...
dotnet publish -c Release -r win-x64 --self-contained false -o publish
if %ERRORLEVEL% neq 0 (
    echo [FAIL] dotnet publish failed
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Build complete!
echo   Output: %~dp0publish\MyAgentAdminPanel.exe
echo ========================================
pause
