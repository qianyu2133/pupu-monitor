"""在机器上找一个「装了指定包」的 Python。

因为双击 bat 时 cmd 用的 python 和开发时用的可能不是同一个，
所以不能假设 sys.executable 就是对的。
"""
import glob
import os
import shutil
import subprocess
import sys


def candidate_pythons():
    seen, out = set(), []

    def add(p):
        if not p:
            return
        p = os.path.abspath(p)
        if not os.path.exists(p) or p in seen:
            return
        seen.add(p)
        out.append(p)

    add(sys.executable)
    for name in ("python", "python3"):
        add(shutil.which(name))

    patterns = [
        os.path.expanduser("~/.workbuddy/binaries/python/versions/*/python.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python3*\python.exe"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\Programs\Python\Python3*\python.exe"),
        r"C:\Python3*\python.exe",
        r"C:\Program Files\Python3*\python.exe",
    ]
    for pat in patterns:
        for p in sorted(glob.glob(pat), reverse=True):
            add(p)
    return out


def has_modules(py, modules):
    code = "import " + ", ".join(modules)
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def find(modules):
    for py in candidate_pythons():
        if has_modules(py, modules):
            return py
    return None


def describe(modules):
    lines = []
    for py in candidate_pythons():
        try:
            ver = subprocess.run([py, "-c", "import sys;print(sys.version.split()[0])"],
                                 capture_output=True, timeout=15).stdout.decode(errors="replace").strip()
        except Exception:
            ver = "?"
        mark = "OK " if has_modules(py, modules) else " - "
        lines.append(f"  {mark} {py}  (Python {ver})")
    return "\n".join(lines)
