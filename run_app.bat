@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv || goto error
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto error
echo Starting Squatch Signal at http://localhost:8501
".venv\Scripts\python.exe" -m streamlit run app.py || goto error
goto end
:error
echo Something went wrong - see messages above.
pause
exit /b 1
:end
endlocal
