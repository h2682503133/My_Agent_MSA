@echo off
title My_Agent MSA Port Forward

echo ========================================
echo   My_Agent MSA - Port Forward
echo ========================================
echo.

REM Kill old kubectl port-forwards
taskkill /F /IM kubectl.exe >nul 2>&1

REM Start port-forwards in separate windows
start "dashboard" cmd /c "kubectl -n agent port-forward svc/dashboard-service 5601:5601"
start "gateway" cmd /c "kubectl -n agent port-forward svc/gateway-backend-service 5210:5210"
start "istio" cmd /c "kubectl -n istio-system port-forward svc/istio-ingressgateway 8080:80"

echo Port forwards started:
echo   Dashboard  : http://localhost:5601
echo   Web        : http://localhost:8080
echo   API        : http://localhost:5210
echo.
echo Type exit to stop all forwards.
echo.

:loop
set /p cmd=">> "
if /i "%cmd%"=="exit" goto cleanup
goto loop

:cleanup
echo Stopping...
taskkill /F /IM kubectl.exe >nul 2>&1
echo Done.
pause
