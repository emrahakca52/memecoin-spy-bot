@echo off
cd /d %~dp0..
python -m pip install -r mt5_bridge\requirements.txt
python -m mt5_bridge.worker
pause
