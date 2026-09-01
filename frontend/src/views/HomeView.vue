<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import ProductVisual from '@/components/ProductVisual.vue'
import { api } from '@/api'
import type { Product } from '@/types'

const router = useRouter()

const products = ref<Product[]>([])
const keyword = ref('')
const activeCategory = ref('全部')
const loaded = ref(false)
const loadError = ref('')

/** 用户浏览时勾选的商品清单。每项可附数量和备注。
 *   - qty: 数量（默认 1），用于「买几件」、「是否够 5 人喝」这种问询
 *   - note: 备注（如「冰箱凉一些」「买给小孩」），AI 会读进 prompt 里
 * 结构形态：[{ sku, qty, note }] */
interface PickedItem {
  sku: string
  qty: number
  note: string
}

const picked = ref<PickedItem[]>([])

/** 从商品列表中提取分类 */
const categories = computed(() => {
  const set = new Set<string>()
  for (const p of products.value) if (p.category) set.add(p.category)
  return ['全部', ...Array.from(set)]
})

/** 经过关键词 + 分类筛选后的商品 */
const filtered = computed(() => {
  let list = products.value
  if (activeCategory.value === '优惠') {
    list = list.filter((p) => (p.promotions?.length ?? 0) > 0)
  } else if (activeCategory.value !== '全部') {
    list = list.filter((p) => p.category === activeCategory.value)
  }
  const kw = keyword.value.trim().toLowerCase()
  if (kw) {
    list = list.filter((p) => {
      const blob = `${p.name} ${p.brand} ${p.category} ${p.spec}`.toLowerCase()
      return blob.includes(kw)
    })
  }
  return list
})

/** 库存三态：充足（>5绿）/ 紧张（1-5黄）/ 售罄（0红） */
function stockState(p: Product): 'ok' | 'low' | 'sold' {
  if (p.stock <= 0) return 'sold'
  if (p.stock <= 5) return 'low'
  return 'ok'
}
function stockLabel(p: Product) {
  const s = stockState(p)
  if (s === 'sold') return '已售罄'
  if (s === 'low') return `仅剩 ${p.stock}`
  return '有货'
}

/** 商品卡：跳到导购并自动触发「介绍 + 搭配」开场白 */
function openProduct(p: Product) {
  // 售罄商品不跳，引导顾客问「还有别的吗」
  if (p.stock <= 0) {
    router.push({ path: '/guide', query: { sku: p.sku_id } })
    return
  }
  router.push({ path: '/guide', query: { sku: p.sku_id } })
}

/** 直接进入导购主页 */
function openGuide(p?: Product) {
  if (p) {
    router.push({ path: '/guide', query: { sku: p.sku_id } })
  } else {
    router.push('/guide')
  }
}

/** 进入视觉结账台 */
function openCheckout() {
  router.push('/checkout')
}

/** 商品卡的优惠徽章文案：第一个促销的 desc，超过 6 字截断 */
function promoText(p: Product): string | null {
  const desc = p.promotions?.[0]?.desc
  if (!desc) return null
  return desc.length > 6 ? desc.slice(0, 6) + '…' : desc
}

/* ===== 购物清单（quick-add） =====
 * 每张商品卡右上角 +/- 切换是否入选；数量与备注在底部抽屉里编辑。
 * 设计取舍：
 *   - 数量：默认 1，步进 ±1，最小 1（不能为零被误删用 ×）
 *   - 备注：单行文本（用户截图希望轻量），按 sku 维度独立保存
 *   - 对比 / 推荐：prompt 同时携带商品名 + 数量 + 备注，避免出现「只带名字不带数量」
 *     导致 LLM 又一次问"买几件"的来回 */
function isPicked(sku: string) {
  return picked.value.some((item) => item.sku === sku)
}
function getPicked(sku: string): PickedItem | undefined {
  return picked.value.find((item) => item.sku === sku)
}
function togglePick(p: Product, ev: Event) {
  ev.stopPropagation()
  const sku = p.sku_id
  const existing = getPicked(sku)
  if (existing) {
    picked.value = picked.value.filter((item) => item.sku !== sku)
  } else {
    picked.value = [...picked.value, { sku, qty: 1, note: '' }]
  }
}
function changeQty(sku: string, delta: number) {
  picked.value = picked.value.map((item) => {
    if (item.sku !== sku) return item
    const qty = Math.max(1, item.qty + delta)
    return { ...item, qty }
  })
}
function setNote(sku: string, note: string) {
  picked.value = picked.value.map((item) =>
    item.sku === sku ? { ...item, note } : item,
  )
}
const pickedProducts = computed(() =>
  picked.value
    .map((item) => {
      const p = products.value.find((pp) => pp.sku_id === item.sku)
      return p ? { product: p, qty: item.qty, note: item.note } : null
    })
    .filter((x): x is { product: Product; qty: number; note: string } => x !== null),
)
function clearList() {
  picked.value = []
}
function removePicked(sku: string, ev: Event) {
  ev.stopPropagation()
  picked.value = picked.value.filter((item) => item.sku !== sku)
}

/** 把清单一次性提交到导购：问「对比 / 哪个更值 / 搭配」。
 *
 * prompt 形态（保持字段顺序稳定，方便后续正则解析）：
 *   我从店里挑了这几样：
 *   - 可口可乐 汽水 ×2（备注：冷藏后更好喝）
 *   - 百事可乐 汽水 ×3
 *   ...
 *   （问句）
 *
 * 用「- 」开头 + 名字 + 「×数量」+ 备注（括号），
 * 既保证不丢任何字段、也不会让 LLM 把零散的「×2」当成未知变量再发问。 */
function formatListItems(): string {
  return pickedProducts.value
    .map((it) => {
      const tag = it.product.name
      const qty = it.qty && it.qty > 1 ? ` ×${it.qty}` : ''
      const note = it.note?.trim()
      return `- ${tag}${qty}${note ? `（备注：${note}）` : ''}`
    })
    .join('\n')
}

function askListAboutList(mode: 'compare' | 'recommend') {
  const items = pickedProducts.value
  if (!items.length) return
  const listText = formatListItems()
  const prompt =
    mode === 'compare'
      ? `我从店里挑了这几样：\n${listText}\n\n请帮我对比它们的营养、口味和价格（按我的购买数量估算总价），告诉我哪几样最适合我。`
      : `我从店里挑了这几样：\n${listText}\n\n还差什么可以搭配？给一个完整组合建议。`
  router.push({ path: '/guide', query: { q: prompt } })
}

onMounted(async () => {
  try {
    products.value = await api.products()
    loaded.value = true
  } catch (e) {
    loadError.value = (e as Error)?.message || '商品数据加载失败'
  }
})
</script>

<template>
  <div class="home">
    <!-- 顶部欢迎卡 + 一键进入能力 -->
    <section class="hero">
      <div class="hero-text">
        <h2>智选无人超市</h2>
        <p class="dim">线下 28 款精选商品 · 点商品卡片让 AI 帮你介绍和搭配</p>
      </div>
      <div class="hero-actions">
        <button class="hero-btn primary" @click="openGuide()">
          <span class="badge">A</span>
          智能导购
        </button>
        <button class="hero-btn ghost checkout-only" @click="openCheckout()">
          <span class="badge ghost-badge">B</span>
          视觉结账
        </button>
      </div>
    </section>

    <!-- 搜索框 -->
    <section class="searchbar">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="11" cy="11" r="7" />
        <path d="M21 21l-4.3-4.3" />
      </svg>
      <input
        v-model="keyword"
        placeholder="搜商品、品牌、关键词（如「可乐」「薯片」）"
      />
      <button v-if="keyword" class="clear" @click="keyword = ''" aria-label="清空">×</button>
    </section>

    <!-- 分类横滑 -->
    <nav class="cats">
      <button
        v-for="c in categories"
        :key="c"
        class="cat"
        :class="{ active: activeCategory === c }"
        @click="activeCategory = c"
      >
        {{ c }}
      </button>
      <button
        class="cat promo"
        :class="{ active: activeCategory === '优惠' }"
        @click="activeCategory = '优惠'"
      >
        🔥 优惠
      </button>
    </nav>

    <!-- 错误兜底 -->
    <div v-if="loadError" class="state error">
      商品加载失败：{{ loadError }}
    </div>

    <!-- 空结果 -->
    <div v-else-if="loaded && filtered.length === 0" class="state muted">
      没找到匹配的商品，换个关键词试试？
    </div>

    <!-- 商品网格 -->
    <section v-else class="grid">
      <article
        v-for="p in filtered"
        :key="p.sku_id"
        class="card"
        :class="{ 'sold-out': p.stock <= 0 }"
        @click="openProduct(p)"
      >
        <div class="visual">
          <ProductVisual :visual="p.visual" :size="92" />
          <span
            v-if="promoText(p)"
            class="discount-badge"
          >
            {{ promoText(p) }}
          </span>
          <button
            class="pick-btn"
            :class="{ on: isPicked(p.sku_id) }"
            :title="isPicked(p.sku_id) ? '从清单移除' : '加入清单'"
            @click="(e) => togglePick(p, e)"
          >
            <svg v-if="!isPicked(p.sku_id)" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 5v14M5 12h14" />
            </svg>
            <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
              <path d="M5 12l5 5 9-12" />
            </svg>
          </button>
        </div>

        <div class="body">
          <div class="name" :title="p.name">{{ p.name }}</div>
          <div class="meta dim">{{ p.brand }} · {{ p.spec }}</div>
          <div class="price-row">
            <span class="price">¥{{ (p.member_price ?? p.price).toFixed(2) }}</span>
            <span v-if="p.member_price && p.member_price < p.price" class="strike">¥{{ p.price.toFixed(2) }}</span>
          </div>
          <div class="row-foot">
            <span
              class="stock-chip"
              :class="stockState(p)"
            >
              <span class="dot" />
              {{ stockLabel(p) }}
            </span>
            <span class="shelf" v-if="p.shelf">📍 {{ p.shelf.aisle }}{{ p.shelf.level }}层</span>
          </div>
        </div>
      </article>
    </section>

    <!-- 选中清单的底部抽屉 -->
    <transition name="listbar">
      <div v-if="pickedProducts.length" class="listbar">
        <div class="listbar-head">
          <div class="listbar-title">
            <strong>已选 {{ pickedProducts.length }} 件</strong>
            <span class="dim">改数量 / 加备注后点下方按钮提问</span>
          </div>
          <button class="ghost clear" @click="clearList">清空</button>
        </div>

        <div class="listbar-rows">
          <div
            v-for="it in pickedProducts"
            :key="it.product.sku_id"
            class="listrow"
          >
            <button
              class="listrow-thumb"
              :title="it.product.name"
              @click="openProduct(it.product)"
            >
              <ProductVisual :visual="it.product.visual" :size="38" :show-label="false" />
            </button>

            <div class="listrow-body">
              <div class="listrow-line1">
                <span class="listrow-name" :title="it.product.name">{{ it.product.name }}</span>
                <span class="listrow-qty">
                  <button
                    class="qty-btn"
                    :disabled="it.qty <= 1"
                    :title="it.qty <= 1 ? '至少为 1，如不要点 × 删除' : '数量 -1'"
                    @click="changeQty(it.product.sku_id, -1)"
                  >−</button>
                  <span class="qty-num">×{{ it.qty }}</span>
                  <button
                    class="qty-btn"
                    title="数量 +1"
                    @click="changeQty(it.product.sku_id, 1)"
                  >+</button>
                </span>
              </div>
              <input
                class="listrow-note"
                placeholder="备注（可选）：冷藏后更好喝 / 买给小孩"
                :value="it.note"
                @input="(e) => setNote(it.product.sku_id, (e.target as HTMLInputElement).value)"
              />
            </div>

            <button class="listrow-x" :title="`移除「${it.product.name}」`" @click="(e) => removePicked(it.product.sku_id, e)">
              ×
            </button>
          </div>
        </div>

        <div class="listbar-actions">
          <button class="primary" @click="askListAboutList('compare')">
            <span class="badge">A</span>
            帮我对比这几样
          </button>
          <button class="ghost-btn" @click="askListAboutList('recommend')">
            <span class="badge ghost-badge">+</span>
            还差什么搭配？
          </button>
        </div>
      </div>
    </transition>
  </div>
</template>

<style scoped>
.home {
  max-width: 1200px;
  margin: 0 auto;
  padding: 16px 16px 32px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* ---------- 顶部欢迎 ---------- */
.hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 20px;
  background: linear-gradient(135deg, #eff6ff 0%, #f0f9ff 100%);
  border-radius: 16px;
  border: 1px solid var(--border);
}
.hero-text h2 {
  margin: 0;
  font-size: 18px;
}
.hero-text p {
  margin: 4px 0 0;
  font-size: 12px;
}
.hero-actions {
  display: flex;
  gap: 8px;
  flex: none;
}
.hero-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 9px 14px;
  border-radius: 10px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
  border: 1px solid var(--brand);
}
.hero-btn.primary {
  background: var(--brand);
  color: #fff;
  border-color: var(--brand);
}
.hero-btn.primary:hover { background: var(--brand-dark); border-color: var(--brand-dark); }
.hero-btn.ghost {
  background: var(--surface);
  color: var(--text);
  border-color: var(--border-strong);
}
.hero-btn.ghost:hover { background: var(--surface-2); }
.hero-btn .badge {
  width: 18px;
  height: 18px;
  border-radius: 5px;
  display: grid;
  place-items: center;
  font-size: 11px;
  background: rgba(255,255,255,0.25);
  color: inherit;
}
.hero-btn .ghost-badge {
  background: var(--brand-soft);
  color: var(--brand-dark);
}

/* ---------- 搜索栏 ---------- */
.searchbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 14px;
  height: 42px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  color: var(--text-3);
}
.searchbar input {
  flex: 1;
  border: none;
  background: transparent;
  outline: none;
  font-size: 14px;
  color: var(--text);
}
.searchbar input::placeholder { color: var(--text-3); }
.searchbar .clear {
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 50%;
  background: var(--surface-2);
  color: var(--text-3);
  font-size: 16px;
  cursor: pointer;
  line-height: 1;
}

/* ---------- 分类横滑 ---------- */
.cats {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding: 4px 0 4px 0;
  scrollbar-width: none;
}
.cats::-webkit-scrollbar { display: none; }
.cat {
  flex: none;
  padding: 6px 14px;
  border-radius: 999px;
  font-size: 13px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-2);
  cursor: pointer;
  transition: all 0.15s ease;
  white-space: nowrap;
}
.cat:hover { color: var(--text); }
.cat.active {
  background: var(--brand);
  color: #fff;
  border-color: var(--brand);
}
.cat.promo:not(.active) {
  color: var(--danger);
  border-color: var(--danger-soft);
  background: var(--danger-soft);
}
.cat.promo.active {
  background: var(--danger);
  border-color: var(--danger);
}

/* ---------- 商品网格 ---------- */
.grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}
@media (max-width: 700px) {
  .grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 380px) {
  .grid { grid-template-columns: 1fr; }
}

.card {
  display: flex;
  flex-direction: column;
  background: var(--surface);
  border-radius: 14px;
  border: 1px solid var(--border);
  cursor: pointer;
  overflow: hidden;
  transition: all 0.15s ease;
  user-select: none;
}
.card:hover {
  border-color: var(--brand);
  box-shadow: var(--shadow);
  transform: translateY(-1px);
}
.card.sold-out { opacity: 0.62; }

.visual {
  position: relative;
  display: grid;
  place-items: center;
  padding: 18px 12px 8px;
  background: linear-gradient(180deg, var(--surface-2) 0%, var(--surface) 100%);
}
.discount-badge {
  position: absolute;
  top: 8px;
  left: 8px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  color: #fff;
  background: var(--danger);
}

/* 卡片右上角的快速加入清单按钮 */
.pick-btn {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  border: 1px solid var(--border);
  background: rgba(255, 255, 255, 0.92);
  color: var(--text-2);
  cursor: pointer;
  transition: all 0.15s ease;
  -webkit-backdrop-filter: blur(4px);
  backdrop-filter: blur(4px);
}
.pick-btn:hover { color: var(--brand); border-color: var(--brand); }
.pick-btn.on {
  background: var(--brand);
  border-color: var(--brand);
  color: #fff;
}

.body {
  padding: 8px 12px 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.name {
  font-size: 13px;
  line-height: 1.35;
  font-weight: 500;
  color: var(--text);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 36px;
}
.meta { font-size: 11px; margin-top: 2px; }
.price-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-top: 2px;
}
.price {
  color: var(--danger);
  font-size: 16px;
  font-weight: 700;
}
.strike {
  font-size: 11px;
  text-decoration: line-through;
  color: var(--text-3);
}

.row-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 4px;
  gap: 6px;
}
.stock-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 7px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
}
.stock-chip .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}
.stock-chip.ok { background: var(--success-soft); color: var(--success); }
.stock-chip.ok .dot { background: var(--success); }
.stock-chip.low { background: var(--warn-soft); color: #b45309; }
.stock-chip.low .dot { background: var(--warn); }
.stock-chip.sold { background: var(--danger-soft); color: var(--danger); }
.stock-chip.sold .dot { background: var(--danger); }

.shelf { font-size: 10px; color: var(--text-3); }

.state {
  text-align: center;
  padding: 40px 16px;
  border-radius: 14px;
  font-size: 13px;
}
.state.error { background: var(--danger-soft); color: var(--danger); }
.state.muted { background: var(--surface-2); color: var(--text-3); }

/* ---------- 手机端适配 ---------- */
@media (max-width: 700px) {
  .home { padding: 12px 12px 96px; }  /* 底部留空给 listbar */
  .hero {
    padding: 14px 16px;
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }
  .hero-actions { width: 100%; }
  .hero-btn { flex: 1; justify-content: center; }
  .hero-text h2 { font-size: 16px; }
  .pick-btn { width: 30px; height: 30px; }  /* 手机触屏稍放大 */
  /* 手机端不暴露视觉结账（属于门店横屏硬件），Hero 只保留「智能导购」入口 */
  .hero-btn.checkout-only { display: none; }
  .hero-actions { width: auto; }
}

/* ---------- 选中清单底部抽屉 ---------- */
.listbar {
  position: fixed;
  left: 12px;
  right: 12px;
  bottom: 12px;
  z-index: 30;
  padding: 12px 14px;
  background: var(--surface);
  border-radius: 16px;
  border: 1px solid var(--border);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: 760px;
  margin: 0 auto;
}
.listbar-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.listbar-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.listbar-title strong { font-size: 14px; }
.listbar-title .dim { font-size: 11px; }
.listbar .clear {
  border: none;
  background: var(--surface-2);
  color: var(--text-2);
  border-radius: 999px;
  padding: 3px 10px;
  font-size: 11px;
  cursor: pointer;
}
.listbar .clear:hover { color: var(--danger); }

/* 底部抽屉里每一条目：缩略图 + 名称 + 数量步进 + 备注输入 + 删除 */
.listbar-rows {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 42vh;
  overflow-y: auto;
  padding-right: 2px;
  scrollbar-width: thin;
}

.listbar-rows::-webkit-scrollbar { width: 6px; }
.listbar-rows::-webkit-scrollbar-thumb {
  background: var(--border-strong);
  border-radius: 3px;
}

.listrow {
  display: grid;
  grid-template-columns: 44px 1fr 28px;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  background: var(--surface-2);
  border-radius: 10px;
}

.listrow-thumb {
  border: none;
  background: transparent;
  padding: 0;
  cursor: pointer;
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  border-radius: 8px;
  transition: background 0.12s ease;
}
.listrow-thumb:hover { background: var(--brand-soft); }

.listrow-body {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
}

.listrow-line1 {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.listrow-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  flex: 1;
}

.listrow-qty {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: none;
}
.qty-btn {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  font-size: 13px;
  line-height: 1;
  display: grid;
  place-items: center;
  cursor: pointer;
  padding: 0;
  transition: all 0.12s ease;
}
.qty-btn:hover:not(:disabled) {
  border-color: var(--brand);
  color: var(--brand);
  background: var(--brand-soft);
}
.qty-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.qty-num {
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
  min-width: 28px;
  text-align: center;
}

.listrow-note {
  width: 100%;
  height: 28px;
  padding: 0 8px;
  font-size: 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
  outline: none;
  transition: border-color 0.12s ease;
}
.listrow-note::placeholder { color: var(--text-3); }
.listrow-note:focus { border-color: var(--brand); }

.listrow-x {
  width: 26px;
  height: 26px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--text-3);
  font-size: 16px;
  display: grid;
  place-items: center;
  cursor: pointer;
  line-height: 1;
  padding: 0;
}
.listrow-x:hover { background: var(--danger-soft); color: var(--danger); }

.listbar-actions {
  display: flex;
  gap: 8px;
}
.listbar-actions button {
  flex: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 9px 12px;
  border-radius: 10px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
  border: 1px solid transparent;
}
.listbar-actions .primary {
  background: var(--brand);
  color: #fff;
  border-color: var(--brand);
}
.listbar-actions .primary:hover { background: var(--brand-dark); }
.listbar-actions .ghost-btn {
  background: var(--surface);
  color: var(--text);
  border-color: var(--border-strong);
}
.listbar-actions .ghost-btn:hover { background: var(--surface-2); }
.listbar-actions .badge {
  width: 18px;
  height: 18px;
  border-radius: 5px;
  display: grid;
  place-items: center;
  font-size: 11px;
  background: rgba(255,255,255,0.25);
}
.listbar-actions .ghost-badge {
  background: var(--brand-soft);
  color: var(--brand-dark);
}

.listbar-enter-from, .listbar-leave-to {
  opacity: 0;
  transform: translateY(20px);
}
.listbar-enter-active, .listbar-leave-active {
  transition: all 0.2s ease;
}
</style>
