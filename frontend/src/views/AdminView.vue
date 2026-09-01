<script setup lang="ts">
/**
 * 后台看板（管理员视角）
 *
 * 入口：URL 直访 /admin。默认无鉴权（dev 演示）；若后端 env 里设了
 * ADMIN_TOKEN，前端需要在弹窗里输一次后存 localStorage，每次请求带 Bearer。
 *
 * 六大区块（自上而下）：
 *   1. KPI 卡 × 4   —— 订单数 / 销售额 / 客单价 / 售出件数（带同比）
 *   2. 热销 TOP10   —— 纯 SVG 水平柱图
 *   3. 库存预警     —— 双列（售罄红 / 紧张黄）
 *   4. 销售趋势     —— 纯 SVG 折线 + 柱图叠加
 *   5. 最近对话     —— 卡片列表
 *   6. 审计追溯     —— 已支付订单的抓拍图 + 不可变清单快照（防损/客诉取证）
 *
 * 时间范围切换会影响 ① KPI / ② 热销 / ④ 趋势 / ⑥ 审计；库存预警和最近对话是实时快照。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api'
import type {
  AuditDetail,
  AuditItem,
  Bestseller,
  ChatTurn,
  InventoryAlerts,
  OverviewResponse,
  SalesTrendPoint,
} from '@/types'

const router = useRouter()

const rangeDays = ref<number>(7)
const ranges = [
  { v: 1, label: '今日' },
  { v: 7, label: '近 7 天' },
  { v: 14, label: '近 14 天' },
  { v: 30, label: '近 30 天' },
]

const tokenInput = ref(localStorage.getItem('admin_token') || '')
const showTokenDialog = ref(false)
const tokenSaved = ref(false)

function saveToken() {
  const v = tokenInput.value.trim()
  if (v) {
    localStorage.setItem('admin_token', v)
    tokenSaved.value = true
  } else {
    localStorage.removeItem('admin_token')
    tokenSaved.value = false
  }
  showTokenDialog.value = false
  refresh()
}
function clearToken() {
  tokenInput.value = ''
  localStorage.removeItem('admin_token')
  tokenSaved.value = false
}

const overview = ref<OverviewResponse | null>(null)
const bestsellers = ref<Bestseller[]>([])
const inventory = ref<InventoryAlerts | null>(null)
const trend = ref<SalesTrendPoint[]>([])
const chats = ref<ChatTurn[]>([])

const loading = ref(false)
const error = ref('')
const lastUpdated = ref<Date | null>(null)

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    const tokenArg = tokenInput.value || localStorage.getItem('admin_token') || undefined
    const [ov, bs, inv, tr, ch] = await Promise.all([
      api.adminOverview(rangeDays.value, tokenArg),
      api.adminBestsellers(rangeDays.value, 10, tokenArg),
      api.adminInventoryAlerts(tokenArg),
      api.adminSalesTrend(rangeDays.value, tokenArg),
      api.adminChatsRecent(8, tokenArg),
    ])
    overview.value = ov
    bestsellers.value = bs
    inventory.value = inv
    trend.value = tr
    chats.value = ch
    lastUpdated.value = new Date()
    await refreshAudit()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(refresh)

// ---- 工具：金额格式化、相对时间 ----
function yuan(n: number) {
  return `¥${n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
}
function relativeTime(ts: number) {
  const diff = Date.now() / 1000 - ts
  if (diff < 60) return `${Math.floor(diff)} 秒前`
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}
function deltaArrow(pct: number | null) {
  if (pct === null) return { txt: '—', cls: 'flat' }
  if (pct > 0) return { txt: `+${pct}%`, cls: 'up' }
  if (pct < 0) return { txt: `${pct}%`, cls: 'down' }
  return { txt: '持平', cls: 'flat' }
}

// ---- 图表数据 ----
const maxBestSellerQty = computed(() =>
  bestsellers.value.reduce((m, b) => Math.max(m, b.qty_sold), 0) || 1,
)
const maxTrendRevenue = computed(() =>
  trend.value.reduce((m, t) => Math.max(m, t.revenue), 0) || 1,
)
const totalTrendRevenue = computed(() =>
  trend.value.reduce((s, t) => s + t.revenue, 0),
)
const totalTrendOrders = computed(() =>
  trend.value.reduce((s, t) => s + t.orders, 0),
)

// 折线点坐标（视口 600×180，padding 24）
function trendPoint(i: number, n: number, revenue: number): { x: number; y: number } {
  const W = 600
  const H = 180
  const PAD = 24
  if (n <= 1) return { x: PAD, y: H - PAD - (revenue / maxTrendRevenue.value) * (H - PAD * 2) }
  const x = PAD + (i / (n - 1)) * (W - PAD * 2)
  const y = H - PAD - (revenue / maxTrendRevenue.value) * (H - PAD * 2)
  return { x, y }
}

const trendPolyline = computed(() => {
  const n = trend.value.length
  if (!n) return ''
  return trend.value
    .map((t, i) => {
      const p = trendPoint(i, n, t.revenue)
      return `${i === 0 ? 'M' : 'L'} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`
    })
    .join(' ')
})

const trendAreaPath = computed(() => {
  const n = trend.value.length
  if (!n) return ''
  const W = 600
  const H = 180
  const PAD = 24
  const top = trend.value
    .map((t, i) => {
      const p = trendPoint(i, n, t.revenue)
      return `${i === 0 ? 'M' : 'L'} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`
    })
    .join(' ')
  return `${top} L ${(W - PAD).toFixed(1)} ${(H - PAD).toFixed(1)} L ${PAD.toFixed(1)} ${(H - PAD).toFixed(1)} Z`
})

// ---- 第六区块：审计追溯 ----
type AuditFilter = 'all' | 'yes' | 'no'
const auditList = ref<AuditItem[]>([])
const auditTotal = ref(0)
const auditQ = ref('')
const auditFilter = ref<AuditFilter>('all')
const auditLoading = ref(false)
const auditError = ref('')
const auditDetailOpen = ref(false)
const auditDetail = ref<AuditDetail | null>(null)
const auditDetailLoading = ref(false)
const clearNoteInput = ref('')
const clearOperatorInput = ref(localStorage.getItem('admin_operator') || '店员')

async function refreshAudit() {
  auditLoading.value = true
  auditError.value = ''
  try {
    const tokenArg = tokenInput.value || localStorage.getItem('admin_token') || undefined
    const resp = await api.adminAuditList(
      { days: rangeDays.value, q: auditQ.value || undefined, cleared: auditFilter.value, limit: 30, offset: 0 },
      tokenArg,
    )
    auditList.value = resp.items
    auditTotal.value = resp.total
  } catch (e) {
    auditError.value = (e as Error).message
    auditList.value = []
    auditTotal.value = 0
  } finally {
    auditLoading.value = false
  }
}

async function openAuditDetail(item: AuditItem) {
  auditDetailOpen.value = true
  auditDetailLoading.value = true
  auditDetail.value = null
  try {
    const tokenArg = tokenInput.value || localStorage.getItem('admin_token') || undefined
    auditDetail.value = await api.adminAuditDetail(item.order_id, tokenArg)
  } catch (e) {
    auditDetail.value = null
    auditError.value = (e as Error).message
  } finally {
    auditDetailLoading.value = false
  }
}

function closeAuditDetail() {
  auditDetailOpen.value = false
  auditDetail.value = null
  clearNoteInput.value = ''
}

async function markCleared() {
  if (!auditDetail.value) return
  const operator = clearOperatorInput.value.trim() || '店员'
  localStorage.setItem('admin_operator', operator)
  try {
    const tokenArg = tokenInput.value || localStorage.getItem('admin_token') || undefined
    const updated = await api.adminAuditClear(
      auditDetail.value.order_id,
      operator,
      clearNoteInput.value.trim(),
      tokenArg,
    )
    auditDetail.value = updated
    await refreshAudit()
  } catch (e) {
    auditError.value = (e as Error).message
  }
}

async function deleteAudit() {
  if (!auditDetail.value) return
  if (!confirm(`确定要删除订单 ${auditDetail.value.order_id} 的审计记录吗？\n此操作不可逆，照片与清单将一并删除。`)) return
  try {
    const tokenArg = tokenInput.value || localStorage.getItem('admin_token') || undefined
    await api.adminAuditDelete(auditDetail.value.order_id, tokenArg)
    closeAuditDetail()
    await refreshAudit()
  } catch (e) {
    auditError.value = (e as Error).message
  }
}

function photoSrc(item: AuditItem | AuditDetail): string {
  if (!item.has_photo) return ''
  const tokenArg = tokenInput.value || localStorage.getItem('admin_token') || ''
  // 详情用缩略图 inline base64，省一次请求；列表卡片用占位色块 + onerror 兜底
  if ('photo_inline_b64' in item && item.photo_inline_b64) {
    return `data:image/jpeg;base64,${item.photo_inline_b64}`
  }
  return api.adminAuditPhotoUrl(item.order_id, tokenArg)
}

function fmtTime(ts: number) {
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function sourceLabel(s: string) {
  return s === 'auto' ? '自动' : s === 'clarify' ? '顾客确认' : s === 'manual' ? '人工录入' : s
}
</script>

<template>
  <div class="admin-page">
    <!-- 顶栏 -->
    <header class="topbar">
      <div class="brand-block">
        <div class="logo">M</div>
        <div class="brand-text">
          <h1>管理后台</h1>
          <p class="tagline">实时经营数据 · 演示态数据每 30 秒刷新</p>
        </div>
      </div>

      <div class="right-tools">
        <div class="range-group">
          <button
            v-for="r in ranges"
            :key="r.v"
            :class="['range-btn', { active: rangeDays === r.v }]"
            @click="rangeDays = r.v; refresh()"
          >
            {{ r.label }}
          </button>
        </div>

        <button v-if="!tokenSaved && tokenInput" class="tool ghost" @click="showTokenDialog = true">
          🔒 设 Token
        </button>
        <button v-else-if="tokenSaved" class="tool ghost" @click="clearToken" title="已开启 Bearer 校验">
          🔓 清 Token
        </button>

        <button class="tool primary" :disabled="loading" @click="refresh">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>

        <button class="tool ghost" @click="router.push('/')">← 回到顾客端</button>
      </div>
    </header>

    <!-- Token 对话框（首次进入 + 用户主动开启时） -->
    <div v-if="showTokenDialog" class="token-overlay" @click.self="showTokenDialog = false">
      <div class="token-card">
        <h3>管理员鉴权</h3>
        <p>留空表示 dev 演示态（无鉴权）。后端 <code>ADMIN_TOKEN</code> 留空时也不校验。</p>
        <input v-model="tokenInput" placeholder="Bearer token（可选）" @keyup.enter="saveToken" />
        <div class="token-actions">
          <button class="ghost" @click="showTokenDialog = false">取消</button>
          <button class="primary" @click="saveToken">保存并刷新</button>
        </div>
      </div>
    </div>

    <main class="dashboard">
      <!-- 错误条 -->
      <div v-if="error" class="error-bar">⚠️ {{ error }} <button @click="refresh">重试</button></div>

      <!-- 1. KPI 卡 × 4 -->
      <section class="kpi-row">
        <div v-if="!overview" class="kpi-card skeleton"><div class="big">–</div><div class="lbl">加载中</div></div>
        <template v-else>
          <div class="kpi-card">
            <div class="kpi-label">订单数</div>
            <div class="kpi-num">{{ overview.orders }}</div>
            <div :class="['kpi-delta', deltaArrow(overview.delta.orders_pct).cls]">
              较上期 {{ deltaArrow(overview.delta.orders_pct).txt }}
            </div>
          </div>
          <div class="kpi-card primary">
            <div class="kpi-label">销售额</div>
            <div class="kpi-num">{{ yuan(overview.revenue) }}</div>
            <div :class="['kpi-delta', deltaArrow(overview.delta.revenue_pct).cls]">
              较上期 {{ deltaArrow(overview.delta.revenue_pct).txt }}
            </div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">客单价</div>
            <div class="kpi-num">{{ yuan(overview.avg_basket) }}</div>
            <div class="kpi-delta flat">{{ overview.customers }} 位独立顾客</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">售出件数</div>
            <div class="kpi-num">{{ overview.items_sold }}</div>
            <div class="kpi-delta flat">{{ overview.window_days }} 天累计</div>
          </div>
        </template>
      </section>

      <!-- 2. 热销 TOP10 + 库存预警 -->
      <section class="row-2">
        <!-- 热销 -->
        <div class="card">
          <div class="card-head">
            <h2>热销 TOP 10</h2>
            <span class="hint">按销量 · 近 {{ rangeDays }} 天</span>
          </div>
          <div v-if="!bestsellers.length" class="empty">暂无销售数据</div>
          <div v-else class="bars">
            <div v-for="(b, i) in bestsellers" :key="b.sku_id" class="bar-row">
              <div class="rank">{{ i + 1 }}</div>
              <div class="bar-name" :title="b.name">{{ b.name }}</div>
              <div class="bar-track">
                <div
                  class="bar-fill"
                  :style="{ width: `${(b.qty_sold / maxBestSellerQty) * 100}%` }"
                ></div>
              </div>
              <div class="bar-qty">{{ b.qty_sold }}</div>
              <div class="bar-rev">{{ yuan(b.revenue) }}</div>
            </div>
          </div>
        </div>

        <!-- 库存预警 -->
        <div class="card">
          <div class="card-head">
            <h2>库存预警</h2>
            <span class="hint">阈值 ≤ {{ inventory?.threshold ?? 5 }} 件</span>
          </div>
          <div v-if="!inventory || (inventory.sold_out.length === 0 && inventory.low_stock.length === 0)" class="empty all-ok">
            ✅ 所有商品库存充足
          </div>
          <div v-else class="alerts">
            <div v-if="inventory.sold_out.length" class="alert-col sold">
              <div class="col-head">
                <span class="dot sold-dot"></span> 已售罄（{{ inventory.sold_out.length }}）
              </div>
              <div v-for="p in inventory.sold_out" :key="p.sku_id" class="alert-row">
                <div class="alert-name">{{ p.name }}</div>
                <div class="alert-meta">{{ p.category }} · {{ yuan(p.price) }}</div>
              </div>
            </div>
            <div v-if="inventory.low_stock.length" class="alert-col low">
              <div class="col-head">
                <span class="dot low-dot"></span> 库存紧张（{{ inventory.low_stock.length }}）
              </div>
              <div v-for="p in inventory.low_stock" :key="p.sku_id" class="alert-row">
                <div class="alert-name">{{ p.name }}</div>
                <div class="alert-meta">仅剩 <strong>{{ p.stock }}</strong> · {{ p.category }}</div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- 3. 销售趋势 -->
      <section class="card">
        <div class="card-head">
          <h2>销售趋势 · 近 {{ rangeDays }} 天</h2>
          <div class="trend-total">
            合计 <strong>{{ yuan(totalTrendRevenue) }}</strong> · {{ totalTrendOrders }} 单
          </div>
        </div>
        <div v-if="!trend.length" class="empty">暂无趋势数据</div>
        <div v-else class="chart-wrap">
          <svg viewBox="0 0 600 180" preserveAspectRatio="none" class="trend-chart">
            <!-- Y 轴网格 -->
            <g class="grid">
              <line v-for="i in 4" :key="i" x1="24" :x2="576" :y1="24 + (i - 1) * 44" :y2="24 + (i - 1) * 44" />
            </g>
            <!-- 区域 + 折线 -->
            <path :d="trendAreaPath" fill="url(#trend-grad)" opacity="0.5" />
            <path :d="trendPolyline" fill="none" stroke="var(--brand)" stroke-width="2.5" />
            <!-- 圆点 -->
            <circle
              v-for="(t, i) in trend"
              :key="i"
              :cx="trendPoint(i, trend.length, t.revenue).x"
              :cy="trendPoint(i, trend.length, t.revenue).y"
              r="3"
              fill="var(--brand)"
            >
              <title>{{ t.date }} · {{ yuan(t.revenue) }} · {{ t.orders }} 单</title>
            </circle>
            <defs>
              <linearGradient id="trend-grad" x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stop-color="var(--brand)" stop-opacity="0.5" />
                <stop offset="100%" stop-color="var(--brand)" stop-opacity="0" />
              </linearGradient>
            </defs>
          </svg>
          <div class="x-axis">
            <span v-for="(t, i) in trend" :key="i" class="x-tick">
              {{ t.date.slice(5) }}<span v-if="t.orders" class="ord-pill">{{ t.orders }}</span>
            </span>
          </div>
        </div>
      </section>

      <!-- 5. 最近对话 -->
      <section class="card">
        <div class="card-head">
          <h2>最近对话抽样</h2>
          <span class="hint">最新 {{ chats.length }} 轮</span>
        </div>
        <div v-if="!chats.length" class="empty">还没有对话记录，让顾客在顾客端问一个问题试试</div>
        <div v-else class="chat-list">
          <article v-for="(c, i) in chats" :key="i" class="chat-card">
            <header class="chat-head">
              <div class="chat-meta">
                <span class="intent-pill" :class="c.intent">{{ c.intent }}</span>
                <span class="mode-pill">{{ c.mode }}</span>
                <span class="time">{{ relativeTime(c.created_at) }}</span>
              </div>
            </header>
            <div class="chat-q">问：{{ c.question }}</div>
            <div class="chat-a">答：{{ c.answer }}</div>
            <div v-if="c.citations.length" class="chat-cites">
              <span v-for="cit in c.citations" :key="cit.sku_id" class="cite-chip">
                {{ cit.name }}
              </span>
            </div>
          </article>
        </div>
      </section>

      <!-- 6. 审计追溯 -->
      <section class="card audit-card">
        <div class="card-head audit-head">
          <div class="audit-head-left">
            <h2>审计追溯</h2>
            <span class="hint">每张已支付订单配抓拍图与不可变清单快照，用于防损与客诉取证</span>
          </div>
          <div class="audit-head-tools">
            <input
              v-model="auditQ"
              class="audit-q"
              placeholder="订单号 / 会话号 / 支付方式"
              @keydown.enter="refreshAudit"
              @input="auditQ.length === 0 && refreshAudit()"
            />
            <div class="audit-filter">
              <button
                v-for="opt in [{v:'all',l:'全部'},{v:'no',l:'未处理'},{v:'yes',l:'已处理'}]"
                :key="opt.v"
                :class="{ active: auditFilter === opt.v }"
                @click="auditFilter = opt.v as AuditFilter; refreshAudit()"
              >
                {{ opt.l }}
              </button>
            </div>
          </div>
        </div>

        <div v-if="auditError" class="error">{{ auditError }}</div>

        <div v-if="auditLoading && !auditList.length" class="empty">加载中…</div>
        <div v-else-if="!auditList.length" class="empty">
          该时间窗口内没有已支付订单可审计。在顾客端完成一笔结账后会出现在这里。
        </div>
        <div v-else class="audit-grid">
          <article
            v-for="item in auditList"
            :key="item.order_id"
            class="audit-tile"
            :class="{ cleared: item.cleared, nodata: !item.has_photo }"
            @click="openAuditDetail(item)"
          >
            <div class="audit-thumb">
              <img
                v-if="item.has_photo"
                :src="photoSrc(item)"
                alt=""
                loading="lazy"
                @error="($event.target as HTMLImageElement).style.display='none'"
              />
              <div v-else class="audit-thumb-empty">
                <span>演示数据</span>
                <em>无实拍</em>
              </div>
              <span v-if="item.cleared" class="audit-cleared-pin">已处理</span>
            </div>
            <div class="audit-meta">
              <div class="audit-id">{{ item.order_id }}</div>
              <div class="audit-sub">
                <span>{{ fmtTime(item.created_at) }}</span>
                <span>{{ item.items_count }} 件 · {{ item.pay_method || '-' }}</span>
              </div>
              <div class="audit-amount">{{ yuan(item.bill_payable) }}</div>
            </div>
          </article>
        </div>

        <div v-if="auditTotal > auditList.length" class="audit-more">
          共 {{ auditTotal }} 条，当前显示前 {{ auditList.length }} 条
        </div>
      </section>

      <footer class="page-footer">
        <span v-if="lastUpdated">最后更新：{{ lastUpdated.toLocaleTimeString('zh-CN') }}</span>
        <span v-if="tokenSaved">🔒 Bearer 鉴权已开启</span>
      </footer>
    </main>

    <!-- 审计详情抽屉 -->
    <transition name="drawer-fade">
      <div v-if="auditDetailOpen" class="audit-mask" @click.self="closeAuditDetail">
        <div class="audit-drawer" role="dialog" aria-label="审计详情">
          <button class="audit-close" @click="closeAuditDetail">×</button>

          <div v-if="auditDetailLoading" class="empty">加载详情中…</div>
          <div v-else-if="!auditDetail" class="empty">无法获取详情</div>
          <div v-else class="audit-drawer-body">
            <!-- 左半：照片 -->
            <div class="audit-photo-pane">
              <img
                v-if="auditDetail.has_photo && auditDetail.photo_inline_b64"
                :src="`data:image/jpeg;base64,${auditDetail.photo_inline_b64}`"
                :alt="`订单 ${auditDetail.order_id} 抓拍`"
              />
              <img
                v-else-if="auditDetail.has_photo"
                :src="photoSrc(auditDetail)"
                :alt="`订单 ${auditDetail.order_id} 抓拍`"
              />
              <div v-else class="audit-photo-empty">
                <strong>该订单没有真实抓拍</strong>
                <em>识别走的是 demo 场景或视觉模型调用失败，看不到当时画面</em>
              </div>
            </div>

            <!-- 右半：清单 + 操作 -->
            <div class="audit-info-pane">
              <header class="audit-info-head">
                <h3>{{ auditDetail.order_id }}</h3>
                <span class="audit-mode-pill" :class="{ demo: auditDetail.recognize_mode.startsWith('demo') }">
                  {{ auditDetail.recognize_mode }}
                </span>
              </header>
              <div class="audit-info-grid">
                <div><span class="lbl">支付方式</span><strong>{{ auditDetail.pay_method || '-' }}</strong></div>
                <div><span class="lbl">支付时间</span><strong>{{ fmtTime(auditDetail.created_at) }}</strong></div>
                <div><span class="lbl">商品金额</span><strong>{{ yuan(auditDetail.bill_origin) }}</strong></div>
                <div><span class="lbl">优惠</span><strong>-{{ yuan(auditDetail.bill_discount) }}</strong></div>
                <div><span class="lbl">实付</span><strong class="big">{{ yuan(auditDetail.bill_payable) }}</strong></div>
                <div><span class="lbl">商品件数</span><strong>{{ auditDetail.items_count }} 件</strong></div>
              </div>

              <section class="audit-items">
                <h4>购买清单（不可变快照）</h4>
                <div v-if="!auditDetail.items_snapshot.length" class="empty small">该订单没有详细清单记录</div>
                <table v-else class="audit-table">
                  <thead>
                    <tr>
                      <th>SKU</th>
                      <th>商品</th>
                      <th>规格</th>
                      <th class="num">单价</th>
                      <th class="num">数量</th>
                      <th class="num">小计</th>
                      <th>来源</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(row, i) in auditDetail.items_snapshot" :key="i">
                      <td class="mono">{{ row.sku_id }}</td>
                      <td>{{ row.name }}</td>
                      <td class="dim">{{ row.spec }}</td>
                      <td class="num">{{ yuan(row.unit_price) }}</td>
                      <td class="num">×{{ row.quantity }}</td>
                      <td class="num">{{ yuan(row.subtotal) }}</td>
                      <td>
                        <span class="source-pill" :class="row.source">{{ sourceLabel(row.source) }}</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </section>

              <section v-if="auditDetail.cleared" class="audit-cleared-block">
                <h4>已处理</h4>
                <p>
                  <span class="lbl">操作人</span>
                  <strong>{{ auditDetail.cleared_by || '-' }}</strong>
                  <span class="lbl">时间</span>
                  <strong>{{ auditDetail.cleared_at ? fmtTime(auditDetail.cleared_at) : '-' }}</strong>
                </p>
                <p v-if="auditDetail.clear_note" class="audit-cleared-note">
                  {{ auditDetail.clear_note }}
                </p>
              </section>

              <section v-else class="audit-clear-block">
                <h4>标记为已处理</h4>
                <p class="dim small">
                  防损/客诉处理完成后勾选，留证之后审计归档。
                </p>
                <input v-model="clearOperatorInput" placeholder="操作人" maxlength="32" />
                <textarea
                  v-model="clearNoteInput"
                  placeholder="处理记录（可选，如：漏检补 1 件/已退款 ¥xx）"
                  maxlength="500"
                  rows="2"
                />
                <button class="primary" @click="markCleared">标记为已处理</button>
              </section>

              <section class="audit-danger-block">
                <h4>危险操作</h4>
                <p class="dim small">删除会同时清掉照片文件，不可恢复，请确认后操作</p>
                <button class="danger" @click="deleteAudit">永久删除该记录</button>
              </section>
            </div>
          </div>
        </div>
      </div>
    </transition>
  </div>
</template>

<style scoped>
.admin-page {
  min-height: 100vh;
  background: var(--bg);
  color: var(--text);
}

/* ===== 顶栏 ===== */
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 28px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 10;
  box-shadow: var(--shadow-sm);
}
.brand-block { display: flex; align-items: center; gap: 12px; }
.logo {
  width: 36px; height: 36px;
  border-radius: 9px;
  background: linear-gradient(135deg, var(--brand) 0%, #7c3aed 100%);
  color: #fff;
  display: grid; place-items: center;
  font-weight: 700;
}
.brand-text h1 { font-size: 17px; margin: 0; }
.brand-text .tagline { margin: 2px 0 0; font-size: 12px; color: var(--text-3); }

.right-tools { display: flex; align-items: center; gap: 8px; }
.range-group {
  display: flex;
  background: var(--surface-2);
  padding: 3px;
  border-radius: 8px;
  border: 1px solid var(--border);
}
.range-btn {
  border: none; background: transparent; cursor: pointer;
  padding: 5px 10px;
  border-radius: 6px;
  font-size: 12px; color: var(--text-2);
}
.range-btn.active { background: var(--surface); color: var(--text); box-shadow: var(--shadow-sm); }

.tool {
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  padding: 6px 12px;
  border-radius: 8px;
  font-size: 13px;
  cursor: pointer;
  transition: background 0.12s;
}
.tool:hover { background: var(--surface-2); }
.tool.primary { background: var(--brand); color: #fff; border-color: var(--brand); }
.tool.primary:hover { background: var(--brand-dark); }
.tool.primary:disabled { opacity: 0.6; cursor: not-allowed; }
.tool.ghost { background: transparent; }

/* ===== Token 对话框 ===== */
.token-overlay {
  position: fixed; inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: grid; place-items: center;
  z-index: 100;
}
.token-card {
  background: var(--surface);
  padding: 22px 24px;
  border-radius: var(--radius-lg);
  width: 380px;
  box-shadow: var(--shadow-lg);
}
.token-card h3 { margin: 0 0 6px; font-size: 16px; }
.token-card p { margin: 0 0 12px; color: var(--text-2); font-size: 13px; }
.token-card input {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 14px;
  margin-bottom: 14px;
}
.token-card code { background: var(--surface-2); padding: 1px 6px; border-radius: 4px; font-size: 12px; }
.token-actions { display: flex; gap: 8px; justify-content: flex-end; }
.token-actions .primary { background: var(--brand); color: #fff; border: none; padding: 6px 14px; border-radius: 8px; cursor: pointer; }
.token-actions .ghost { background: transparent; border: 1px solid var(--border); padding: 6px 14px; border-radius: 8px; cursor: pointer; }

/* ===== Dashboard ===== */
.dashboard {
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px 28px 80px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

/* 错误条 */
.error-bar {
  background: var(--danger-soft);
  border: 1px solid var(--danger);
  color: var(--danger);
  padding: 10px 14px;
  border-radius: 10px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
}
.error-bar button {
  background: transparent;
  border: 1px solid var(--danger);
  color: var(--danger);
  padding: 3px 10px;
  border-radius: 6px;
  cursor: pointer;
}

/* KPI 行 */
.kpi-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
}
.kpi-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 18px;
  box-shadow: var(--shadow-sm);
}
.kpi-card.primary { background: linear-gradient(135deg, var(--brand-soft), var(--surface)); border-color: var(--brand); }
.kpi-card.skeleton { opacity: 0.5; }
.kpi-label { font-size: 12px; color: var(--text-3); margin-bottom: 6px; }
.kpi-num { font-size: 28px; font-weight: 700; line-height: 1.1; }
.kpi-delta { font-size: 12px; margin-top: 6px; }
.kpi-delta.up { color: var(--success); }
.kpi-delta.down { color: var(--danger); }
.kpi-delta.flat { color: var(--text-3); }

/* 两列：热销 + 库存 */
.row-2 { display: grid; grid-template-columns: 1.4fr 1fr; gap: 14px; }
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 18px 20px;
  box-shadow: var(--shadow-sm);
}
.card-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 14px; }
.card-head h2 { font-size: 15px; margin: 0; }
.card-head .hint { font-size: 12px; color: var(--text-3); }
.empty { color: var(--text-3); padding: 32px 0; text-align: center; font-size: 13px; }
.empty.all-ok { color: var(--success); }

/* 热销柱图 */
.bars { display: flex; flex-direction: column; gap: 6px; }
.bar-row {
  display: grid;
  grid-template-columns: 24px 1fr 90px 40px 60px;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.rank { color: var(--text-3); text-align: center; font-weight: 600; }
.bar-name {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.bar-track {
  height: 14px;
  background: var(--surface-2);
  border-radius: 4px;
  overflow: hidden;
  border: 1px solid var(--border);
}
.bar-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--brand) 0%, #60a5fa 100%);
  border-radius: 4px;
}
.bar-qty { text-align: right; color: var(--text-2); font-weight: 600; }
.bar-rev { text-align: right; color: var(--text-3); }

/* 库存预警双列 */
.alerts { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.alert-col { background: var(--surface-2); border-radius: 10px; padding: 12px; }
.alert-col.sold { border-left: 3px solid var(--danger); }
.alert-col.low { border-left: 3px solid var(--warn); }
.col-head { font-size: 12px; font-weight: 600; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.sold-dot { background: var(--danger); }
.low-dot { background: var(--warn); }
.alert-row { padding: 6px 0; border-top: 1px dashed var(--border); }
.alert-row:first-of-type { border-top: none; }
.alert-name { font-size: 13px; font-weight: 500; }
.alert-meta { font-size: 11px; color: var(--text-3); margin-top: 2px; }

/* 销售趋势 */
.trend-total { font-size: 12px; color: var(--text-2); }
.trend-total strong { color: var(--text); font-size: 14px; }
.chart-wrap { display: flex; flex-direction: column; gap: 6px; }
.trend-chart { width: 100%; height: 200px; }
.trend-chart .grid line { stroke: var(--border); stroke-dasharray: 3 3; stroke-width: 1; }
.x-axis { display: flex; justify-content: space-between; font-size: 10px; color: var(--text-3); padding: 0 24px; }
.x-tick { display: flex; flex-direction: column; align-items: center; gap: 2px; }
.ord-pill {
  background: var(--surface-2);
  color: var(--text-2);
  padding: 1px 5px;
  border-radius: 8px;
  font-size: 9px;
}

/* 最近对话 */
.chat-list { display: flex; flex-direction: column; gap: 10px; }
.chat-card {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 14px;
  background: var(--surface-2);
}
.chat-head { margin-bottom: 8px; }
.chat-meta { display: flex; gap: 8px; align-items: center; font-size: 11px; }
.intent-pill, .mode-pill {
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 500;
}
.intent-pill { background: var(--brand-soft); color: var(--brand); }
.intent-pill.pairing { background: #fef3c7; color: #92400e; }
.intent-pill.stock { background: #d1fae5; color: #065f46; }
.intent-pill.nutrition { background: #ddd6fe; color: #5b21b6; }
.intent-pill.location { background: #fce7f3; color: #9d174d; }
.intent-pill.promotion { background: #fed7aa; color: #9a3412; }
.intent-pill.price { background: #e0e7ff; color: #3730a3; }
.intent-pill.allergen { background: var(--danger-soft); color: var(--danger); }
.mode-pill { background: var(--surface); color: var(--text-3); border: 1px solid var(--border); }
.time { color: var(--text-3); }
.chat-q, .chat-a { font-size: 13px; line-height: 1.5; margin-top: 4px; }
.chat-q { color: var(--text); font-weight: 500; }
.chat-a { color: var(--text-2); white-space: pre-wrap; }
.chat-cites { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px; }
.cite-chip {
  font-size: 11px;
  padding: 2px 8px;
  background: var(--brand-soft);
  color: var(--brand);
  border-radius: 10px;
}

.page-footer {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-3);
  padding-top: 12px;
  border-top: 1px solid var(--border);
}

/* 响应式：平板 */
@media (max-width: 980px) {
  .kpi-row { grid-template-columns: repeat(2, 1fr); }
  .row-2 { grid-template-columns: 1fr; }
}
/* 响应式：手机 */
@media (max-width: 600px) {
  .topbar { padding: 10px 14px; flex-wrap: wrap; gap: 10px; }
  .right-tools { flex-wrap: wrap; }
  .dashboard { padding: 14px; }
  .kpi-row { grid-template-columns: 1fr 1fr; gap: 8px; }
  .kpi-num { font-size: 22px; }
  .alerts { grid-template-columns: 1fr; }
  .bar-row { grid-template-columns: 20px 1fr 80px 30px 50px; font-size: 11px; }
}

/* ========== 第六区块：审计追溯 ========== */
.audit-card { gap: 14px; }
.audit-head { flex-wrap: wrap; gap: 12px; }
.audit-head-left { flex: 1 1 auto; }
.audit-head-tools {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.audit-q {
  width: 220px;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  font-size: 12px;
  background: var(--surface);
  color: var(--text);
}
.audit-q:focus { outline: none; border-color: var(--brand); }
.audit-filter {
  display: flex;
  background: var(--surface-2);
  padding: 3px;
  border-radius: 8px;
  border: 1px solid var(--border);
}
.audit-filter button {
  border: none;
  background: transparent;
  padding: 5px 12px;
  border-radius: 6px;
  font-size: 12px;
  color: var(--text-2);
  cursor: pointer;
}
.audit-filter button.active {
  background: var(--surface);
  color: var(--text);
  box-shadow: var(--shadow-sm);
}

.audit-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}
.audit-tile {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface-2);
  overflow: hidden;
  cursor: pointer;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
  display: flex;
  flex-direction: column;
}
.audit-tile:hover { transform: translateY(-2px); box-shadow: var(--shadow); }
.audit-tile.cleared { opacity: 0.62; }
.audit-tile.nodata .audit-thumb-empty {
  background: linear-gradient(135deg, var(--surface), var(--surface-2));
  display: grid;
  place-items: center;
  color: var(--text-3);
}
.audit-tile.nodata .audit-thumb-empty span { font-size: 13px; font-weight: 500; }
.audit-tile.nodata .audit-thumb-empty em { font-size: 11px; font-style: normal; color: var(--text-3); }
.audit-thumb {
  position: relative;
  width: 100%;
  aspect-ratio: 4/3;
  background: var(--surface);
  overflow: hidden;
}
.audit-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.audit-cleared-pin {
  position: absolute;
  top: 6px;
  right: 6px;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--success);
  color: #fff;
  font-size: 10px;
  font-weight: 500;
}
.audit-meta {
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.audit-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.audit-sub {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-3);
}
.audit-amount {
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
  margin-top: 2px;
}
.audit-tile.cleared .audit-amount { color: var(--text-3); }
.audit-more {
  margin-top: 8px;
  font-size: 11px;
  color: var(--text-3);
  text-align: center;
}

/* ---------- 抽屉 ---------- */
.audit-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.42);
  display: grid;
  place-items: stretch;
  z-index: 30;
}
.audit-drawer {
  position: relative;
  width: min(1080px, 96vw);
  height: min(720px, 92vh);
  margin: auto;
  background: var(--surface);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.audit-close {
  position: absolute;
  top: 12px;
  right: 16px;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  border: none;
  background: var(--surface-2);
  cursor: pointer;
  font-size: 20px;
  line-height: 1;
  color: var(--text-2);
  z-index: 2;
}
.audit-close:hover { background: var(--danger-soft); color: var(--danger); }
.audit-drawer-body {
  display: grid;
  grid-template-columns: 1fr 1.05fr;
  flex: 1;
  min-height: 0;
}
.audit-photo-pane {
  background: #0f172a;
  display: grid;
  place-items: center;
  padding: 16px;
  min-height: 0;
}
.audit-photo-pane img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  border-radius: var(--radius);
  background: #000;
}
.audit-photo-empty {
  text-align: center;
  color: #94a3b8;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.audit-photo-empty strong { color: #cbd5e1; font-size: 14px; }
.audit-photo-empty em { font-style: normal; font-size: 11px; }

.audit-info-pane {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  overflow-y: auto;
  min-height: 0;
}
.audit-info-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.audit-info-head h3 {
  margin: 0;
  font-size: 14px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.audit-mode-pill {
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--brand-soft);
  color: var(--brand-dark);
  font-size: 11px;
}
.audit-mode-pill.demo { background: #f1f5f9; color: #64748b; }

.audit-info-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px 12px;
  font-size: 12px;
}
.audit-info-grid .lbl { color: var(--text-3); margin-right: 6px; font-size: 11px; }
.audit-info-grid strong { font-size: 13px; }
.audit-info-grid .big { font-size: 18px; color: var(--danger); }

.audit-items { display: flex; flex-direction: column; gap: 6px; }
.audit-items h4,
.audit-clear-block h4,
.audit-cleared-block h4,
.audit-danger-block h4 {
  margin: 0 0 4px;
  font-size: 12px;
  color: var(--text-2);
}
.audit-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.audit-table th,
.audit-table td {
  padding: 6px 8px;
  text-align: left;
  border-bottom: 1px solid var(--border);
}
.audit-table th { background: var(--surface-2); color: var(--text-3); font-weight: 500; }
.audit-table td.num,
.audit-table th.num { text-align: right; }
.audit-table td.mono { font-family: ui-monospace, SFMono-Regular, monospace; font-size: 11px; color: var(--text-3); }
.audit-table td.dim { color: var(--text-3); font-size: 11px; }

.source-pill {
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 10px;
}
.source-pill.auto { background: var(--brand-soft); color: var(--brand-dark); }
.source-pill.clarify { background: #fef3c7; color: #92400e; }
.source-pill.manual { background: var(--danger-soft); color: var(--danger); }

.audit-clear-block,
.audit-cleared-block,
.audit-danger-block {
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  border: 1px solid var(--border);
}
.audit-clear-block input,
.audit-clear-block textarea {
  width: 100%;
  padding: 6px 8px;
  border-radius: 6px;
  border: 1px solid var(--border);
  font-size: 12px;
  margin-top: 4px;
  font-family: inherit;
  background: var(--surface);
  color: var(--text);
  resize: vertical;
}
.audit-clear-block input:focus,
.audit-clear-block textarea:focus { outline: none; border-color: var(--brand); }
.audit-clear-block button.primary,
.audit-danger-block button.danger {
  margin-top: 8px;
  padding: 7px 14px;
  border-radius: 6px;
  border: none;
  cursor: pointer;
  font-size: 12px;
}
.audit-clear-block button.primary {
  background: var(--brand);
  color: #fff;
}
.audit-clear-block button.primary:hover { filter: brightness(1.05); }
.audit-danger-block button.danger {
  background: var(--danger);
  color: #fff;
}
.audit-danger-block button.danger:hover { filter: brightness(1.05); }
.audit-cleared-block p { margin: 4px 0; font-size: 12px; }
.audit-cleared-block .lbl { margin-right: 6px; }
.audit-cleared-note {
  background: var(--success-soft);
  padding: 6px 10px;
  border-radius: 6px;
  color: #065f46;
  font-size: 12px;
}

.empty.small { padding: 16px; font-size: 12px; }

/* 抽屉淡入淡出 */
.drawer-fade-enter-active,
.drawer-fade-leave-active { transition: opacity 0.18s ease; }
.drawer-fade-enter-from,
.drawer-fade-leave-to { opacity: 0; }

@media (max-width: 980px) {
  .audit-drawer-body { grid-template-columns: 1fr; }
  .audit-photo-pane { max-height: 40vh; }
  .audit-info-grid { grid-template-columns: 1fr; }
}
</style>
