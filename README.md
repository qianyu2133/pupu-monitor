# 出清监控

盯朴朴超市购物车与「疯狂折扣」页的价格，降价推微信，看到便宜货一键加购。

**本机运行，数据不上传。** 唯一的外部请求是朴朴接口和企业微信 webhook。

---

## 它做什么

| 能力 | 说明 |
|---|---|
| 购物车监控 | 分层轮询（21–23 点 12 秒 / 平时 180 秒，带 ±30% 抖动），降价推送 |
| 星标重点商品 | 购物车里点 ☆，**降 1 分钱就推**，普通商品走 ≥5% 且 ≥0.5 元 |
| 出清池 | 一键抓「疯狂折扣」全量（分页到底，实测 242 件），按折扣排序，可筛肉类 / 海鲜 / 其它 |
| 一键加购 | 出清池每行「加购」，直接进购物车 |
| 删除商品 | 单件或批量 |
| 一键锁单 | 提交订单占库存、停在待支付；带「撤销锁单」退单 + 商品回车 |
| 地址切换 | 切换收货地址即切门店，购物车和出清池自动跟着换 |
| 傻瓜引导 | 三步向导：装证书 → 开代理 → 点一下小程序购物车 |

## 快速开始

**用打包好的版本**：双击 `dist\ChujianMonitor\ChujianMonitor.exe`（或单文件版 `dist\ChujianMonitor.exe`），首次打开按向导走三步。

**从源码跑**：

```bash
cd chujian
python -m pip install -r requirements.txt
python -m pip install pywebview pythonnet
python desktop.py
```

前置条件：Windows 10/11 + **Windows 版微信**（用来开朴朴小程序）。不需要手机。

## 三步接入是怎么做到的

小程序跑在 PC 微信里，流量走 Windows 系统代理。所以：

```
微信(小程序) → 系统代理 127.0.0.1:8080 → 本程序内嵌的 mitmproxy
                                              ├─ 截出 token 和整套请求头
                                              ├─ 录下购物车/删除/下单的请求模板
                                              └─ 收割商品进池子
```

向导只是把「装根证书、改注册表代理、检测流量」这三件事自动化。**全程不需要管理员权限**（证书装进当前用户存储）。

## 目录

```
chujian/
├─ desktop.py              入口：窗口 + 代理 + 调度
├─ app/                    业务代码（详见 docs/ARCHITECTURE.md）
│   ├─ adapters/pupu.py    朴朴接口适配器（核心，559 行）
│   ├─ web.py              FastAPI + 内嵌单页（27 个端点）
│   ├─ scheduler.py        分层轮询
│   ├─ sysproxy.py         系统代理与证书
│   └─ classify.py         三分类
├─ tools/grab_token.py     抓包 addon
├─ docs/                   文档
├─ config.json             配置
├─ data/                   账号与数据库（加密，已被 git 忽略）
└─ dist/                   打包产物
```

## 文档

| 文档 | 内容 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 进程模型、模块职责、三条数据流、非显然的设计决策 |
| [docs/PUPU-API.md](docs/PUPU-API.md) | 朴朴接口逆向笔记：全部路径、字段、坑、失效排查顺序 |
| [docs/DEV.md](docs/DEV.md) | 开发环境、打包、发版检查清单、常见问题 |
| [CHANGELOG.md](CHANGELOG.md) | 版本历史与已知待办 |

## 配置

`config.json` 关键项：

```json
{
  "poll":   { "peak_interval": 12, "idle_interval": 180, "jitter": 0.3 },
  "rule":   { "min_drop_pct": 5.0, "min_drop_abs": 0.5 },
  "notify": { "wecom_webhook": "" },
  "danger": { "allow_submit_order": false }
}
```

- **webhook**：企业微信建一个只有自己的群 → 群机器人 → 复制 webhook 粘到界面
- **allow_submit_order**：锁单总开关，默认关。开了才会真的提交订单

## 必须知道的边界

**① 账号风险真实存在。** 自动化读写平台接口违反用户协议，代价是账号被限权（功能受限、券失效）。建议非主账号使用。锁单是唯一「碰钱」的动作，默认关闭。

**② 只做自用。** 不要做成对外服务或收费产品——已有爬取平台数据被判赔 500 万的判例，性质会变。

**③ token 会过期。** 几周一次，重走向导即可（约 30 秒），模板不用重录。凭证失效时监控自动暂停并推送提醒，不会拿死 token 硬刷。

**④ 接口会改版。** 改了就要抓包定位新路径，改录制规则和适配器——见 `docs/PUPU-API.md` 的排查顺序。

**⑤ 未签名。** 首次在别的电脑运行会被 SmartScreen 拦，点「更多信息 → 仍要运行」。

**⑥ 小象超市未接入。** 需 `mtgsig1.2` 动态签名，只能走安卓自动化，复杂度翻倍。

## 版本

当前 v0.8（桌面封装 + 分发包）。详见 [CHANGELOG.md](CHANGELOG.md)。
