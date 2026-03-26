@echo off
REM JobHunter Daily Pipeline — runs scrape, tailor, and apply
cd /d "c:\Users\kaina\OneDrive\Documents\JobHunter"

REM Activate venv if it exists
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

REM Create log directory if needed
if not exist "data\logs" mkdir "data\logs"

REM Run the full pipeline, logging output
python -m src pipeline >> "data\logs\pipeline_%date:~-4%-%date:~4,2%-%date:~7,2%.log" 2>&1
