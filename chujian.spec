# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("webview", "mitmproxy", "mitmproxy_rs", "pythonnet", "clr_loader",
            "uvicorn", "fastapi", "starlette", "pydantic", "anyio"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        print("collect_all failed:", pkg, e)

hiddenimports += [
    "app", "app.web", "app.scheduler", "app.proxy_host", "app.sysproxy",
    "app.adapters.pupu", "app.adapters.parser", "app.adapters.mock", "app.classify",
    "tools", "tools.grab_token",
]

a = Analysis(
    ["desktop.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PyQt5", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ChujianMonitor",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="ChujianMonitor",
)
