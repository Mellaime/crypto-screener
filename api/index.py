"""
api/index.py  -  Vercel serverless entry point.

Vercel runs files in the /api folder as serverless functions. This one simply
exposes our existing FastAPI app (from backend/main.py) as `app`, which
Vercel's Python runtime serves as an ASGI application.

vercel.json routes every /api/* request here, so FastAPI handles the real
paths (/api/coins, /api/dropped, /api/cron/daily).
"""
import os
import sys

# backend/ lives one level up from this /api folder; put it on the import path
# so "from main import app" (and its imports of database, models, ...) resolve.
BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, BACKEND_DIR)

from main import app  # noqa: E402  -> the FastAPI application Vercel will serve
