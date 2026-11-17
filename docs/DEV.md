# 开发与打包

## 环境

| 项 | 要求 |
|---|---|
| Python | 3.13（实测 3.13.12 可用；3.12 也跑得通） |
| 系统 | Windows 10/11（依赖注册表、certutil、WebView2） |
| WebView2 | 系统自带（Edge 附带），缺失则窗口无法创建 |

### ⚠️ 这台机器上有多个 Python

```
C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe   ← 依赖装在这里
C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe       ← 双击 bat 时默认用这个
```

`tools/_pyfind.py` 会枚举所有候选解释器并逐个试 `import mitmproxy`，自动挑对的那个。
手动验证：

```bash
python tools/capture.py --check
```

它会打印每个 Python 打勾或打叉，一眼看出该给谁装包。

## 依赖安装

```bash
python -m pip install -r requirements.txt
python -m pip install pyinstaller pywebview pythonnet pillow   # 打包与资源
```

## 开发运行

```bash
cd chujian
python desktop.py            # 完整桌面版（窗口 + 代理 + 调度）
```

调试期常用的降级入口：

```bash
python run.py                # 只起 8787 控制台，不起窗口、不起代理
python tools/capture.py      # 只起 8080 抓包代理（独立进程）
python tools/proxy.py on|off|status   # 命令行切系统代理
```

日志：开发时打终端，打包后写入 `data/app.log`（`desktop.py` 顶部做了重定向）。

## 打包

**推荐用一键脚本**（打包 + 同步数据 + 生成干净版，一步到位）：

```bash
python tools/release.py both      # 两个版本都打
python tools/release.py onedir    # 只打文件夹版
python tools/release.py onefile   # 只打单文件版
python tools/release.py clean     # 只重新生成干净版（不打包）
```

它会自动做：打包 → 拷 `data/`+`config.json` 到产物 → 生成不含用户数据的分发包 → 自检（发现 `data/`、`*.db`、`*.key`、`*.pem` 就中止）→ 打印体积。

**手动打包**（需要控制细节时）：

```bash
# 文件夹版（日常用，启动快，杀软误报少）
python -m PyInstaller chujian.spec --noconfirm --clean

# 单文件版（分发用，35MB，启动慢 2~3 秒）
python -m PyInstaller chujian-onefile.spec --noconfirm --clean
```

产物：

```
dist/ChujianMonitor.exe          单文件版
dist/ChujianMonitor/             文件夹版（80MB，含 _internal）
```

### 打包后必须做的两件事

**① 同步数据**（否则 exe 是空白状态，要重新抓包）

```bash
mkdir -p dist/data dist/ChujianMonitor/data
cp data/chujian.db data/secret.key dist/data/
cp data/chujian.db data/secret.key dist/ChujianMonitor/data/
cp -r data/mitm dist/data/
cp config.json dist/ && cp config.json dist/ChujianMonitor/
```

**② 冷启动验证**（在临时目录跑，确认不依赖开发环境）

```bash
cd dist && rm -rf _smoke && mkdir _smoke && cp -r "出清监控-干净版/." _smoke/ && cd _smoke && ./ChujianMonitor.exe
# 另开终端
curl -s http://127.0.0.1:8787/api/setup
```

预期：`has_token: false`、`cert: false`（说明新 CA 未被信任，向导会从第 1 步开始）。

### 数据目录规则

`app/paths.py` 的 `root()`：

- 开发时 = 项目根目录（`app/` 的父目录）
- `sys.frozen` 为真时 = `os.path.dirname(sys.executable)`

所以 exe 旁边的 `data/` 和 `config.json` 必须与 exe 同级，**不能放进 `_internal`**。

## 发版检查清单

```
[ ] python -c "import ast; [ast.parse(open(f,encoding='utf-8').read()) for f in [...]]"  语法
[ ] 前端 JS：抽出 <script> 后 node --check
[ ] 关键链路：购物车拉取 / 出清池抓取 / 加购 / 删除 / 锁单 + 撤销
[ ] 系统代理开关往返（on → 确认注册表 → off → 确认还原原值）
[ ] 退出后：8080/8787 释放、系统代理已还原、无残留进程
[ ] 干净版不含 data/ secret.key *.pem
[ ] CHANGELOG 已更新
```

前端语法校验一条命令搞定：

```bash
python -c "
from app import web
js = web.HTML.split('<script>')[1].split('</script>')[0]
open('data/_check.js','w',encoding='utf-8').write(js)
" && node --check data/_check.js && echo JS_OK
```

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `[x] 没找到 mitmdump` | 用了没装依赖的 Python | `python tools/capture.py --check` 看是哪个 |
| 端口 8787 被占 | 旧实例没退 | `Get-NetTCPConnection -LocalPort 8787` 找 PID 后 kill |
| 向导显示证书已安装但抓不到包 | 旧版 CA 同名干扰 | 已修为指纹比对；仍异常则 `remove_cert()` 后重装 |
| 打包报 exit=1 且无明确错误 | `dist/ChujianMonitor` 目录被占用 | 关掉运行的 exe，删掉该目录再打 |
| exe 启动后无窗口 | WebView2 缺失 / 日志有异常 | 看 `data/app.log`；确认 `msedgewebview2.exe` 存在 |
| 界面按钮点了没反应 | 原生 `confirm()` 在 iframe 里被吞 | 一律用页内 `uiConfirm()`，不要用原生弹窗 |
| 换地址后加购报错 | 出清池还是旧门店商品 | 已实现切地址自动清空 + 后台重抓 |

## 同步到 GitHub

仓库：<https://github.com/qianyu2133/pupu-monitor>

```bash
python tools/sync.py "fix: 修正轮询间隔"
```

自动完成：暂存 → 敏感扫描 → 提交 → 推送。可选参数：

```bash
python tools/sync.py --dry-run     # 只看会提交什么
python tools/sync.py --no-push     # 只提交不推送
```

### 提交守卫

首次 clone 后必须启用（hook 不会随仓库自动生效）：

```bash
git config core.hooksPath .githooks
```

`pre-commit` 会拦两类东西：

| 类型 | 内容 |
|---|---|
| 路径 | `data/` `dist/` `build/` `*.db` `*.key` `*.pem` `*.cer` `config.json` |
| 内容 | 手机号、JWT/Token、企业微信 webhook key |

拦下来是有意的——这些东西一旦推到 GitHub 就删不干净（会被 fork、被缓存）。确认无风险后可用 `git commit --no-verify` 强制提交，但这基本不该发生。

### 节奏

**每次改动自测通过后立即同步，不要攒。** 攒着的结果通常是：某次改动把本地 `data/` 混进来，然后整批都不敢推了。

## 维护节奏

| 触发 | 动作 |
|---|---|
| token 过期（几周） | 重走向导，约 30 秒。模板不用重录 |
| 换电脑 / 换设备 | 同上 |
| 换收货地址 | 界面切地址即可，**不用重录**（下单时会按当前地址重写门店/收货人） |
| 朴朴改接口 | 抓包看真实路径 → 改 `grab_token.py` 录制规则 + 适配器 → 只重录受影响的那条 |
| 微信 PC 版改网络栈 | 抓包可能失效。先跑 `capture.py --check`，再确认流量是否进代理（看 `/api/state` 的 `hits`） |
