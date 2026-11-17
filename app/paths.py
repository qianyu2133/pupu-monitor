"""统一的根目录：开发时是项目目录，打包后是 exe 所在目录。"""
import os
import sys


def root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    d = os.path.join(root(), "data")
    os.makedirs(d, exist_ok=True)
    return d
