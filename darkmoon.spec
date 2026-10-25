# PyInstaller spec — run via build.bat (or: pyinstaller --noconfirm darkmoon.spec)
# -*- mode: python ; coding: utf-8 -*-
import sys
from PyInstaller.utils.hooks import collect_submodules

datas = [
    ("web", "web"), ("themes", "themes"), ("ui_plugins", "ui_plugins"),
    ("plugins", "plugins"), ("assets", "assets"), ("models", "models"), ("config.example.toml", "."),
]
hidden = (
    collect_submodules("uvicorn") + collect_submodules("app") + collect_submodules("ddgs")
    + ["webview", "pystray", "pynput", "PIL.Image"]
)
if sys.platform == "win32":
    hidden += ["pystray._win32", "pynput.keyboard._win32", "pynput.mouse._win32", "webview.platforms.edgechromium",
               "webview.platforms.winforms", "clr"]

a = Analysis(["launcher.py"], pathex=["."], datas=datas, hiddenimports=hidden, excludes=["tkinter"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Darkmoon", console=False,
          icon="assets/icon.ico", upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Darkmoon", upx=False)
