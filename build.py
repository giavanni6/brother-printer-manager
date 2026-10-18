"""Build dist/PrinterManager/PrinterManager.exe (no console). Run: uv run python build.py

One-folder, not --onefile: a one-file exe unpacks ~20 MB to %TEMP%\\_MEI* on every launch
(slower start at every login, and a leftover folder whenever it is killed, e.g. at shutdown).
"""

from pathlib import Path

import PyInstaller.__main__

from printmgr import icons

root = Path(__file__).parent
build = root / "build"
build.mkdir(exist_ok=True)
ico = build / "printer.ico"
icons.render("ok", 256).save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
entry = build / "entry.py"
entry.write_text("from printmgr.app import main\nmain()\n", encoding="utf-8")

PyInstaller.__main__.run([
    str(entry),
    "--name", "PrinterManager",
    "--onedir",
    "--noconsole",
    "--icon", str(ico),
    "--hidden-import", "pystray._win32",
    "--collect-submodules", "zeroconf",
    "--distpath", str(root / "dist"),
    "--workpath", str(build / "work"),
    "--specpath", str(build),
    "--noconfirm",
    "--log-level", "WARN",
])
