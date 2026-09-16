# 智选 · 无人零售导购 Agent 与多模态结账系统

面向无人零售场景的完整演示系统，包含**四个前端界面**与一套 FastAPI 后端：

| 部分 | 界面 | 形态 | 一句话 |
| --- | --- | --- | --- |
| **A · 智能导购 Agent** | `/guide` | 手机竖屏 | 自然语言咨询商品位置、营养成分、过敏原、促销与搭配，支持扫条码直达 |
| **B · 自适应视觉结账** | `/checkout` | 结算台横屏 | 购物盘多目标识别、置信度仲裁、歧义追问、重量校验、账单与支付 |
| **C · 后台数据看板** | `/admin` | 桌面端 | KPI、热销榜、库存预警、销售趋势、对话抽样 |
| **D · 审计追溯** | `/admin` 第六区块 | 桌面端 | 结账抓拍与结算清单绑定存档，30 天自动清理，用于防损与客诉取证 |

全部业务数据统一存放在 **SQLite 数据库**（`backend/data/smartmart.db`），
导购、视觉识别、结账、订单、看板、审计六个环节共享同一份数据源。

> 商品数据为演示用示例数据，营养成分与配料以真实商品包装为准。

---

## 一、快速开始

### 1. 启动后端

```bash
cd backend
pip install -r requirements.txt
python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

接口文档：<http://127.0.0.1:8000/docs>

### 2. 启动前端

```bash
cd frontend
npm install
npm run dev
```

打开 <http://127.0.0.1:5173> ，首页可切换 A / B 两个界面，顶部标签也常驻。
前端已配置 Vite 代理，`/api` 请求自动转发到 8000 端口，无需处理跨域。

### 3. 进入后台看板

后台入口默认**不放顶栏 Tab**，通过 URL 直访：<http://127.0.0.1:5173/admin>

若后端 `.env` 配了 `ADMIN_TOKEN`，进入时会要求输入一次并存 localStorage；
未配置时开发模式直接放行。

### 4. 接入真实大模型（可选）

```bash
cd backend
cp .env.example .env
# 填入 LLM_API_KEY / VISION_API_KEY 后重启后端
```

**不填任何 Key 也能完整演示**：导购自动走本地知识库，视觉自动走内置示例场景。
顶栏会实时显示当前运行模式。

### 5. 手机访问（可选）

同一 Wi-Fi 下用手机打开电脑 IP 即可体验导购端：

```bat
:: 右键 -> 以管理员身份运行，放行 5173 / 8000 入站并打印可用 IP
enable-lan.bat

:: Wi-Fi 不通时的兜底：USB 数据线 + adb reverse 端口转发
run-usb-bridge.bat
```

详见 [`docs/局域网手机访问手册.md`](docs/局域网手机访问手册.md)。

---

## 二、功能模块总览

```
                        ┌─────────────────────────────────────┐
   顾客 ── 自然语言 ──▶ │  A · 智能导购 Agent  (手机竖屏)      │
                        │  意图 → 工具调用 → 流式作答 + 引用卡 │
                        └──────────────┬──────────────────────┘
                                       │ 工具调用（模型不碰库）
   顾客 ── 拍照/扫码 ──▶ ┌─────────────▼──────────────────────┐
                        │  B · 自适应视觉结账  (结算台横屏)    │
                        │  闭卷匹配 → 校准 → 三态仲裁 → 支付  │
                        └──────────────┬──────────────────────┘
                                       │ 订单落库 + 抓拍存档
   店员 ── 桌面浏览器 ─▶ ┌─────────────▼──────────────────────┐
                        │  C · 后台数据看板   /admin 1~5 区块 │
                        │  D · 审计追溯       /admin 第 6 区块│
                        └──────────────┬──────────────────────┘
                                       │
                        ┌──────────────▼──────────────────────┐
                        │  统一数据访问层 → SQLite             │
                        └─────────────────────────────────────┘
```

| 模块 | 核心难点 | 对应章节 |
| --- | --- | --- |
| A 智能导购 | 抑制幻觉、混合检索、受控 SQL、扫码直达 | [五](#五a-部分--智能导购-agent) |
| B 视觉结账 | 闭卷选择、置信度校准、三态仲裁、多帧投票、级联检测 | [六](#六b-部分--视觉结账) |
| C 后台看板 | 纯 SVG 图表零依赖、时间范围联动、库存双列预警 | [七](#七c-部分--后台数据看板) |
| D 审计追溯 | 不可变快照、sidecar 降级、路径穿越防护、自动清理 | [八](#八d-部分--审计追溯) |

---

## 三、目录结构

```
supermarket/
├── backend/
│   ├── app/
│   │   ├── api/            # 路由层：health / agent / catalog / checkout / admin
│   │   ├── cli/            # purge_audit.py 清理审计 / check_prompts.py 校验提示词
│   │   ├── core/config.py  # 配置与识别阈值
│   │   ├── data/           # 29 个 SKU 主数据 + 4 组示例购物盘场景
│   │   ├── db/             # 建表 / 种子 / ORM 模型
│   │   ├── models/         # Pydantic 数据契约
│   │   ├── prompts/        # 提示词（YAML），见「提示词管理」章节
│   │   │   ├── loader.py          # 加载 + 热重载 + 完整性自检
│   │   │   ├── guide.yaml         # 导购主提示词 + 7 个工具的文案
│   │   │   ├── checkout.yaml      # 视觉结账闭卷提示词（全图 / 裁剪两版）
│   │   │   ├── sql.yaml           # 兜底 SQL 工具的表结构说明
│   │   │   └── local_fallback.yaml# 无 LLM Key 时的本地话术与引用标签
│   │   ├── repositories/   # 统一数据访问层
│   │   └── services/
│   │       ├── agent.py        # 导购 Agent：意图 → 工具调用 → 流式作答
│   │       ├── llm.py          # 大模型适配（OpenAI 兼容协议）
│   │       ├── search.py       # 混合检索：字面匹配 + 属性语义
│   │       ├── text2sql.py     # 受控只读 SQL（五道防线）
│   │       ├── vision.py       # 视觉适配：真实模型 / 模拟引擎自动切换
│   │       ├── cascade.py      # 多帧投票聚合 + YOLO 级联
│   │       ├── arbitration.py  # 置信度仲裁与重量校验
│   │       ├── pricing.py      # 促销、组合价、满减计算与折扣分摊
│   │       ├── audit.py        # 审计追溯：抓拍存档 + 不可变快照 + 自动清理
│   │       └── store.py        # 会话存储（生产环境替换为 Redis）
│   ├── data/               # SQLite 库与审计抓拍图（运行态，不入库）
│   ├── eval/               # 导购效果评测集 + 三方案消融，见「十二、测试与评测」
│   │   ├── cases.json                 # 58 条标注用例
│   │   ├── catalog_snapshot.json      # 商品主数据快照（评测真值来源）
│   │   ├── run_eval.py                # 执行器：只跑不判，落盘 raw_*.jsonl
│   │   ├── score_eval.py              # 打分器：只读不跑，产出指标与明细
│   │   ├── retrieval_ablation.py      # 检索层七方案 × 双口径对照实验
│   │   └── results/                   # 指标、报告、逐题原始记录
│   ├── tests/              # 14 个测试脚本（12 个离线自跑 + 2 个需服务）
│   └── training/           # YOLO 微调工作区（数据集与 *.pt 权重不入库）
├── frontend/
│   └── src/
│       ├── api/            # 接口封装（含 SSE 流式读取）
│       ├── components/     # 商品示意图等公共组件
│       ├── types/          # 与后端对齐的 TS 类型
│       └── views/
│           ├── HomeView.vue     # 首页：A / B / 后台入口
│           ├── GuideView.vue    # A 部分 导购手机端（含扫码）
│           ├── CheckoutView.vue # B 部分 结算台横屏
│           └── AdminView.vue    # C + D 部分 后台看板六大区块
├── docs/
│   ├── 需求分析与系统架构.md
│   ├── 导购内容维护指南.md
│   └── 局域网手机访问手册.md
├── enable-lan.bat          # 一键放行防火墙，开放手机访问
└── run-usb-bridge.bat      # USB 数据线桥接兜底方案
```

商品与购物盘全部用 SVG / CSS 绘制，**不依赖任何外部图片资源**，断网也能完整演示。

---

## 四、数据层：统一数据源

所有业务数据存放在 SQLite（`backend/data/smartmart.db`），启动时自动建表，
首次运行自动从 `app/data/products.py` 灌入种子数据。

### 表结构

| 表 | 用途 | 演示库现有量 |
| --- | --- | --- |
| `products` | 商品主表：价格、营养、过敏原、标签、货架位、视觉特征、**条码 `barcode`**、**库存 `stock`** | 29 |
| `promotions` | 促销规则，`scope=store` 全场 / `scope=sku` 单品 | 7 |
| `similar_groups` | 相似商品分组，供视觉仲裁判定歧义 | 9 |
| `checkout_sessions` | 结账会话（含确认记录与账单快照） | 运行态 |
| `checkout_detections` | 每次识别产出的检测框与候选商品 | 运行态 |
| `orders` / `order_items` | 已支付订单与订单明细 | 运行态 |
| `chat_logs` | 导购对话日志，供后台「最近对话抽样」 | 运行态 |
| `audit_records` | 审计追溯：抓拍路径 + 不可变清单快照 + 时间戳 | 30 天滚动 |

会话与订单落库后，**后端重启不再丢账单**。

### 库存：支付扣减 + 导购可查

`products.stock` 列记录实时库存（种子默认 100）。两条链路都会触达它：

- **支付扣减（幂等）**：`POST /pay` 在扣款前先 `check_stock` 校验，不足返回 409 拒绝支付；支付成功后 `_persist_order` 在落订单的同一事务里 `deduct_stock`，**以 `session_id` 作主键去重**，重复支付/回调不会重复扣。库存扣到负数会抛错让整个事务回滚，订单也一并取消。
- **导购查询**：导购 Agent 新增 `check_stock` 工具（LLM 路径）+ 本地 `stock` 意图（无 Key 时降级）。顾客问「可乐还有货吗」「还剩多少」时，工具按 `sku_id` 精确取货，回答三态：**充足**（>5 件）/ **紧张**（1~5 件，提醒尽快来）/ **售罄**（0 件，推荐同类在售商品）。搜索结果与商品详情也会带上库存数量。

### 加一个商品要动哪些地方

1. 在 `app/data/products.py` 的 `PRODUCTS` 里照抄一段 `Product(...)`，填全价格、营养、配料、过敏原、货架位与视觉特征
2. 若与已有商品包装相近，把 `sku_id` 一起加进 `SIMILAR_GROUPS`，仲裁阶段才会把它当作可混淆候选
3. 重建种子：`python -m app.db.seed --force`（只清空商品相关三张表，订单不受影响）

之后**不需要改任何识别代码**：闭卷选择的候选清单由 `product_repo.list_products()` 动态生成，
新 SKU 会自动出现在给视觉模型的提示词里；前端商品图按 `visual.shape` 绘制、场景按钮按接口返回渲染，同样无需改动。

> 改动 `scenes.py` 这类内存数据后，Windows 下 uvicorn 热重载常常不触发，**需要手动重启后端**才生效。
> 重要改动前建议先备份 `backend/data/smartmart.db`。

### 大模型如何访问数据

**模型不直接连数据库。** 它只调用工具，工具内部再查库——这是刻意设的安全边界：

```
导购大模型 → 工具调用 → 统一数据访问层 → SQL → 数据库
                  ↑
            模型止步于此
```

6 个预设工具（`search_product` / `get_location` / `get_nutrition` / `get_promotions` /
`recommend_pairing` / `check_stock`）之外，另有一个兜底工具 `query_product_database`
可执行只读 SQL，受五道防线约束：

1. 只放行 SELECT，写操作关键字一律拒绝
2. 表白名单，碰不到白名单以外的表
3. 禁止多语句（检测到分号即拒绝）
4. 强制 LIMIT ≤ 50，缺 LIMIT 时自动补
5. 结果行数上限 + 单元格截断

### 混合检索

纯关键词匹配对「红色罐装饮料」这类描述无解——它跟「可口可乐 汽水」没有一字重叠。

所以检索分两层：

1. **字面匹配**：完整命中 > 子串包含 > 二元组重叠
2. **属性语义**：利用商品库已有的结构化属性——颜色（色相距离）、包装形状、品类、口味、健康标签、价格区间

提问会先剥掉疑问词（「在哪里」「多少钱」），否则整句会稀释匹配分。实测：

| 提问 | 命中 |
| --- | --- |
| 红色罐装饮料 | 可口可乐 汽水 |
| 无糖的饮料 | 元气森林 白桃味苏打气泡水 |
| 不含花生的零食 | 乐事 黄瓜味薯片（士力架因含花生被排除） |
| 提神的饮料 | 三顿半 精品速溶咖啡 |

这套方案的价值在于**零依赖**：不需要下载 embedding 模型、不需要 API Key，
断网也能跑，且每个判断都可解释——比向量检索更适合演示与答辩。

### 改数据

见 [`docs/导购内容维护指南.md`](docs/导购内容维护指南.md)。简言之：

改 `app/data/products.py` 后执行 `python -m app.db.seed --force` 重新灌库；
也可以直接用任何 SQLite 工具改库，改完立即生效，无需重启后端。

---

## 五、A 部分 · 智能导购 Agent

**链路**：用户提问 → 意图识别 → 工具取数 → 大模型流式生成 → 答案 + 商品引用卡片

| 能力 | 说明 |
| --- | --- |
| 位置问答 | 精确到货架编号与层数，如「A1 货架第 2 层」 |
| 营养与过敏原 | 输出五项营养成分、配料表、过敏原，并附免责说明 |
| 促销与搭配 | 基于组合规则与商品标签关联推荐，含推荐理由 |
| 库存查询 | 三态回答（充足 / 紧张 / 售罄），售罄时推荐同类在售商品 |
| 多轮上下文 | 携带最近 6 轮对话 |
| 语音输入 | 使用浏览器 Web Speech API，不支持时按钮自动禁用 |
| 扫码直达 | 摄像头扫条码或手输 SKU，直达商品详情并触发 Agent 开场白 |

**抑制幻觉的设计**：大模型只能通过 `search_product` / `get_location` / `get_nutrition` /
`get_promotions` / `recommend_pairing` / `check_stock` 六个工具取数，system prompt 明确禁止凭记忆回答价格、
营养成分与过敏原。未配置 Key 时走本地规则引擎，同样强制基于商品主数据作答。

### 扫码进入指定商品

手机端「扫一扫」按钮走的是**原生 `BarcodeDetector` + `getUserMedia`**，不需要引入任何扫码库：

- 取景框 350ms 轮询识别，命中即停并调 `GET /catalog/sku?barcode=...`
- 浏览器不支持或摄像头被拒时，自动降级为**手动输入 SKU** 兜底
- 命中后跳转到商品详情，并让 Agent 主动说一段「这个商品的亮点 + 搭配建议」开场白
- 支持 `?sku=XXX` deep link，扫码落地页、运营外链都能直接唤起同一路径

条码在 `products.barcode` 列（EAN-13，nullable + indexed）。老库升级由
`database._migrate_add_columns()` 自动 ALTER，`_backfill_product_fields()` 回填种子条码，无需手工干预。

---

## 六、B 部分 · 视觉结账

**链路**：拍摄 → 闭卷匹配 → 置信度校准 → 三态仲裁 → 分级兜底 → 账单 → 支付

### 闭卷匹配：让模型做选择题，而不是填空题

识别准确率的关键不在模型有多大，而在是否给它划定了范围。

早期做法是让模型自由描述商品（"矿泉水"、"红色的罐子"），再交给模糊检索去猜。实测暴露了严重问题——检索器永远不返回"无匹配"，只要有一点语义分就强行返回一个商品：

| 模型输出 | 系统返回 | 问题 |
| --- | --- | --- |
| 矿泉水 | 可口可乐 汽水 | 字面分 0，语义分按品类打平，排序退化成按 sku_id，永远是 SKU001 |
| 洗发水 | 可口可乐 汽水 | 属性抽取把"洗发水"误判为饮料品类 |
| 面包 | 乐事 黄瓜味薯片 | 同上 |
| 酸奶 / 泡面 | 无匹配，整件商品消失 | 模型明明看到了，却从账单里蒸发 |

改为**闭卷选择**后，29 个 SKU 的清单（ID + 名称 + 规格 + 包装形态 + 外观特征）直接注入提示词，模型只输出 `sku_id`，关键词→检索这个错配环节被彻底移除。清单外的 `sku_id` 一律丢弃。

### 置信度校准：看领先幅度，不看自报分数

模型自报的置信度普遍虚高，而「第一名领先第二名多少」更能说明问题。折算系数随分差递减：

| 头名领先幅度 | 折算系数 | 含义 |
| --- | --- | --- |
| ≥ 0.50 | 1.00 | 压倒性优势，基本采信 |
| ≥ 0.30 | 0.95 | 较有把握 |
| ≥ 0.15 | 0.85 | 有一定区分度 |
| < 0.15 | 0.70 | 难分伯仲，明显下调后交给追问 |

同样自报 0.90：独占鳌头时折算为 0.900 自动入账，与对手 0.88 并驾齐驱时折算为 0.630 转追问顾客。

### 允许说不知道

模型返回清单外 `sku_id`、或明确表示看不清时，生成**候选为空**的检测项并转人工复核，而不是强行匹配或静默丢弃。界面上显示「未识别」，可由顾客指认或店员远程核对。

### 四个示例场景覆盖四种典型情况

| 场景 | 内容 | 预期表现 |
| --- | --- | --- |
| S1 日常小采购 | 3 种 4 件，包装差异明显 | 全部自动入账，称重校验通过，可乐第二件半价生效 |
| S2 相似包装 | 两对同品牌不同口味 | 触发底部追问区，顾客点选后账单实时更新 |
| S3 拥挤与遮挡 | 6 件堆叠，1 件被压住 | 遮挡项转人工复核（不计入账单），称重报「重 65g 疑似漏检」 |
| S4 新商品测试 | 4 款新增商品，2 款与同品不同规格混淆 | 水溶C100、崂山啤酒自动入账；乐事 40g、奥利奥 97g 因与同品不同规格混淆触发追问 |

### 置信度仲裁规则

| 条件 | 判定 |
| --- | --- |
| 分数 ≥ 0.88 | 自动入账 |
| 存在可混淆候选 且 分数在 0.55 ~ 0.88 | 主动追问顾客 |
| 无可混淆候选 且 分数 ≥ 0.72 | 自动入账（没有更好的选择，追问无意义） |
| 分数 < 0.55 | 转人工复核，不计入账单 |

阈值在 `backend/.env` 中可调。

### 多信号交叉验证

称重读数与账单商品应重做差值校验，容差 25g。
S3 中被遮挡商品转入人工后不计入应重，差额 65g 立即暴露漏检——**视觉看不到的，重量能发现**。

### 人工复核闭环

转人工（review）的商品不会自动入账，界面提供两条补入路径：

- **从识别候选中选择** —— 模型给了候选但不确信，点选即可
- **从全部商品指定** —— 模型连候选都给不准时，在 29 个 SKU 中直接指定。
  后端仅对 review 项放开此权限，clarify 项仍只能在候选内选择，避免顾客误选

补入后该行以 `manual` 来源标记「人工确认」，并保留原始低置信度以便追溯。

**重量校验会随之重算**：补对商品 → 校验转为通过；补错商品 → 立刻反向报警。
也就是说人工复核同样受重量交叉验证约束，不是免检通道。

### 识别状态，界面如实区分

`mode` 字段决定结果该被信任到什么程度，前端据此显示不同颜色与提示：

| mode | 触发条件 | 界面表现 |
| --- | --- | --- |
| `vision` | 真实视觉模型单帧识别成功 | 绿色标签「视觉大模型」，结果可信 |
| `vision-vote` | 连拍多帧投票识别（前端默认连拍 3 帧） | 绿色标签 + 帧数说明，结果取多帧共识 |
| `vision-cascade:yolo-coco` | YOLO 出真实框 + 大模型逐框判类别 | 绿色标签「级联识别」，框是真实坐标 |
| `vision-failed` | 真实视觉模型调用失败（403 / 超时等） | 红色标签「识别失败」+ 提示条，引导手动录入 |
| `demo:S1` | 点击示例场景按钮 | 橙色标签「演示数据」，明确这是内置剧本 |

**拍照失败绝不用示例数据顶替。** 早期做法是失败后降级到模拟引擎继续返回商品，
后果是顾客拿到一张跟他买的东西完全无关的账单，却以为机器只是"认错了"——
这比直接报失败更伤信任。现在改成如实失败：宁可不给结果，也不能给错账单。

### OCR 辅助：读包装上的字，而不是只看外观

清单里存在「同名同口味、仅规格不同」的商品（乐事 40g/70g、奥利奥 97g/116g），
它们的外观几乎一样，唯一确定的区分依据是包装上印的净含量。所以提示词强制模型
先读包装文字（`ocr_text`）再选 sku_id，读到的文字随检测结果返回（`evidence` 字段），
并在前端「待确认 / 人工复核」面板里展示——识别依据可被肉眼验证，不是黑盒打分。

### 多帧投票：连拍三帧取共识

前端每次拍照自动连拍 3 帧一次性提交（`images` 数组），后端并行识别后投票聚合：

- 过半帧支持的商品才独立成条，少数派降为对手候选（触发追问而非直接计费）
- 三帧各说各话时合并成一条，交给分差压到人工复核——**绝不重复计费**
- 一致性会折算进置信度：三帧都认同一件的分数 > 两帧认的 > 三帧各认各的

### 级联识别：YOLO 出真实框，大模型判类别（DETECTOR=yolo）

早期检测框是按商品数量在盘面均分的**占位框**，与画面实际内容无关。
级联模式解决它：COCO 预训练的 YOLO（零训练，权重 `backend/models/yolov8n.pt`）
先圈出每个物体的真实位置，裁剪后逐个交给视觉大模型认品类。

分工依据：YOLO 圈得准但 COCO 里没有零售包装（认不出是乐事还是好丽友）；
大模型认得出商品却给不出坐标。两者一拼，得到真实坐标 + 准确品类。

安全设计：

- **只借框，不信类别**：检测器的 cls 字段一律丢弃
- **非商品拒判**：裁剪图若是人物/街景/色块，提示词强制返回空候选，宁可错过不可错认
  （实测未加这条时，bus.jpg 边缘的垃圾框曾被硬认成水溶C100 并以 0.95 置信直接入账）
- **自动回退**：检测器没提出框、或裁剪全被拒判时，退回整图识别——级联是增强，不是依赖
- **失败即降级**：ultralytics 未安装 / 权重缺失时自动退回整图识别，结账流程不受影响

`.env` 中 `DETECTOR=yolo` 启用、`DETECTOR=off` 关闭（改完需重启后端）。
实测耗时：首次请求约 13s（含权重加载 7.5s，一次性），稳态单帧约 2.7s、三帧约 4.2s。

#### 识别失败后怎么结账

识别整体失败时画面里一个检测框都没有，而 `clarify` 接口要求先有检测框，
所以另开了一条不依赖检测框的补录通道：

```bash
POST /api/v1/checkout/sessions/{session_id}/manual
{ "sku_id": "SKU028", "quantity": 2 }
```

账单区右上角的「+ 手动添加」按钮走的就是这个接口，漏检时同样可用。
人工录入的条目 `source` 记为 `manual`、置信度按 100% 计——它本来就是人确认过的。

#### 排查：视觉模型到底有没有在工作

密钥配了不等于能用。`backend/.env` 里的 `VISION_API_KEY` 可能因过期、欠费、模型未开通而被拒，此时每次拍照都会静默降级，**表现为"识别准确率极低"**——因为你拍什么它都返回内置示例。

判断依据（顶栏标签 + 悬停提示）：

```bash
curl http://127.0.0.1:8000/api/v1/health
```

| 字段 | 含义 |
| --- | --- |
| `vision_enabled: true` | 密钥有效且最近一次调用成功 |
| `vision_enabled: false` | 未配置，或最近一次调用失败 |
| `vision_error: "403 ..."` | 鉴权被拒，检查密钥是否过期、DashScope 是否已开通该模型 |
| `vision_error: null` 但未配置密钥 | 正常，属于"未启用"而非"故障" |

拿到有效密钥后，可用 `python tests/vision_live_probe.py` 快速验证链路是否打通。

---

## 七、C 部分 · 后台数据看板

URL 直访 `/admin`。六大区块自上而下，时间范围切换会联动 ① KPI / ② 热销 / ④ 趋势 / ⑥ 审计；
库存预警和最近对话是实时快照，不随时间范围变化。

| 区块 | 内容 | 数据来源 |
| --- | --- | --- |
| ① KPI 卡 × 4 | 订单数 / 销售额 / 客单价 / 售出件数，带环比 | `GET /admin/overview` |
| ② 热销 TOP 10 | 纯 SVG 水平柱图 | `GET /admin/bestsellers` |
| ③ 库存预警 | 双列：售罄（红）/ 紧张（黄） | `GET /admin/inventory/alerts` |
| ④ 销售趋势 | 纯 SVG 折线 + 柱图叠加 | `GET /admin/sales/trend` |
| ⑤ 最近对话抽样 | 卡片列表，可看顾客问了什么、Agent 怎么答 | `GET /admin/chats/recent` |
| ⑥ 审计追溯 | 见下一章节 | 见下一章节 |

**图表全部用原生 SVG 手绘**（`<rect>` / `<polyline>` 直接算坐标），不引 ECharts / Chart.js。
理由是后台看板本来就低频访问，为它引入一个几百 KB 的图表库不划算，而且手绘 SVG 能精确控制配色与响应式。

鉴权：后端 `.env` 配 `ADMIN_TOKEN` 后，所有 `/admin/*` 接口走 `Depends(_require_admin)` 校验
`X-Admin-Token` 头；未配置时开发模式放行。前端把 token 存 localStorage，后续请求自动带上。

---

## 八、D 部分 · 审计追溯

把**结账时拍摄的照片**和**最终结算清单**绑定存档并打上时间戳，用于防损核查与客诉处理。

### 写入分两个时机

```
recognize() 收到首帧 ──▶ save_pending()  写到 _pending/{session_id}.jpg
                          （不等支付完成就先抓住证据，中途断电也能回查）

pay() 支付成功     ──▶ promote_and_persist()  把 _pending 文件迁移到正式目录
                          同时把不可变 items_snapshot 与时间戳落进 audit_records
```

demo 场景（`recognize_mode` 以 `demo:` 开头）没有真实抓拍可写，只写一条占位记录：
`photo_path` 为空、识别模式明确标 demo，看板据此**灰态显示**并提示「演示数据无抓拍」。

### 为什么存不可变快照而不是外键

`items_snapshot` 是**落库那一刻的 JSON 快照**，不是指向 `order_items` 的外键。
商品改名、改价、下架之后，审计场景还能精准回查「当时这笔单子买了什么、单价多少」。
反过来若引用 `order_items`，商品表一旦 ARCHIVE 就追不回来了。

### 五个接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/admin/audit` | 列表，支持 `q` 关键字（订单号/会话号/支付方式）+ `cleared` 三态过滤 |
| GET | `/admin/audit/{order_id}` | 详情，含完整 items_snapshot |
| GET | `/admin/audit/{order_id}/photo` | 抓拍原图（同时支持 Bearer 与 `?token=`） |
| POST | `/admin/audit/{order_id}/clear` | 标记已处理，落 `cleared_by` / `cleared_at` / `clear_note` |
| DELETE | `/admin/audit/{order_id}` | 真删：清数据库行 + unlink 磁盘文件 |

`order_id` 上有 **UNIQUE 约束** + 写库前 `one_or_none()` 查重，重复支付不会产生第二行。

### 30 天自动清理

保留期 `AUDIT_RETENTION_DAYS`（默认 30 天）到期后自动删除，数据库行与磁盘文件一起清。

```bash
# 手动清理（一次性）
python -m app.cli.purge_audit
```

除此之外，后端启动时会在 FastAPI `lifespan` 里拉起一个后台协程：

```bash
# backend/.env
AUDIT_AUTO_PURGE=true          # 设 false 关闭，只保留手动脚本
AUDIT_PURGE_INTERVAL_HOURS=1   # 轮询周期（小时），设 0 同样关闭
```

协程分两阶段：**启动即清一次**（吃掉上次异常退出留下的历史残留）+ **之后每 N 小时续清**。
进程关停时 `stop_event.set()` 让循环干净退出，日志留一行 `audit auto-purge stopped`。

> 目前按单进程设计。若将来用 `uvicorn --workers N` 起多 worker，每个进程都会拉一个清理协程，
> 届时应改回 cron 调度 `purge_audit` 脚本。

---

## 九、提示词管理

早期 5 处提示词散落在 `agent.py` / `vision.py` / `text2sql.py` 里：改一个字要重启服务、没法 A/B、没法按场景切换。
现已统一抽离到 `backend/app/prompts/`，由 `loader.py` 加载、`__init__.py` 统一出口。

```
backend/app/prompts/
├── guide.yaml          # 导购主提示词 + 7 个工具（search_product / get_nutrition / …）的文案
├── checkout.yaml       # 视觉结账闭卷提示词：全图版 + 裁剪版两个 scope
├── sql.yaml            # 兜底 SQL 工具的表结构说明（SCHEMA_HINT）
├── local_fallback.yaml # 无 LLM Key 时的本地规则话术 + 引用卡片标签
├── loader.py           # 加载 + 热重载 + 启动完整性自检
└── __init__.py         # 统一出口 + PROMPTS_VERSION
```

**设计要点**

- **零新增运行时依赖**：只装了 `pyyaml`（部署需要），加载用标准库 `string.Template`，动态部分留 `$catalog` 占位符，运行时由代码注入。
- **动态内容仍走代码**：SKU 清单由 `services/vision.py` 的 `build_catalog()` 从数据库实时生成后注入模板，加商品不需要改提示词。
- **版本号可追溯**：每个 YAML 带 `__version__`，`__init__.py` 汇总成 `PROMPTS_VERSION`；`lifespan` 启动时打印 `提示词：guide=1.0 checkout=1.0 ...`，客诉复盘时可确认当时用的是哪一版。
- **启动完整性自检（fail-fast）**：必需 key、占位符、工具名齐全才放行；缺一项直接抛 `PromptLoadError` 指名道姓报错，**不静默降级到过期内嵌副本**（提示词与代码同仓提交，损坏概率等同于代码损坏）。
- **热重载仅开发态**：`PROMPT_HOT_RELOAD=true` 时改 YAML 无需重启；生产默认 `false`，靠重启生效，避免 mtime 抖动与 reload 线程安全问题。
- **向后兼容**：`services` 层通过模块级 `__getattr__` 保留 `SYSTEM_PROMPT` / `build_vision_prompt` / `SCHEMA_HINT` 等旧名，依赖它们的测试零改动。

**怎么改提示词**

1. 编辑对应 YAML（保持缩进，多行用 `|` 块标量）。
2. 跑自检：

```bash
cd backend
python -m app.cli.check_prompts     # 校验 4 个文件结构完整、工具名齐全
```

3. 重启后端（或开发态开热重载即时生效）。部署启动也会自动跑一遍自检，坏文件直接起不来。

**防回归**：`tests/test_prompts_parity.py` 逐字比对 YAML 与原始源码，搬运时错一个字、多一个空格立刻变红。

---

## 十、核心代码亮点

### 1. 闭卷选择：把填空题改成选择题

早期让模型自由描述商品再交给检索去猜，结果"洗发水"被匹配成可口可乐、"酸奶"直接消失。
根本原因是**检索器永远不会说"无匹配"**。改成把 SKU 清单注入提示词、让模型只吐 `sku_id` 后，
模糊检索这个错配环节被整个移除。

```python
def build_catalog(products: list[Product]) -> str:
    """把商品主数据压成模型能一眼扫完的候选清单。"""
    for p in products:
        lines.append(
            f"- {p.sku_id} {p.name}（{p.spec}，{shape}，外观特征：{p.visual.label}）"
        )
```

清单由 `product_repo.list_products()` 动态生成，加商品不用改一行业务代码。

### 2. 置信度折算：信领先幅度，不信自报分数

大模型自报分数普遍虚高。同样是 0.90，独占鳌头与并驾齐驱是完全不同的两回事，
所以用「头名领先第二名多少」来折算：

```python
def calibrate_confidence(top_conf: float, gap: float) -> float:
    top_conf = min(max(top_conf, 0.0), 1.0)
    if gap >= 0.50:   factor = 1.0   # 压倒性优势，基本采信
    elif gap >= 0.30: factor = 0.95
    elif gap >= 0.15: factor = 0.85
    else:             factor = 0.70  # 难分伯仲，下调后交给追问
    return round(top_conf * factor, 3)
```

### 3. 三态仲裁：没有对手时不该追问

阈值不是死的。商品库里根本没有能与之混淆的对象时，追问毫无意义——
识别结果虽不完美，但也没有更好的选择，此时应当放宽阈值：

```python
def decide_state(score, settings, has_competitors=False) -> str:
    if score >= settings.AUTO_ACCEPT_SCORE:            return "auto"
    if not has_competitors and score >= settings.SINGLE_CANDIDATE_SCORE:
        return "auto"          # 无对手，0.72 即可入账而非 0.88
    if score >= settings.CLARIFY_SCORE:                return "clarify"
    return "review"
```

### 4. 受控 SQL：给兜底工具套五道防线

模型被允许写 SQL 是危险的事，所以 `query_product_database` 走独立校验：

```python
def validate_sql(raw_sql: str) -> tuple[bool, str]:
    sql = re.sub(r"--[^\n]*", "", raw_sql)          # 先剥注释，防注释绕过
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    if ";" in sql:
        return False, "不允许执行多条语句（检测到分号）"
    if not sql.lower().startswith("select"):
        return False, "只允许 SELECT 查询"
    for keyword in FORBIDDEN_KEYWORDS:              # insert/update/drop/...
        if re.search(rf"\b{keyword}\b", sql.lower()):
            return False, f"禁止使用 {keyword.upper()} 语句"
    unknown = sorted({t for t in TABLE_PATTERN.findall(sql) if t not in ALLOWED_TABLES})
    if unknown:
        return False, f"不允许访问表：{', '.join(unknown)}"
    if not re.search(r"\blimit\b", sql.lower()):
        sql = f"{sql} LIMIT {MAX_ROWS}"             # 缺 LIMIT 自动补
    return True, sql
```

### 5. 审计是 sidecar：写盘失败只降级，绝不打断结账

主链路是结账，审计数据缺失不能让顾客结不了账。所以所有审计写操作都吞异常：

```python
except Exception as exc:
    _logger.warning("落审计记录失败 order=%s: %s", order.order_id, exc)
```

但日志必须显著（`warning` 而非 `debug`），保证后台能定位。

### 6. 路径穿越防护：拼回绝对路径后再校验一次

审计照片路径存在数据库里，接口按 `order_id` 取出来拼路径——这是典型的任意文件读风险点。
所以拼完必须再确认一次它仍在 root 下：

```python
def resolve_photo_path(settings: Settings, photo_path: str) -> Path | None:
    root = _photo_root(settings)
    try:
        abs_path = (root / photo_path).resolve()
        abs_path.relative_to(root)   # 被 ../ 绕出去会抛 ValueError
        return abs_path if abs_path.exists() else None
    except ValueError:
        return None
```

### 7. 支付幂等：订单落库与库存扣减同事务、同条件

重复支付/回调绝不能重复扣库存。做法是**以 `session_id` 作订单主键**，
只有首次创建订单的分支才扣库存：

```python
order = db.get(OrderModel, snapshot.session_id)
if order is None:
    order = OrderModel(order_id=snapshot.session_id, ...)
    db.add(order)
    db.flush()
    for item in bill.items:
        db.add(OrderItemModel(...))
    # 只有首次创建订单时扣一次；库存扣到负数会抛错让整个事务回滚
    product_repo.deduct_stock(db, _bill_quantities(bill))
```

### 8. 后台清理协程：可中断 sleep + 线程池跑同步 SQL

清理任务不能阻塞 FastAPI 的事件循环，所以同步 SQLAlchemy 一律扔进线程池；
同时用 `wait_for(stop_event.wait(), timeout=...)` 代替 `sleep`，
保证关停信号一到立刻退出，而不是等满整个 interval：

```python
while not stop_event.is_set():
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
    except asyncio.TimeoutError:
        loop = asyncio.get_event_loop()
        deleted = await loop.run_in_executor(None, _do_purge)   # 同步 SQL 走线程池
```

配 `lifespan` 的 finally 块做收尾：

```python
finally:
    stop_event.set()
    try:
        await asyncio.wait_for(purge_task, timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        purge_task.cancel()
```

### 9. 两个容易踩的隐性坑

**autoflush 的假象**：SQLAlchemy 默认 `autoflush=True`，但 `Session.query()` **不会**
自动 flush 已 `add` 的 pending 行。落订单时如果不显式 `db.flush()` 就去查 `OrderItemModel`，
拿到的 `items_snapshot` 会是空数组。

```python
db.flush()   # 必须显式，否则下面查不到刚 add 的订单项
items_iter = db.query(OrderItemModel).filter(...).all()
```

**uvicorn 会静默吞掉你的日志**：uvicorn 的 `log_config` 只配置 `uvicorn.*`，
把 root logger 的 `handlers` 清空了。业务 logger 冒泡到 root 时无人接，信息被丢弃——
表现为单元测试跑得好好的，服务启动却一条业务日志都看不到。
`app/main.py` 里补了一个 StreamHandler 解决：

```python
_root_logger = logging.getLogger()
if not any(isinstance(h, logging.StreamHandler)
           and getattr(h, "stream", None) is sys.stderr
           for h in _root_logger.handlers):
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    _root_logger.addHandler(_handler)
_root_logger.setLevel(logging.INFO)
```

注意用 `isinstance` + `stream is sys.stderr` 双重去重，否则 `--reload` 时会重复添加。

---

## 十一、接口一览

### 基础

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 服务状态与模型接入情况 |

### 导购

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/agent/ask` | 导购问答，SSE 流式 |

### 商品目录

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/catalog/products` | 商品列表（支持 keyword / category 筛选） |
| GET | `/api/v1/catalog/sku` | **按条码精确查**（扫码用） |
| GET | `/api/v1/catalog/products/{sku_id}` | 商品详情 |
| GET | `/api/v1/catalog/products/{sku_id}/recommendations` | 搭配推荐 |
| GET | `/api/v1/catalog/categories` | 品类列表 |

### 结账

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/checkout/scenes` | 示例购物盘场景 |
| POST | `/api/v1/checkout/sessions` | 创建结账会话 |
| GET | `/api/v1/checkout/sessions/{id}` | 查询会话 |
| POST | `/api/v1/checkout/sessions/{id}/recognize` | 图像识别（支持多帧） |
| POST | `/api/v1/checkout/sessions/{id}/clarify` | 顾客澄清歧义商品 |
| POST | `/api/v1/checkout/sessions/{id}/manual` | 手动补录商品（不依赖检测框） |
| POST | `/api/v1/checkout/sessions/{id}/confirm` | 确认账单 |
| POST | `/api/v1/checkout/sessions/{id}/pay` | 支付（成功后扣库存，幂等） |
| POST | `/api/v1/checkout/sessions/{id}/reset` | 重置会话 |

### 后台看板与审计（`X-Admin-Token` 校验）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/admin/overview` | KPI 概览（订单数/销售额/客单价/件数） |
| GET | `/api/v1/admin/bestsellers` | 热销 TOP N |
| GET | `/api/v1/admin/inventory/alerts` | 库存预警（售罄 / 紧张） |
| GET | `/api/v1/admin/sales/trend` | 销售趋势（按天聚合） |
| GET | `/api/v1/admin/chats/recent` | 最近对话抽样 |
| GET | `/api/v1/admin/audit` | 审计列表（关键字 + 已处理三态过滤） |
| GET | `/api/v1/admin/audit/{order_id}` | 审计详情（含清单快照） |
| GET | `/api/v1/admin/audit/{order_id}/photo` | 抓拍原图 |
| POST | `/api/v1/admin/audit/{order_id}/clear` | 标记已处理 |
| DELETE | `/api/v1/admin/audit/{order_id}` | 删除（含磁盘文件） |

结账状态机：`CREATED → CAPTURING → RECOGNIZING → NEED_CLARIFY → CONFIRMED → PAYING → PAID`
（异常分支 `REVIEWING` / `FAILED`）

---

## 十二、测试与评测

测试分两层，回答的是两个不同的问题：

| 层 | 位置 | 回答的问题 | 什么时候跑 |
| --- | --- | --- | --- |
| **回归测试** | `backend/tests/` | 这次改动有没有弄坏已有功能 | 每次改代码 |
| **效果评测** | `backend/eval/` | 导购答得准不准、有没有在编 | 改 prompt / 检索 / 约束前后 |

两者互补，不能互相替代。回归测试只验证**已有行为没坏**，所以它永远发现不了
「某个工具从上线起就没生效过」这类问题——只有效果评测能发现，本项目就靠它挖出两个长期缺陷。

### 1. 回归测试

所有后端测试都**不需要 pytest**，直接 `python tests/xxx.py` 即可运行。

| 脚本 | 需要服务 | 覆盖内容 |
| --- | --- | --- |
| `tests/db_test.py` | 否 | 建库灌种子、混合检索、受控 SQL 五道防线、结账链路与订单落库 |
| `tests/new_sku_test.py` | 否 | 新增 SKU 是否进入闭卷清单、三态判定、导购检索命中、称重交叉验证 |
| `tests/stock_test.py` | 否 | 库存扣减、售罄拦截、重复支付幂等 |
| `tests/vision_accuracy_test.py` | 否 | 给识别层喂各种模型输出，验证闭卷匹配/未知 SKU 丢弃/校准/仲裁（不耗 token） |
| `tests/cascade_test.py` | 否 | 多帧投票聚合、级联框坐标传递、误检裁剪拒判（全 mock） |
| `tests/test_cascade_instances.py` | 否 | 按框实例聚合：同帧两包不合并、不同规格不合并、多帧不重复计数 |
| `tests/test_ruler_40g_fix.py` | 否 | 单候选硬纠偏：OCR 净含量 + 尺子实测长度把 40g / 70g 分对 |
| `tests/test_size_ranking_sku_fix.py` | 否 | 尺寸排序/排除法真的重设 `candidates[0].sku_id`；Phantom 小框降级为 review |
| `tests/test_weight_resolve.py` | 否 | 相似组重量终裁：总重残差枚举规格组合，唯一容差命中才切换 SKU |
| `tests/test_confirm_guard.py` | 否 | 未解决 review（含空候选「未识别品类」）在 confirm 阶段强制拦截，且不误拦已人工指认项 |
| `tests/test_audit_auto_purge.py` | 否 | 自动清理三阶段：启动即清 / interval 续清 / 干净退出 |
| `tests/test_prompts_parity.py` | 否 | 提示词 YAML 与原始源码逐字比对，搬运错一字即红（抽离防回归） |
| `tests/smoke_test.py` | **是** | 端到端：S1 顺畅路径、S2 歧义追问、S3 转人工与称重报警、S4 新商品、导购流式 |
| `tests/vision_live_probe.py` | **是** + 有效 Key | 用合成图片走通真实视觉模型（耗少量 token） |

前 12 个离线脚本一轮跑完：

```bash
cd backend
for f in db_test new_sku_test stock_test vision_accuracy_test cascade_test \
         test_cascade_instances test_confirm_guard test_audit_auto_purge \
         test_prompts_parity test_ruler_40g_fix test_size_ranking_sku_fix test_weight_resolve; do
  python "tests/$f.py" || echo "FAIL $f"
done
```

提示词结构自检（不属于 pytest，直接跑）：

```bash
cd backend
python -m app.cli.check_prompts        # 校验 4 个 YAML 结构完整、工具名齐全
```

前端类型检查与构建：

```bash
cd frontend && npm run build
```

### 2. 效果评测：58 条标注用例 + 三方案消融

回归测试过了只能说明「没坏」，说明不了「答得准」。`backend/eval/` 回答后者。

同一套 58 条用例跑三种配置，**A→B 只加工具，B→C 只加约束**，差值即各自贡献：

| 配置 | 注册工具 | system prompt | 衡量 |
| --- | --- | --- | --- |
| **A** 纯 LLM 直答 | ❌ 不注册 | 只给店员人格，不给任何规则 | 不接店内数据时，模型会编多离谱 |
| **B** 仅工具调用（无约束） | ✅ 7 个（6 领域 + 1 兜底 SQL） | 只给店员人格，不给任何规则 | 光把工具挂上去够不够 |
| **C** 工具 + 边界约束（**线上现状**） | ✅ 7 个（6 领域 + 1 兜底 SQL） | 完整提示词 + 第二轮硬约束 | 「给大模型划边界」的增量收益 |

实测（58 条，temperature=0.3）：

| 配置 | 事实正确率 | 幻觉率 | 库外商品率 | 越界率 | 协议标记泄漏 | 工具命中率 | 平均延迟 | prompt / completion tok |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A 纯 LLM 直答 | 37.9% | 5.2% | 19.0% | 0.0% | 0.0% | — | 2,797 ms | 95 / 371 |
| B 仅工具（无约束） | 94.8% | 0.0% | 1.7% | 0.0% | 5.2% | 100% | 2,302 ms | 1,409 / 217 |
| **C 工具 + 边界约束** | **98.3%** | **0.0%** | **0.0%** | **0.0%** | **0.0%** | **98.2%** | 2,367 ms | 2,054 / 233 |

这张表怎么读：

- **A→B（事实 37.9%→94.8%、库外商品 19.0%→1.7%）**：把店内数据接进来是最大的一跳。
  约束再强也救不了「模型手里根本没有店内数据」，它只能靠记忆编。
- **B→C（事实 94.8%→98.3%、协议标记泄漏 5.2%→0%）**：约束的价值不是「让它知道」，
  而是「不让它跑偏」。C 组第二轮**不传 tools 参数**，是从协议层面禁止再调工具，
  而不是在提示词里求它别调；`<|DSML|tool_calls|>` 这类协议标记泄漏随之归零。
- **成本结构**：C 组 prompt 是 A 组的 21 倍（2,054 vs 95 tok），换来约 60 个百分点的正确率。
  这套系统的成本贵在 prompt，不在生成。

#### 评测集挖出的两个长期缺陷

第一轮跑就红了两个此前一直没人发现的 bug——不是评测写错了，是代码真错：

| 缺陷 | 现象 | 根因 | 修复后 |
| --- | --- | --- | --- |
| `check_stock` 工具**从上线起没生效过** | 顾客问「还有货吗」，工具恒返回「请提供要查询的 sku_id 列表」 | 对模型声明的参数是 `keyword`，`run_tool` 却只读 `sku_ids`，声明与实现错位 | 库存题 1/5 → **5/5** |
| 「既无糖又要是茶」检索不到 | 唯一的无糖茶（东方树叶）0 命中 | `HEALTH_TAGS` 把「无糖」映射为 `0糖/无糖`，商品标签写的是「无糖茶」，严格相等判不中；且「茶」不在风味词表 | 互相包含匹配 + 补词表 |

第一个尤其典型：日常对话里 `search_product` 顺带返回的库存字段把它兜住了，
**人工试用完全看不出来**；只有拿 58 条题一条条过，5 道库存题全挂才暴露。
套用同一套判据回算，C 组 91.4% → 98.3%，B 组 87.9% → 94.8%。

#### 怎么跑

```bash
cd backend

# 1) 先把数据库重置回种子态（评测会读库存与促销，数据跑脏了就不可比）
python -m app.db.seed --force

# 2) 重建商品快照（改了 products.py 或重置过数据库才需要）
python eval/refresh_snapshot.py

# 3) 跑三组（58 题 × 3 组，约 2 分钟 / 174 次调用）
python eval/run_eval.py --arm all

# 4) 打分（不调模型；判据改了直接重跑这一步，不烧额度）
python eval/score_eval.py
```

产物在 `eval/results/`：`raw_{pure,tools,guard}.jsonl`（逐题原始记录，含工具调用轨迹与 token 用量）、
`metrics.json`（机器可读指标）、`report.md`（总表 + 分类别 + 全部问题用例明细）。

`run` 与 `score` 分离是刻意的：**判据一定会改**，分离后改判据不用重新烧 API 额度。

用例构成、指标定义、判据修正过程与 5 条已知局限见 [`backend/eval/README.md`](backend/eval/README.md)。

> 诚实边界：58 条是「够发现问题、不够下结论」的规模（单条 = 1.7pp）；
> 幻觉率是**下限**（只统计能被自动校验的断言）；用例由作者本人编写，存在
> 「按自己系统的能力设计题目」的偏差，所以它只能算内部评测，不能算基准。

### 3. 检索层对照实验：七种策略 × 两个输入口径

评测集还回答了一个更底层的问题：**检索本身该怎么实现**。
线上走的是「0.65 属性分 + 0.35 字面分」的加权求和，这个配比一直没人验证过。

同一套用例、同一份商品库，只换排序逻辑。下表用**贴近线上的口径**（先用 LLM 把问句改写成
检索关键词，再交给检索层，也就是模型真实调用 `search_product` 的样子）：

| 策略 | Recall@1 | Recall@3 | 库外题返回空 |
| --- | --- | --- | --- |
| 纯字面（关键词） | 84.6% | 84.6% | 100% |
| 纯属性语义（规则词典） | 44.2% | 53.8% | 100% |
| **现线上**（0.65 属性 + 0.35 字面） | 92.3% | 94.2% | 100% |
| 纯向量（text-embedding-v3） | 94.2% | 98.1% | **0%** |
| RRF 融合（向量也参与放行） | 96.2% | 96.2% | **0%** |
| **RRF + 结构化闸门** | **96.2%** | **96.2%** | **100%** |

三个发现：

1. **口径选错会得出错误结论。** 如果把顾客原话直接喂检索层，纯向量 92.3% 对现线上 84.6%，
   看起来「该换成向量」；但换成线上真实的短关键词后，差距只有 **+1.9pp**——
   向量的强项是扛住长口语问句（「抽纸在哪个区？」），而线上模型传进来的本来就是「抽纸」，
   这项优势被上游的查询改写提前吃掉了。**该换的不是「字面 vs 向量」，而是「加权求和 vs RRF 融合」。**
2. **向量不能兼任「库里有没有这件东西」的闸门。** 纯向量召回最高，却一条库外题都挡不住
   （「iPhone」→ 卫龙亲嘴烧、奥利奥、蒙牛雪糕）。扫阈值可见：库外题最高余弦 0.530、
   库内题最低 0.475，两条分布重叠；要把库外题全挡住得把阈值提到 0.60，那时库内题只剩 40/52 能过。
   → 召回交给多路融合，**闸门只能由结构化信号把关**。
3. **一条被数据否掉的方案**：「结构化闸门 + 强向量补召回」与不带强向量版全部指标完全相同，
   0.60 那条路径没有任何增量，是应该删掉的复杂度。负结果留在报告里，免得下次再想一遍。

```bash
cd backend
python eval/retrieval_ablation.py               # 两个口径都跑（约 1 分钟）
python eval/retrieval_ablation.py --mode raw    # 只跑原句口径
```

向量用 DashScope `text-embedding-v3`（1024 维，复用项目现有的那一个 Key），
按文本缓存在 `eval/results/retrieval_vectors.json`（该文件不入库，首次运行会自动重算）。
**没有引入 FAISS**：29 个向量做穷举点积是微秒级，为这个规模装向量索引属于过度设计。

---

## 十三、已知限制

- 支付为模拟实现，未对接真实支付网关，无商户号与回调验签
- 示例场景（`demo:S*`）返回内置剧本数据，不代表真实模型精度，界面明确标注「演示数据」
  ——演示数据在审计追溯里**没有真实抓拍照片**，只有结算清单与时间戳
- 级联模式用的是 COCO 通用权重，只提供定位（框）不提供品类；换成 `training/`
  工作区微调出的 `best.pt` 后品类也能本地判断（届时只需改 `.env` 里的 `YOLO_WEIGHTS`）
- 未开启级联（`DETECTOR=off`）时检测框仍是占位框；开启后遮挡堆叠场景的计数
  仍依赖大模型"数数"，真实计数需要检测权重微调后才能保证
- 后台看板的 `ADMIN_TOKEN` 是单一静态口令，无角色分级、无登录态过期，仅满足演示级隔离
- 自动清理协程按**单进程**设计；`uvicorn --workers N` 多 worker 部署时需改回 cron 调度
- 人脸/隐私合规处理未实现，真实部署需补充边缘脱敏与告知同意流程
