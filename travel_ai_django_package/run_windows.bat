@echo off
setlocal
cd /d "%~dp0"

REM TravelAI Django - one-click run (Windows)
REM - Creates venv if missing
REM - Installs requirements
REM - Creates .env if missing
REM - Runs migrations
REM - Starts dev server

if not exist venv (
  python -m venv venv
)

call venv\Scripts\activate

python -m pip install --upgrade pip
pip install -r requirements.txt

if not exist .env (
  copy .env.example .env
  echo [INFO] Created .env from .env.example. Edit it if you want API keys.
)

python manage.py migrate

echo.
echo [INFO] Starting server at http://127.0.0.1:8000
echo [INFO] Admin: python manage.py createsuperuser
echo.
python manage.py runserver
