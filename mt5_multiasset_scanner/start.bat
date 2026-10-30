@echo off
cd /d %~dp0..
python -m pip install -r mt5_multiasset_scanner\requirements.txt
python -m mt5_multiasset_scanner.worker
pause
