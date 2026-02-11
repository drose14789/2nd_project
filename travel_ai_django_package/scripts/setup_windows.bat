@echo off
setlocal

REM TravelAI Django - quick setup (Windows)
REM Run this from the project root folder (travel_ai_django)

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

python manage.py makemigrations
python manage.py migrate

echo.
echo [DONE] Setup complete.
echo - Run: python manage.py createsuperuser
echo - Run: python manage.py runserver
echo.
pause
