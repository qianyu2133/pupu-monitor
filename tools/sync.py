"""把当前改动提交并推送到 GitHub。

用法：
    python tools/sync.py                    自动生成提交信息
    python tools/sync.py "fix: 修正轮询间隔"   指定提交信息
    python tools/sync.py --dry-run          只检查，不提交不推送
    python tools/sync.py --no-push          只提交，不推送

流程：检测改动 → 暂存 → 敏感扫描 → 提交（pre-commit 会再拦一次）→ 推送
"""
import re
import subprocess
import sys

ROOT = __file__.rsplit("tools", 1)[0].rstrip("\\/")

# 禁止入库的路径
BAD_PATH = re.compile(
    r"(^|/)(data|dist|build)/|\.(db|key|pem|cer|p12|sqlite|sqlite3)$|(^|/)config\.json$"
)

# 禁止入库的内容
BAD_CONTENT = [
    ("手机号", re.compile(r"1[3-9]\d{9}")),
    ("JWT/Token", re.compile(r"eyJ[A-Za-z0-9_-]{20,}")),
    ("企业微信 webhook 密钥", re.compile(r"qyapi\.weixin\.qq\.com[^ \"']*key=")),
]


def git(*args, capture=True):
    r = subprocess.run(["git"] + list(args), cwd=ROOT,
                       capture_output=capture, text=True)
    return r


def main():
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    no_push = "--no-push" in argv
    msg = " ".join(a for a in argv if not a.startswith("--")).strip()

    # 1. 有无改动
    changed = git("status", "--porcelain").stdout.strip()
    if not changed:
        print("没有改动，已是最新。")
        return 0

    files = [line[3:] for line in changed.splitlines() if len(line) > 3]
    print(f"检测到 {len(files)} 个文件改动：")
    for f in files[:15]:
        print("   ", f)
    if len(files) > 15:
        print(f"    ... 另有 {len(files) - 15} 个")

    if dry:
        print("\n--dry-run：不提交。")
        return 0

    # 2. 暂存
    git("add", "-A")

    # 3. 路径检查
    staged = [p for p in git("diff", "--cached", "--name-only").stdout.splitlines() if p]
    bad = [p for p in staged if BAD_PATH.search(p)]
    if bad:
        print("\n拒绝同步：这些文件属于用户数据或密钥，不能进仓库")
        for b in bad:
            print("   ", b)
        print("\n已中止，暂存区保留原样。确认后可 git reset 取消暂存。")
        return 1

    # 4. 内容检查（只看新增行）
    diff = git("diff", "--cached", "-U0").stdout
    added = "\n".join(
        line for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    hits = []
    for name, pat in BAD_CONTENT:
        n = len(pat.findall(added))
        if n:
            hits.append((name, n))
    if hits:
        print("\n拒绝同步：暂存内容里有敏感信息（不显示具体内容）")
        for name, n in hits:
            print(f"   {name}: {n} 处")
        print("\n请先脱敏。这些内容推到 GitHub 后无法彻底删除（会被 fork / 缓存）。")
        return 1

    # 5. 提交
    if not msg:
        msg = f"chore: 同步更新（{len(files)} 个文件）"
    r = git("commit", "-m", msg)
    if r.returncode != 0:
        out = (r.stdout or "") + (r.stderr or "")
        print("\n提交失败：")
        print(out.strip()[:800])
        return 1
    print("\n已提交")

    # 6. 推送
    if no_push:
        print("--no-push：未推送。")
        return 0

    r = git("push", "origin", "HEAD")
    if r.returncode != 0:
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        print("\n推送失败：")
        print(out[:800])
        print("\n常见原因：网络不通、凭据过期。可先 git pull --rebase 再试。")
        return 1

    head = git("rev-parse", "--short", "HEAD").stdout.strip()
    remote = git("remote", "get-url", "origin").stdout.strip()
    print(f"已推送到 {remote}  ({head})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
