"""Per-installation look of the web pages: logo, favicon, colours and texts.

Everything lives in DATA_DIR/themes/, outside the code, so `git pull` never
overwrites it and the code carries no organisation's branding:

    themes/logo.svg (or .png, .jpg, .webp)     shown at the top of the card
    themes/favicon.png (or .ico, .svg)         browser tab icon
    themes/theme.css                           extra CSS, e.g. :root { --accent: #26316c; }
    themes/theme.json                          {"org_name", "welcome", "support", "copyright"}
                                               ("{year}" in copyright becomes the current year)

Every file is optional. The theme is read once at start-up, so restart the
container after changing it.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_ASSET_BYTES = 1024 * 1024
IMAGE_TYPES = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}


@dataclass
class Asset:
    content: bytes
    media_type: str


@dataclass
class Theme:
    org_name: str
    welcome: str | None = None
    support: str | None = None
    copyright: str | None = None
    logo: Asset | None = None
    favicon: Asset | None = None
    css: str | None = None

    @classmethod
    def load(cls, directory: Path, org_name: str) -> "Theme":
        theme = cls(org_name=org_name)
        if not directory.is_dir():
            return theme

        texts_file = directory / "theme.json"
        if texts_file.is_file():
            try:
                texts = json.loads(texts_file.read_text(encoding="utf-8-sig"))
            except ValueError as e:
                logger.error("Ignoring %s: %s", texts_file, e)
                texts = {}
            if isinstance(texts, dict):
                theme.org_name = _text(texts.get("org_name")) or org_name
                theme.welcome = _text(texts.get("welcome"))
                theme.support = _text(texts.get("support"))
                theme.copyright = _text(texts.get("copyright"))

        theme.logo = _image(directory, "logo")
        theme.favicon = _image(directory, "favicon")
        css_file = directory / "theme.css"
        if css_file.is_file() and css_file.stat().st_size <= MAX_ASSET_BYTES:
            theme.css = css_file.read_text(encoding="utf-8")

        logger.info(
            "Theme from %s: logo=%s favicon=%s css=%s",
            directory, bool(theme.logo), bool(theme.favicon), bool(theme.css),
        )
        return theme

    @property
    def copyright_line(self) -> str | None:
        """The copyright text with {year} replaced by the current year."""
        if not self.copyright:
            return None
        return self.copyright.replace("{year}", str(date.today().year))

    @property
    def version(self) -> str:
        """Changes whenever a theme file changes, so browsers never show a cached old logo."""
        digest = hashlib.sha256()
        for part in (self.logo, self.favicon):
            digest.update(part.content if part else b"-")
        digest.update((self.css or "-").encode())
        return digest.hexdigest()[:12]


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _image(directory: Path, stem: str) -> Asset | None:
    for suffix, media_type in IMAGE_TYPES.items():
        path = directory / f"{stem}{suffix}"
        if path.is_file():
            if path.stat().st_size > MAX_ASSET_BYTES:
                logger.error("Ignoring %s: larger than %d bytes", path, MAX_ASSET_BYTES)
                return None
            return Asset(path.read_bytes(), media_type)
    return None
