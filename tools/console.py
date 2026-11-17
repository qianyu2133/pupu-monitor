"""启动控制台（给 start_console.bat 调用的）。

自动找一个装了 fastapi + uvicorn 的 Python 再跑 run.py。
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _pyfind

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    if not os.path.exists(os.path.join(ROOT, "run.py")):
        print(f"[x] 找不到 run.py（{ROOT}）")
        return 1

    py = _pyfind.find(["fastapi", "uvicorn"])
    if not py:
        print("[x] 没找到装了 fastapi / uvicorn 的 Python。")
        print("\n扫到的解释器：")
        print(_pyfind.describe(["fastapi", "uvicorn"]))
        print(f'\n解决：用任意一个带 OK 的 python 装依赖')
        print(f'  "{sys.executable}" -m pip install -r requirements.txt')
        return 1

    print(f"[ok] python: {py}")
    print("控制台: http://127.0.0.1:8787")
    try:
        return subprocess.call([py, "run.py"], cwd=ROOT)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
