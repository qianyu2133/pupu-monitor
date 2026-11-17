"""一键发版：打包 → 同步数据 → 生成干净分发包。

用法：
    python tools/release.py onefile     只打单文件版
    python tools/release.py onedir      只打文件夹版
    python tools/release.py both        两个都打（默认）
    python tools/release.py clean       只重新生成干净版（不打包）

注意：
- 会覆盖 dist/ 下同名产物
- 干净版不含 data / secret.key / *.pem，可以放心发给别人
"""
import json
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
ONEDIR = os.path.join(DIST, "ChujianMonitor")
ONEFILE = os.path.join(DIST, "ChujianMonitor.exe")
CLEAN = os.path.join(DIST, "出清监控-干净版")
CLEAN_ZIP = os.path.join(DIST, "出清监控-干净版.zip")

# 打包时要同步给"自己用"的版本的文件
USER_FILES = ["data/chujian.db", "data/secret.key", "config.json"]
USER_DIRS = ["data/mitm"]

README = """出清监控 · 使用说明
====================================

【第一步】双击 ChujianMonitor.exe
  首次打开会自动弹出向导，共三步。

【第二步】信任证书
  点「安装」。把本程序的根证书装进「当前用户」的受信任根存储，
  只影响这台电脑，不需要管理员权限。随时可在向导里卸载。

【第三步】开启系统代理
  点「开启」。此时本机上网会经过本程序，结束后自动还原。

【第四步】抓凭证
  打开 Windows 版微信 → 搜索「朴朴超市」小程序 → 点一下「购物车」。
  向导会显示「已捕获 N 条请求」，拿到凭证后「完成」按钮自动亮起。
  点完成后自动关闭代理、还原网络设置。

【之后每次打开】
  直接进主界面：
    - 购物车：监控中的商品。点 ☆ 标记重点，重点商品降 1 分钱就推送
    - 出清池：点「抓取疯狂折扣」拉全量，可按折扣和分类（肉类/海鲜/其它）筛选
    - 推送：填企业微信群机器人 webhook，降价会推到微信

【注意事项】
  1. 只读写你自己账号的数据，不上传任何服务器。
  2. 账号数据存在 data 目录（加密）。换账号删掉 data 目录即可。
  3. token 约几周过期。过期后凭证显示「需重抓」，
     点右上角「接入账号」重走向导，约 30 秒，不用重新配置。
  4. 「一键锁单」默认关闭 —— 它会真的提交订单占库存。
     要用请到「账号与接口」里手动打勾，风险自负。
  5. 未做代码签名。首次运行若被 Windows 拦截，点「更多信息 → 仍要运行」。
"""

CLEAN_CONFIG = {
    "pupu": {
        "host": "https://j1.pupuapi.com",
        "cart_path": "/client/shopping_cart/shopping_cart/detail/v3",
        "cart_method": "POST",
        "refresh_path": "",
        "price_scale": 0.01,
    },
    "notify": {"wecom_webhook": "", "public_base": "http://127.0.0.1:8787"},
    "poll": {
        "peak_start": 21,
        "peak_end": 23,
        "peak_interval": 12,
        "idle_interval": 180,
        "jitter": 0.3,
    },
    "rule": {"min_drop_pct": 5.0, "min_drop_abs": 0.5, "alert_on_clearance_tag": True},
    "danger": {"allow_submit_order": False},
}


def sh(cmd, label=""):
    print(f"\n>>> {label or ' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        print(f"!!! 失败，退出码 {r.returncode}")
        sys.exit(1)
    print("    ok")


def build(spec):
    sh([sys.executable, "-m", "PyInstaller", spec, "--noconfirm", "--clean"], f"打包 {spec}")


def sync_user_data():
    """把当前账号数据同步到自己用的产物里。"""
    print("\n>>> 同步账号数据")
    targets = []
    if os.path.isdir(ONEDIR):
        targets.append(ONEDIR)
    if os.path.isfile(ONEFILE):
        targets.append(DIST)
    for t in targets:
        for rel in USER_FILES:
            src = os.path.join(ROOT, rel)
            if os.path.exists(src):
                dst = os.path.join(t, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
        for rel in USER_DIRS:
            src = os.path.join(ROOT, rel)
            if os.path.isdir(src):
                dst = os.path.join(t, rel)
                shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst)
        print(f"    {os.path.relpath(t, ROOT)}")


def make_clean():
    """生成不含任何用户数据的分发包。"""
    print("\n>>> 生成干净版")
    if not os.path.isdir(ONEDIR):
        print("!!! 需要先打包文件夹版（onedir）")
        sys.exit(1)

    shutil.rmtree(CLEAN, ignore_errors=True)
    os.makedirs(CLEAN, exist_ok=True)
    shutil.copytree(os.path.join(ONEDIR, "_internal"), os.path.join(CLEAN, "_internal"))
    shutil.copy2(os.path.join(ONEDIR, "ChujianMonitor.exe"), CLEAN)
    with open(os.path.join(CLEAN, "config.json"), "w", encoding="utf-8") as f:
        json.dump(CLEAN_CONFIG, f, ensure_ascii=False, indent=2)
    with open(os.path.join(CLEAN, "使用说明.txt"), "w", encoding="utf-8") as f:
        f.write(README)

    # 自检：不允许残留任何用户痕迹
    # 只查顶层与 data/ —— _internal 是依赖目录，里面 certifi/cacert.pem 是标准 CA 包
    bad = []
    for fn in os.listdir(CLEAN):
        full = os.path.join(CLEAN, fn)
        if os.path.isfile(full) and fn.endswith((".pem", ".cer", ".key", ".db")):
            bad.append(fn)
    if os.path.isdir(os.path.join(CLEAN, "data")):
        bad.append("data/")
    if bad:
        print("!!! 干净版里发现残留，已中止：")
        for b in bad:
            print("   ", b)
        sys.exit(1)
    print("    自检通过：无 data / db / key / pem / cer")

    if os.path.exists(CLEAN_ZIP):
        os.remove(CLEAN_ZIP)
    with zipfile.ZipFile(CLEAN_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dirpath, _, files in os.walk(CLEAN):
            for fn in files:
                full = os.path.join(dirpath, fn)
                z.write(full, os.path.relpath(full, DIST))
    mb = os.path.getsize(CLEAN_ZIP) / 1024 / 1024
    print(f"    zip: {mb:.1f} MB")


def report():
    print("\n" + "=" * 52)
    print(" 产物")
    print("=" * 52)
    for p in (ONEFILE, os.path.join(ONEDIR, "ChujianMonitor.exe"), CLEAN_ZIP):
        if os.path.exists(p):
            if p.endswith(".zip"):
                size = os.path.getsize(p) / 1024 / 1024
            else:
                size = os.path.getsize(p) / 1024 / 1024
            print(f"  {os.path.relpath(p, ROOT):50} {size:6.1f} MB")
    print("\n 提醒：exe 必须和同级的 data\\ 、config.json 放在一起")


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "both").lower()
    if cmd == "clean":
        make_clean()
        report()
        return

    if cmd in ("both", "onedir"):
        build("chujian.spec")
    if cmd in ("both", "onefile"):
        build("chujian-onefile.spec")
    sync_user_data()
    make_clean()
    report()


if __name__ == "__main__":
    main()
