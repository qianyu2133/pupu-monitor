import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn

from app import db, sysproxy
from app.web import app

if __name__ == "__main__":
    db.init()
    sysproxy.heal_on_start()
    print("控制台: http://127.0.0.1:8787")
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
