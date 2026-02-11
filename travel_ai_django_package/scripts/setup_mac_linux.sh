#!/usr/bin/env bash
set -euo pipefail

# TravelAI Django - quick setup (macOS/Linux)
# Run this from the project root folder (travel_ai_django)

if [ ! -d "venv" ]; then
  python3 -m venv venv
fi

source venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "[INFO] Created .env from .env.example. Edit it if you want API keys."
fi

python manage.py makemigrations
python manage.py migrate

echo ""
echo "[DONE] Setup complete."
echo " - Run: python manage.py createsuperuser"
echo " - Run: python manage.py runserver"
