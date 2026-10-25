"""Where things live.

APP_DIR  = bundled, read-only resources (web UI, built-in themes, example config).
DATA_DIR = per-user, writable data (config.toml, history DB, your themes,
           your plugins, QuickCSS). Survives app updates.

Override DATA_DIR with the DARKMOON_HOME environment variable.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    env = os.environ.get("DARKMOON_HOME") or os.environ.get("LOCALAGENT_HOME")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "Darkmoon"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Darkmoon"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "darkmoon"


APP_DIR = _app_dir()
DATA_DIR = _data_dir()

WEB_DIR = APP_DIR / "web"
BUILTIN_THEMES_DIR = APP_DIR / "themes"
BUILTIN_UI_PLUGINS_DIR = APP_DIR / "ui_plugins"
BUILTIN_TOOL_PLUGINS_DIR = APP_DIR / "plugins"
EXAMPLE_CONFIG = APP_DIR / "config.example.toml"

CONFIG_FILE = DATA_DIR / "config.toml"
DB_FILE = DATA_DIR / "history.db"
USER_THEMES_DIR = DATA_DIR / "themes"
USER_UI_PLUGINS_DIR = DATA_DIR / "ui_plugins"
USER_TOOL_PLUGINS_DIR = DATA_DIR / "plugins"
QUICKCSS_FILE = DATA_DIR / "quickcss.css"
UI_SETTINGS_FILE = DATA_DIR / "ui_settings.json"
LOG_FILE = DATA_DIR / "darkmoon.log"


def ensure_dirs() -> None:
    for d in (DATA_DIR, USER_THEMES_DIR, USER_UI_PLUGINS_DIR, USER_TOOL_PLUGINS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists() and EXAMPLE_CONFIG.exists():
        shutil.copy(EXAMPLE_CONFIG, CONFIG_FILE)
    if not QUICKCSS_FILE.exists():
        QUICKCSS_FILE.write_text(
            "/* QuickCSS — anything here is applied on top of your theme, live. */\n",
            encoding="utf-8",
        )
    readme = USER_THEMES_DIR / "README.txt"
    if not readme.exists():
        readme.write_text(
            "Drop .css theme files here. They appear in Settings > Themes.\n"
            "Start from any built-in theme; see the main README for the variable list.\n",
            encoding="utf-8",
        )
