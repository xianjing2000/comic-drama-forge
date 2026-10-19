@echo off
REM ============================================================================
REM  安装「tasks.db 每日备份」计划任务（F-4，2026-10-16）
REM ----------------------------------------------------------------------------
REM  T23 实测生产 tasks.db 无任何备份（单点损坏即丢任务/凭证/设置）。
REM  本脚本把 tools\backup_db.py --apply 注册成每天 03:00 的本机计划任务。
REM  用法：右键「以管理员身份运行」，或双击（会请求提权）。
REM  卸载：schtasks /delete /tn MJSCXT_DBBackup /f
REM ============================================================================
setlocal
set "PROJ=%~dp0.."
set "PY=python"
set "TASK=MJSCXT_DBBackup"

where %PY% >nul 2>nul || (
  echo [错误] 找不到 python（请确保后端用的解释器在 PATH，或改本脚本的 PY 变量）。
  pause & exit /b 1
)
if not exist "%PROJ%\tools\backup_db.py" (
  echo [错误] 未找到 %PROJ%\tools\backup_db.py
  pause & exit /b 1
)

echo 将注册计划任务 %TASK%：每天 03:00 运行  " %PY% %PROJ%\tools\backup_db.py --apply "
schtasks /create /tn "%TASK%" /sc daily /st 03:00 /f ^
  /tr "\"%PY%\" \"%PROJ%\tools\backup_db.py\" --apply"
if errorlevel 1 ( echo [失败] 创建计划任务失败。 & pause & exit /b 1 )

echo.
echo [完成] 已创建。查看：schtasks /query /tn %TASK% /v /fo LIST
echo        立即试跑：%PY% "%PROJ%\tools\backup_db.py"   （预演，不写）
pause
endlocal
