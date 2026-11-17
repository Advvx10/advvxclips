import argparse
import os

from .server import serve

ap = argparse.ArgumentParser(description="AdvvxClips")
# En un servidor el puerto lo da la variable PORT (Render, Fly.io...).
ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
ap.add_argument("--host", type=str, default=None, help="127.0.0.1 (local) o 0.0.0.0 (para el link público)")
ap.add_argument("--no-browser", action="store_true")
a = ap.parse_args()
serve(a.port, not a.no_browser, a.host)
