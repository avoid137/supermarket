# 智选 · 2026-09-01 上午工作汇总（含清单批量问询）

## 三件事一次性落地（手机端）

### ① 手机端访问连不上 → 已实测 + 兜底脚本
- 根因：Windows 防火墙 + VMware 多网卡假 IP
- 落地：`enable-lan.bat`、`run-usb-bridge.bat`、手册 §0.5
- 真实 Wi-Fi IP：`10.50.112.107`

### ② 顶部条简化 + 商品橱窗主页（HomeView）
- 窄屏 (≤700px)：`.status` 模型标签 / `.tagline` 副标题 / Tab badge 全部隐藏
- 电脑端：状态条照常显示（调试用）
- 路由：`/` → HomeView（之前 redirect 到 /guide）
- HomeView 含：搜索 + 分类横滑（全部/饮料/零食/日用/优惠） + 3 列商品卡片
- 商品卡：SVG 矢量图 + 名字 + 品牌 · 规格 + 价格（红字）+ 库存三态（绿/黄/红）+ 货架位置 + 优惠徽章
- 点击卡片 → `/guide?sku=SKUxxx` → GuideView 弹详情卡 + Agent 自动开场白

### ③ 购物清单批量问询 ← 新加
- HomeView 每张卡片右上角 `+` / `✓` 按钮：**快速加入清单**，不立即跳转
- 加 `.pick-btn on` 状态、`.pick-btn:hover` 反馈
- 选中 ≥ 1 件 → 底部弹出 sticky 抽屉：
  - 已选商品横滑列表（带 × 单独移除）
  - 「帮我对比这几样」按钮 → 自动组装 prompt，跳 `/guide?q=...`
  - 「还差什么搭配？」按钮 → 推荐组合 prompt，跳 `/guide?q=...`
- GuideView `handleDeepLink` 扩展支持 `?q=` 参数（之前只支持 `?sku=`）

## 当前服务

| 服务 | 端口 | 状态 |
|---|---|---|
| 后端 uvicorn | 0.0.0.0:8000 | ✅ 200 |
| 前端 Vite dev | 0.0.0.0:5173 | ✅ 200 |
| 商品总数 | — | 29（饮料 10 + 零食 13 + 日用 6）|

## API 路由实测

```
GET  /                              → 200 (HomeView SPA)
GET  /api/v1/catalog/products       → 200 (29 items)
GET  /guide                         → 200 (Vue Router)
GET  /guide?sku=SKU001              → 200 (单件 deep link)
GET  /guide?q=...                   → 200 (批量清单 deep link)
```

## 修改/新增文件
- ✏️ `frontend/src/App.vue`：新增「商城」Tab，窄屏样式完善
- ✏️ `frontend/src/router/index.ts`：`/` → HomeView
- ✏️ `frontend/src/views/HomeView.vue`：**新增**，~590 行
- ✏️ `frontend/src/views/GuideView.vue`：`handleDeepLink` 扩展支持 `?q=`
- ✏️ `enable-lan.bat`、`run-usb-bridge.bat`：兜底脚本
- ✏️ `docs/局域网手机访问手册.md`：补 §0.5 / §5 FAQ
- 构建：`dist/assets/index-*.js` 142 KB / `index-*.css` 34 KB（gzip 54 KB + 6 KB）

## 经验沉淀
- 窄屏顶部条 `@media (max-width: 700px)` 隐藏状态/副标题/badge
- `RouterLink exact-active-class` 解决 `/` 前缀冲突
- 商品卡 promo 用 `promotions[0].desc`，不用 `discount` 数值
- Quick-add 按钮 `@click.stop` 防止冒泡触发卡片跳转
- Deep link 同时支持 `?sku=`（弹详情卡）和 `?q=`（直接提问）
- 手机端 grid `padding-bottom` 必须留空给底部 sticky 抽屉
