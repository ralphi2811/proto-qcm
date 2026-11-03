"""Version de l'application : injectée à la construction de l'image (CI), sinon lue
dans pyproject.toml (développement)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path


def _pyproject_version() -> str:
    try:
        with open(Path(__file__).resolve().parent.parent / "pyproject.toml", "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "dev"


VERSION = os.environ.get("APP_VERSION") or _pyproject_version()
COMMIT = os.environ.get("APP_COMMIT", "")[:7] or "dev"
BUILT = os.environ.get("APP_BUILT", "")
# identifiant unique d'un déploiement : change le cache du service worker à chaque mise à jour
BUILD_ID = f"{VERSION}-{COMMIT}"


def info() -> dict:
    return {"version": VERSION, "commit": COMMIT, "built": BUILT}
