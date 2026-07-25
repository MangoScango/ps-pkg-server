@echo off
set PKG_DIRS=\\TOWER\mediapool\downloads\ps4_fpkg;\\TOWER\mediapool\PS5_Games\retail
py -m pip install -r requirements.txt
py -m uvicorn app:app --host 0.0.0.0 --port 8000