# 朴朴接口逆向笔记

> 全部结论来自真实抓包与实测，非文档推测。接口可能随版本变动，失效时优先看本文档的"排查顺序"。
> **本文档不含任何真实 token / 地址 / 订单号。**

## 基础信息

| 项 | 值 |
|---|---|
| API host | `https://j1.pupuapi.com` |
| 鉴权 | 请求头 `Authorization`（抓包拿到的原值，含前缀，非 Bearer 标准格式） |
| 用户标识 | 请求头 `pp-userid` |
| 价格单位 | **分**（`current_price: 2793` = ¥27.93） |
| 响应结构 | `{"errcode": 0, "errmsg": "", "data": ...}`，`errcode` 非 0 表示业务错误 |

### 必需请求头（缺任一会返回 `errcode 400000 请求头参数缺失`）

```
Authorization       抓包原值
pp-userid           用户 id
pp-storeid          门店 id
pp-placeid          地点/地址 id
pp-placezip         地址邮编
pp_store_city_zip   城市邮编
sign-v2             签名（疑似与请求体+时间相关，未逆向）
seal-v2             校验串
timestamp           毫秒时间戳
Content-Type        application/json
```

**结论**：除 `Authorization` / `pp-userid` 外，其余头无法稳定构造，因此所有请求走**录制重放**（`replay` 表存整套头 + body，发送时换掉 token 和地址相关头）。

### 门店四头与地址的映射（换地址 = 换这 4 个头）

| 请求头 | 来源字段（`receivers/v3` 返回） |
|---|---|
| `pp-storeid` | `place.service_store_id` 或 `service_store_id` |
| `pp-placeid` | `place.id` |
| `pp-placezip` | `place.zip` |
| `pp_store_city_zip` | `place.store_city_zip` |

---

## 读接口

### 购物车

```
POST /client/shopping_cart/shopping_cart/detail/v3
Body: 抓包时的原值（通常为 {} 或含 supplement_id）
```

响应结构（真实路径，非通用商品数组）：

```
data.business_module.product_group_module.product_group_list[]
    └── items[]
          ├── base_model    ← 商品实体，价格/数量/图片都在这里
          └── show_model    ← 展示组件（不要用于取数）
```

`base_model` 关键字段：

| 字段 | 含义 | 备注 |
|---|---|---|
| `id` | 购物车条目 id（cart_item_id） | 勾选 / 删除 / 下单都用它 |
| `product_id` | 商品 id | **加购只需要这个** |
| `store_product_id` | 门店商品 id | 也是出清池的 sku |
| `spu_id` | 聚合 id | 非分类 |
| `name` | 名称 | |
| `current_price` | 现价（分） | |
| `market_price` | 原价 / 吊牌价（分） | **这才是原价** |
| `original_price` | ⚠️ 不可靠 | 实测出现过原价低于现价 |
| `selected_count` | 数量 | **0 即不在车内** |
| `is_selected` | 是否勾选 | |
| `main_image` | 主图 URL | 域名 `product-files.pupumall.com` |
| `activity_id` | 活动 id | 仅活动商品有 |
| `batch_id` | 批次 id | 仅活动商品有 |
| `time_create` | 加车时间 | 下单明细需要 |
| `type` | 商品类型 | |
| `product_tag` | 标签枚举 | |

`discount_rate` 可用于交叉验证：≈ `current_price / market_price`。

### 疯狂折扣 · 全量列表（分页）

```
GET /client/marketing/channel/crazy_discount/hub/v2?page=N
```

- 参数只认 **`page`**（`page_index` / `page_num` / `offset` / `cursor` 全部无效，会返回第 1 页）
- 每页 20 条，`is_end_page` 为终止条件，`count` 为总数（实测 242）
- 当前地址下约 13 页

响应：`data.product_list[]`，每项含 `store_product_model`（`id`/`product_id`/`spu_id`/`price`/`market_price`/`stock_quantity`/`main_image`）+ `show_model` + `add_cart_model`。

**没有分类字段**。已递归扫描所有含 `categ/class/group/tag` 的键，仅有 `spread_tag`（数字曝光位）、`spu_id`、`is_catering_product`（布尔）。

### 疯狂折扣 · 首页预览（不用于抓全量）

```
GET /client/marketing/channel/flashsale/products/index/v2
```

只返回 2 个场次各 12 件，带 `more_button_text: "更多"`，指向上面的 `hub/v2`。

### 收货地址

```
GET /client/account/receivers/v3
```

返回数组，每项含 `id` / `name` / `address` / `building_room_num` / `is_default` / `is_in_service` / `place{id, zip, store_city_zip, service_store_id}` / `place_building` / `order_count_all`。

- 不接受分页参数，一次返回全部
- 实测会包含「点过但下过 0 单」的地址（App 里看不到），可用 `order_count_all` 过滤

### 订单列表

```
GET /client/order/orders_list/v4?page=1&size=20&type=N
```

`type` 实测：`10` = 待支付，`0/20/30` 其它状态分组。返回 `data[]` 含 `id` / `order_num` / `client_status_name` / `total_amount`。

---

## 写接口

### 加购 / 改数量 / 删除（三合一）

```
POST /client/shopping_cart/shopping_cart_item/purchasing_product/v3
Body:
{
  "add_item_ro": {
    "item_id":        "<cart_item_id>",       // 改数量/删除时需要，纯加购可省
    "product_id":     "<product_id>",         // 必填
    "current_price":  2793,                   // 分
    "product_tag":    0,
    "selected_count": 1,                      // 0 = 删除
    "is_selected":    true,
    "type":           0,
    "activity_id":    "..."                   // 仅活动商品
  },
  "return_data_type": 0,
  "purchase_pattern": 10
}
```

- **删除 = `selected_count: 0`**，没有独立的 delete/remove 接口
- 纯加购（商品不在车里）只需 `product_id` + `current_price`，不需要 `item_id`
- `activity_id` / `batch_id` 只在活动商品上出现，需按存在与否动态补

### 勾选

```
PUT /client/shopping_cart/shopping_cart_item/selection_status/v3
Body:
{
  "selection_status_ros": [
    {"is_selected": true, "item_id": "<cart_item_id>"}
  ],
  "purchase_pattern": 10
}
```

锁单前必须先**取消勾选其它所有商品**，否则它们会一起进订单。

### 结算（下单前置，必须有）

```
POST /client/order/settlement/detail/v3
Body: 录制模板，其中 cart_info.cart_item_list 替换为当前勾选的 base_model 列表
```

返回 `data.place_order_param`（含 `order_items` / `order_trace_id` / `add_to_cart_param`）+ `data.fee`（`total_amount` / `logistics_fee` / `packing_fee`）+ `is_end_page` 等。

**注意**：手工重建 `cart_info` 容易触发「数据不完整」（缺 activity_list 等上下文）。因此实现为「尽力而为」——结算失败则回退手工拼单。

### 提交订单

```
POST /client/order/unifiedorder/v4
```

**成功判定特殊**：成功时 `errcode = 17115002`（非 0），`errmsg = "订单已创建成功，请至订单列表查看"`。把非 0 一律当失败会把成功误判。

必改字段：

| 字段 | 处理 |
|---|---|
| `order_items` | 用结算返回的，或按勾选商品重建 |
| `settlement_total_amount` / `external_payment_amount` | 用结算返回的 `fee.total_amount` |
| `logistics_fee` | 满 35 元免，否则 300 分（金额必须自洽，否则服务端拒绝） |
| `discount_entity_ids` | **必须清空**。录制时的券绑定旧单金额，会报「商品金额不满足条件金额」 |
| `time_delivery_promise(_start/_end)` | 录制的已过期，改为 `now + 45min` |
| `order_trace_id` | 新 uuid，否则可能命中幂等返回旧单 |
| `store_id` / `place_id` / `receiver*` | 按当前选中地址重写 |
| `coin_payment_amount` | 0 |

### 取消订单

```
POST /client/order/orders_order/cancel/{order_id}   // 路径含订单 uuid
```

用正则替换模板 URL 里的 uuid。**注意实现坑**：算出新 URL 后必须传新 url 给发送函数，早期版本把旧 rep 传过去，导致反复取消同一个早已取消的旧单，新单一直挂着占库存 → 表现为「库存不足」。

---

## 排查顺序（接口失效时）

1. 看 `/api/state` 的 `replay` 字段：`cart` / `write` / `order` 哪个为 false
2. 抓包看真实请求路径是否变了 → 改 `tools/grab_token.py` 的录制规则
3. 业务码变了 → 看 `_send()` 里的成功判定白名单（当前含 `17115002`）
4. 报「请求头参数缺失」→ `header_profile` 表里的头过期，重抓一次
5. 业务码 `200099` → **签名过期**。`sign-v2` 和时间戳绑定，录制模板放久了必然失效（实测约一天）。界面「购物车已录制」的时间戳就是判断依据——时间旧了就重录，不用改代码
6. 报「数据不完整」→ 结算重建的上下文缺失，检查 `cart_info.activity_list` 过滤逻辑
7. 报「金额不满足条件」→ 检查是否残留 `discount_entity_ids`，或活动门槛未达

## 已确认无效的路径（别再试）

```
/client/category/tree/v2, /client/category/index/v2
/client/product/detail/v2, /client/product/detail/v3
/client/store_product/detail/v2
/client/search/product/index/v2, /client/search/products/v2
/client/category/products/v2, /client/category/product_list/v2
/client/marketing/channel/crazy_discount/category/v2
```

---

## 小象超市（未接入）

| 项 | 情况 |
|---|---|
| 网页版 | `maicai.meituan.com` 仅为宣传页，无商品/购物车/登录 |
| 接口 | `mall.meituan.com`，必须带 `mtgsig1.2` 动态签名（随请求体 + 时间 + 设备指纹变化） |
| 可行性 | 抓到的签名不能复用，只能让 App 自己签名 → 需安卓自动化（Appium + 真机/云手机） |
| 建议 | 先用小号试；走安卓自动化后 token 变量从项目中消失（登录态由 App 维护） |
