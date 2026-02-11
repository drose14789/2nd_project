#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# TravelAI Django - one-click run (Mac/Linux)
# - Creates venv if missing
# - Installs requirements
# - Creates .env if missing
# - Runs migrations
# - Starts dev server

if [ ! -d venv ]; then
  python3 -m venv venv
fi
source venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[INFO] Created .env from .env.example. Edit it if you want API keys."
fi

python manage.py migrate

echo
echo "[INFO] Starting server at http://127.0.0.1:8000"
echo "[INFO] Admin: python manage.py createsuperuser"
echo
python manage.py runserver
