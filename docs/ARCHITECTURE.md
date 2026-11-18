# 架构说明

## 一句话定位

本机运行的单进程桌面程序：内嵌一个 MITM 代理（抓凭证 / 录模板 / 收商品）+ 一个本地 HTTP 服务（界面与 API）+ 一个分层轮询器（盯价格并推送）。全部数据落本地 SQLite，不经过任何第三方服务器。

## 进程模型

```
ChujianMonitor.exe
├── 主线程        webview 窗口（WebView2 渲染 app/web.py 里的单页）
├── 线程 A        uvicorn :8787   界面 + REST API + 调度的宿主
├── 线程 B        mitmproxy :8080 内嵌代理（proxy_host.py）
└── 线程 C        Monitor 轮询循环（scheduler.py）
```

三者共享同一个 Python 进程，因此 `db` / `config` 是共享内存状态，改完配置无需重启子进程。
**代价**：SQLite 写入必须串行（`db._lock`），且代理线程里不能做重 CPU 操作。

## 目录职责

| 路径 | 职责 | 行数 |
|---|---|---|
| `desktop.py` | 唯一入口：日志重定向 → 建窗口 → 起三个线程 → 退出时还原系统代理 | 130 |
| `app/paths.py` | 统一根目录。开发时=项目目录，打包后=`sys.executable` 所在目录 | 15 |
| `app/config.py` | `config.json` 读写（轮询节奏、推送、规则、危险开关） | 72 |
| `app/crypto.py` | Fernet 加解密，密钥在 `data/secret.key` | 57 |
| `app/db.py` | SQLite 全部读写，14 张表，`_lock` 串行 | 892 |
| `app/models.py` | `Item` 数据类（sku/name/price/origin_price/qty/tags/image/raw/platform） | 27 |
| `app/adapters/base.py` | `Adapter` 抽象 + 三种异常（`AuthExpired` / `NotConfigured` / `AdapterError`） | 17 |
| `app/adapters/pupu.py` | **核心**。朴朴全部读写接口、请求头合成、模板重放、下单/取消 | 559 |
| `app/adapters/parser.py` | 购物车专用解析 `parse_pupu` + 通用收割 `harvest_products` + 样本落盘 | 250 |
| `app/adapters/mock.py` | 演示用的假平台，用于无凭证跑通链路 | 40 |
| `app/rules.py` | 价格比对 → 是否告警 → 写历史 + 推通知（含 10 分钟同类冷却） | 60 |
| `app/scheduler.py` | 两条独立节奏：购物车轮询（高峰 8s / 平峰 60s，±30% 抖动）+ 出清池全量任务（默认 1800s，独立线程） | 150 |
| `app/notify.py` | 企业微信群机器人 markdown 推送（降价 / 凭证失效 / 新出清） | 67 |
| `app/classify.py` | 本地三分类：肉类 / 海鲜 / 其它。先剔专名再判词 | 61 |
| `app/sysproxy.py` | Windows 系统代理开关（HKCU 注册表，带原值备份还原）+ 证书装/卸（指纹精确匹配） | 149 |
| `app/proxy_host.py` | 把 mitmproxy 跑在子线程里，提供 start/stop/ca_file | 82 |
| `app/web.py` | FastAPI 应用 + 内嵌单页（HTML/CSS/JS 字符串），28 个端点 | 1047 |
| `tools/grab_token.py` | mitmproxy addon：提凭证、存请求头档案、录制读写模板、收割商品入池 | 251 |
| `tools/capture.py` | 独立的抓包启动器（不依赖 exe，调试用） | 103 |
| `tools/proxy.py` | 命令行代理开关 | 80 |
| `tools/_pyfind.py` | 在多 Python 环境里找出真正装了 mitmproxy 的那个 | 68 |

## 三条主要数据流

### ① 抓包 → 建立可用状态

```
微信(PC) → 系统代理 127.0.0.1:8080 → mitmproxy
   └─ grab_token.request()
        ├─ 命中 pupuapi.com + 有 authorization 头
        │    → credential 表（token 加密存）
        │    → header_profile 表（整套请求头，含门店/设备/sign）
        ├─ 路径匹配 → replay 表（购物车 / 删除 / 下单 三类模板）
        └─ response() → harvest_products() → scan_item 表（出清池）
```

设计要点：**模板存结构，不存身份**。发送时 `Authorization` 与全部请求头由 `header_profile` 覆盖，所以 token 过期只需重抓一次，模板不用重录。

### ② 轮询 → 判断 → 推送

```
scheduler._loop() 每 1s 检查 next_at
  └─ _tick(cred) → PupuAdapter.fetch_cart()
       └─ 有 replay 就走 _fetch_by_replay（原样重放 + 换头）
       └─ 无 replay 走 _fetch_by_config（按 config 拼 URL）
            → parse_pupu() → Item[]
                 ├─ db.replace_cart()    记录"此刻车里有什么"
                 ├─ db.mark_in_cart()    反标池子里哪些已在车上
                 │   （注意：购物车商品**不再写入**出清池，否则活动结束后清不掉）
                 └─ rules.process()
                      ├─ 与 price_history 上一次比价
                      ├─ 星标商品：降 1 分即告警
                      ├─ 普通商品：≥5% 且 ≥0.5 元
                      └─ notify.price_drop() → 企业微信
```

### ③ 出清池全量抓取

```
POST /api/scan/refresh   （或 scheduler 的 30 分钟自动任务）
  └─ fetch_crazy_discount()  循环 GET hub/v2?page=N，最多 20 页
       ├─ 每页 sleep 350ms（15 页约 6s，避免被当扫描）
       ├─ 靠 is_end_page 终止，不靠 count
       ├─ harvest_products() 之后过滤：有原价且现价 ≥95% 原价的剔除（活动已结束）
       └─ db.replace_scan()：写入本次抓到的，删除 last_seen 早于本次开始时间的
            → 已下架、已恢复原价的商品不会留在池子里
```

## 几个非显然的设计决策

**为什么用录制重放而不是拼请求**
朴朴购物车接口除 token 外还校验 `pp-storeid`、`pp-placeid`、`sign-v2`、`seal-v2`、`timestamp` 等一批头，`sign-v2` 疑似与请求体相关。逆向签名成本高且会随版本失效，录制重放让服务端自己算签名。代价是模板会随接口改版失效——所以有 `/api/replay` 状态可见。

**为什么下单要「重建结算 → 提交」两步**
`unifiedorder` 的 body 里带 `order_items` 明细和金额，且服务端做幂等（同商品同金额返回旧单号）。所以提交前必须：只勾选目标商品 → 调 `settlement/detail/v3` 让服务端算活动/运费/券 → 用返回的 `place_order_param` 覆盖 body。结算失败时回退手工拼单（金额自己算，含 35 元免运门槛）。

**为什么删除是「数量置 0」**
朴朴没有独立删除接口。加购、改数量、删除共用 `purchasing_product/v3`，`selected_count=0` 即移除。前两次排查都因为"路径里没有 delete"而漏掉。

**为什么购物车和出清池用两条独立节奏**
两者成本和时效完全不同：购物车一次请求（几十 KB），价格随时会变，值得高频；出清池要翻 15 页（约 6 秒、15 个请求），商品一天只换几次。早期共用一个节奏的结果是——购物车被拖到 180 秒才刷一次，出清池又跟着买菜的节奏空转。现在拆成两个任务：购物车 8s/60s，出清池 30 分钟，互不阻塞（出清池跑在独立线程）。

**为什么池子用「全量替换」而不是累加**
`upsert` 只增不删，昨天的商品会永远留着——用户看到的是已经恢复原价的东西。现在每次抓取记录开始时间 `t0`，写完本次的之后删掉 `last_seen < t0` 的，即「这次没抓到就是下架了」。同时过滤掉现价 ≥95% 原价的（活动结束）。清理只在**完整抓取成功**后执行，中途失败不动，避免误删一半。

**为什么证书要比对指纹**
早期实现用 `certutil -verifystore Root mitmproxy` 判断，只要机器上装过任意一份同名证书就返回 true。换版本后新 CA 尚未安装，向导却显示"已信任"，流量解不开。现改为算本机 CA 的 SHA1 再精确比对。

**为什么分三个大类而不是二十个**
朴朴商品数据里没有分类字段（已实证：`category_id` 不存在，分类只在「分类 tab」那套接口里，与本接口不同源）。本地关键词分类必然有误判，类目越细误判越显眼。三大类 + "先剔专名再判词"的算法把误判压到可接受范围（295 件实测，抽查合理）。

## 扩展新平台（如小象超市）需要动的地方

1. `app/adapters/xiaoxiang.py` 实现 `Adapter` 三个方法：`fetch_cart()` / `headers()` / 异常映射
2. `tools/grab_token.py` 增加该平台的域名匹配与模板录制规则
3. `app/web.py` 的 `_best_cred()` / `_map_skus()` 支持按平台路由
4. `app/classify.py` 不变（与平台无关）

小象没有可用的 H5 入口，接口需 `mtgsig1.2` 动态签名，方向上只能走安卓自动化（让 App 自己签名），复杂度显著高于朴朴。
