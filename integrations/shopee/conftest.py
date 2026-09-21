import sys
from pathlib import Path

SHOPEE_DIR = Path(__file__).resolve().parent
AUTH_DIR = SHOPEE_DIR / "auth"
for path in (str(SHOPEE_DIR), str(AUTH_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)
