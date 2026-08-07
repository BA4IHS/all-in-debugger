@echo off
rem Nuitka 打包入口（等价于 python build.py，可后接 --dry-run / --no-archive）
cd /d "%~dp0"
python build.py %*
