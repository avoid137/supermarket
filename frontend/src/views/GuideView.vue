<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import ProductVisual from '@/components/ProductVisual.vue'
import { api, askAgent } from '@/api'
import type { Citation, Product } from '@/types'

interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  text: string
  citations?: Citation[]
  streaming?: boolean
  mode?: string
  intent?: string
  latency?: number
  notice?: string
}

const QUICK_QUESTIONS = [
  '可乐在哪里',
  '薯片的营养成分',
  '现在有什么优惠',
  '买薯片配什么好',
  '我对花生过敏能吃什么',
]

const INTENT_LABEL: Record<string, string> = {
  location: '位置查询',
  nutrition: '营养成分',
  allergen: '过敏原',
  promotion: '促销活动',
  pairing: '搭配推荐',
  price: '价格查询',
  general: '商品咨询',
}

const messages = ref<ChatMessage[]>([])
const products = ref<Product[]>([])
const input = ref('')
const loading = ref(false)
const useLlm = ref(true)
const chatEl = ref<HTMLElement | null>(null)
const detail = ref<Product | null>(null)
const listening = ref(false)
const speechSupported = ref(false)

// ---------- 扫码 ----------
const scanning = ref(false)
const scanSupported = ref(false)
const scanError = ref<string>('')
const manualSku = ref('')
const submittingManual = ref(false)
const videoEl = ref<HTMLVideoElement | null>(null)
let detector: any = null
let stream: MediaStream | null = null
let scanTimer: number | null = null

let seq = 0
let recognizer: any = null

const productMap = computed(() => {
  const map: Record<string, Product> = {}
  for (const p of products.value) map[p.sku_id] = p
  return map
})

const lastMeta = computed(() => {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    const m = messages.value[i]
    if (m.role === 'assistant' && m.intent) return m
  }
  return null
})

function scrollBottom() {
  nextTick(() => {
    const el = chatEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function pushWelcome() {
  messages.value.push({
    id: ++seq,
    role: 'assistant',
    text: '你好，我是智选无人超市的导购助手。\n可以问我商品位置、营养成分、当前优惠，也可以让我帮你搭配。',
  })
}

function buildHistory() {
  return messages.value
    .filter((m) => !m.streaming && m.text)
    .slice(-6)
    .map((m) => ({ role: m.role === 'user' ? 'user' : 'assistant', content: m.text }))
}

async function send(text: string) {
  const question = text.trim()
  if (!question || loading.value) return

  messages.value.push({ id: ++seq, role: 'user', text: question })
  const reply: ChatMessage = { id: ++seq, role: 'assistant', text: '', streaming: true, citations: [] }
  messages.value.push(reply)
  input.value = ''
  loading.value = true
  scrollBottom()

  try {
    await askAgent(
      { question, history: buildHistory(), use_llm: useLlm.value },
      (event) => {
        if (event.type === 'meta') {
          reply.mode = event.mode
          reply.intent = event.intent
        } else if (event.type === 'citations') {
          reply.citations = event.items
        } else if (event.type === 'delta') {
          reply.text += event.text
          scrollBottom()
        } else if (event.type === 'notice') {
          reply.notice = event.text
        } else if (event.type === 'done') {
          reply.streaming = false
          reply.latency = event.latency_ms
        }
      },
    )
  } catch (error) {
    reply.text = `导购服务暂时不可用：${(error as Error).message}`
    reply.streaming = false
  } finally {
    loading.value = false
    scrollBottom()
  }
}

function toggleSpeech() {
  if (!speechSupported.value) return
  if (listening.value) {
    recognizer?.stop()
    listening.value = false
    return
  }
  listening.value = true
  recognizer.start()
}

function openDetail(skuId: string) {
  const product = productMap.value[skuId]
  if (product) detail.value = product
}

/** 商品「扫码直达」二维码（后端生成 SVG，内容 SMARTMART:SKUxxx） */
function qrUrl(skuId: string) {
  return `/api/v1/catalog/products/${encodeURIComponent(skuId)}/qrcode`
}

/* ===== 扫码：原生 BarcodeDetector + 手动输入 SKU 兜底 =====
   - Chrome / Edge / Android WebView：支持 BarcodeDetector，调用摄像头实时识别
   - Safari / 微信内置 / 旧版浏览器：降级为「手动输入 SKU」对话框
   命中后调 /catalog/sku?barcode=... 拿到商品，跳到 ?sku=SKUxxx，Agent 自动开场白 */
async function openScanner() {
  scanError.value = ''
  // 先看浏览器是否支持
  const Ctor: any = (window as any).BarcodeDetector
  if (Ctor) {
    try {
      detector = new Ctor({
        formats: ['ean_13', 'ean_8', 'code_128', 'code_39', 'qr_code'],
      })
      scanSupported.value = true
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'environment' },
        audio: false,
      })
      scanning.value = true
      await nextTick()
      if (videoEl.value) {
        videoEl.value.srcObject = stream
        await videoEl.value.play()
        tickScan()
      }
      return
    } catch (e) {
      // 摄像头权限拒绝 / 设备无摄像头 → 降级到手动输入
      scanError.value = (e as Error)?.message || '无法访问摄像头'
      cleanupScan()
    }
  }
  // 不支持或失败 → 直接打开手动输入
  scanSupported.value = false
  manualSku.value = ''
  scanning.value = false
}

function tickScan() {
  if (!scanning.value || !videoEl.value || !detector) return
  scanTimer = window.setTimeout(async () => {
    try {
      const codes = await detector.detect(videoEl.value!)
      if (codes && codes.length > 0) {
        const raw = codes[0].rawValue || ''
        if (raw) {
          await onBarcodeHit(raw)
          return
        }
      }
    } catch {
      /* 单帧失败忽略，下一帧重试 */
    }
    tickScan()
  }, 350)
}

async function onBarcodeHit(raw: string) {
  cleanupScan()
  scanning.value = false
  try {
    // 自有协议 SMARTMART:SKUxxx → 直接解析 SKU（本店二维码，不依赖条码库）
    const m = /^SMARTMART:(SKU\d+)$/i.exec(raw.trim())
    if (m) {
      const product = await api.getProduct(m[1])
      await openScannedProduct(product)
      return
    }
    // 普通商品条码（EAN-13 等）→ 查条码库
    const product = await api.lookupByBarcode(raw.trim())
    await openScannedProduct(product)
  } catch (e) {
    scanError.value = `未找到 ${raw}：${(e as Error).message}`
    manualSku.value = ''
    scanning.value = false
  }
}

async function openScannedProduct(product: Product) {
  // 跳到 ?sku=SKUxxx，触发 deep link 自动开场白
  const url = new URL(window.location.href)
  url.searchParams.set('sku', product.sku_id)
  window.history.replaceState({}, '', url.toString())
  // 先把商品详情卡片直接弹出，体验更顺
  detail.value = product
  // 让 Agent 用「看看这个 + 推荐搭配」开场
  await send(`介绍一下 ${product.name}，并推荐搭配`)
}

async function submitManualSku() {
  const sku = manualSku.value.trim().toUpperCase()
  if (!sku) return
  submittingManual.value = true
  try {
    const hit = await api.getProduct(sku)
    await openScannedProduct(hit)
    manualSku.value = ''
  } catch (e) {
    scanError.value = `找不到商品 ${sku}：${(e as Error).message}`
  } finally {
    submittingManual.value = false
  }
}

function cleanupScan() {
  if (scanTimer) {
    clearTimeout(scanTimer)
    scanTimer = null
  }
  if (stream) {
    stream.getTracks().forEach((t) => t.stop())
    stream = null
  }
  if (videoEl.value) videoEl.value.srcObject = null
}

function closeScanner() {
  cleanupScan()
  scanning.value = false
  scanError.value = ''
  manualSku.value = ''
}

/* 处理 deep link：
 *   ?sku=SKU001  → 弹商品详情卡 + 自动开场「介绍 + 推荐搭配」
 *   ?q=<prompt>  → 不弹详情卡，直接向 Agent 提问（用于「已选 N 件」清单批量问询） */
async function handleDeepLink() {
  const params = new URLSearchParams(window.location.search)
  const sku = params.get('sku')?.toUpperCase()
  const q = params.get('q')
  if (sku) {
    const hit = products.value.find((p) => p.sku_id === sku)
    if (hit) {
      detail.value = hit
      await send(`介绍一下 ${hit.name}，并推荐搭配`)
    }
    return
  }
  if (q && q.trim()) {
    // 短暂停顿让欢迎语先渲染，再发出提问
    await new Promise((r) => setTimeout(r, 200))
    await send(decodeURIComponent(q))
  }
}

onMounted(async () => {
  pushWelcome()
  try {
    products.value = await api.products()
  } catch {
    products.value = []
  }

  const Ctor = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
  if (Ctor) {
    speechSupported.value = true
    recognizer = new Ctor()
    recognizer.lang = 'zh-CN'
    recognizer.interimResults = false
    recognizer.onresult = (event: any) => {
      const text = event.results?.[0]?.[0]?.transcript ?? ''
      if (text) {
        input.value = text
        send(text)
      }
    }
    recognizer.onend = () => {
      listening.value = false
    }
  }

  // deep link: ?sku=SKUxxx 由扫码或外链进入，自动开场白
  handleDeepLink()
})

onBeforeUnmount(() => {
  cleanupScan()
})
</script>

<template>
  <div class="page">
    <section class="stage">
      <div class="phone">
        <div class="statusbar">
          <span>9:41</span>
          <span class="notch" />
          <span>智选 · 中山路店</span>
        </div>

        <header class="shop">
          <div class="shop-avatar">智</div>
          <div class="shop-info">
            <strong>智选无人超市</strong>
            <span class="dim">A1 饮料 · A2 零食 · A3 日用</span>
          </div>
          <button class="scan-btn" :title="scanSupported ? '扫一扫商品条码' : '扫一扫 / 手动输入 SKU'" @click="openScanner">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 7V5a2 2 0 0 1 2-2h2" />
              <path d="M17 3h2a2 2 0 0 1 2 2v2" />
              <path d="M21 17v2a2 2 0 0 1-2 2h-2" />
              <path d="M7 21H5a2 2 0 0 1-2-2v-2" />
              <path d="M7 8v8M10 8v8M13 8v8M16 8v8" />
            </svg>
            <span>扫一扫</span>
          </button>
          <span class="tag success">会员已登录</span>
        </header>

        <div ref="chatEl" class="chat">
          <div
            v-for="m in messages"
            :key="m.id"
            class="row"
            :class="m.role"
          >
            <div v-if="m.role === 'assistant'" class="avatar">智</div>
            <div class="bubble">
              <span v-if="m.role === 'assistant' && m.intent" class="tag brand intent">
                {{ INTENT_LABEL[m.intent] || m.intent }}
              </span>
              <div class="text">{{ m.text }}<span v-if="m.streaming" class="caret" /></div>

              <div v-if="m.citations?.length" class="cards">
                <button
                  v-for="c in m.citations"
                  :key="c.sku_id"
                  class="pcard"
                  @click="openDetail(c.sku_id)"
                >
                  <ProductVisual
                    v-if="productMap[c.sku_id]"
                    :visual="productMap[c.sku_id].visual"
                    :size="34"
                    :show-label="false"
                  />
                  <span class="pcard-main">
                    <strong>{{ c.name }}</strong>
                    <em class="dim">¥{{ productMap[c.sku_id]?.member_price ?? '--' }} · {{ c.reason }}</em>
                  </span>
                </button>
              </div>

              <div v-if="m.notice" class="notice">{{ m.notice }}</div>
              <div v-if="m.latency" class="dim latency">
                {{ m.mode === 'llm' ? '大模型生成' : '本地知识库' }} · {{ m.latency }}ms
              </div>
            </div>
          </div>
        </div>

        <div class="quick">
          <button
            v-for="q in QUICK_QUESTIONS"
            :key="q"
            class="chip"
            :disabled="loading"
            @click="send(q)"
          >
            {{ q }}
          </button>
        </div>

        <footer class="composer">
          <button
            class="mic"
            :class="{ active: listening }"
            :disabled="!speechSupported"
            :title="speechSupported ? '语音输入' : '当前浏览器不支持语音识别'"
            @click="toggleSpeech"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
              <path d="M19 11a7 7 0 0 1-14 0M12 18v4" />
            </svg>
          </button>
          <input
            v-model="input"
            placeholder="问问商品位置、营养或优惠…"
            :disabled="loading"
            @keyup.enter="send(input)"
          />
          <button class="primary send" :disabled="loading || !input.trim()" @click="send(input)">
            {{ loading ? '思考中' : '发送' }}
          </button>
        </footer>

        <transition name="sheet">
          <div v-if="detail" class="sheet-mask" @click.self="detail = null">
            <div class="sheet">
              <div class="sheet-head">
                <ProductVisual :visual="detail.visual" :size="52" />
                <div>
                  <strong>{{ detail.name }}</strong>
                  <p class="dim">{{ detail.spec }} · {{ detail.brand }}</p>
                </div>
                <button class="ghost close" @click="detail = null">关闭</button>
              </div>
              <div class="sheet-body">
                <div class="price-row">
                  <span class="price">¥{{ detail.member_price }}</span>
                  <span class="dim strike">¥{{ detail.price }}</span>
                  <span class="tag brand">{{ detail.shelf.aisle }} 第 {{ detail.shelf.level }} 层</span>
                  <span class="tag" :class="detail.stock <= 0 ? 'danger' : detail.stock <= 5 ? 'warn' : 'success'">
                    {{ detail.stock <= 0 ? '已售罄' : `库存 ${detail.stock} 件` }}
                  </span>
                </div>
                <p class="dim loc">{{ detail.shelf.desc }}</p>

                <h4>每 100 克/毫升</h4>
                <div class="nutri">
                  <div><em>{{ detail.nutrition.energy_kj }}</em><span>kJ</span></div>
                  <div><em>{{ detail.nutrition.protein_g }}</em><span>蛋白质 g</span></div>
                  <div><em>{{ detail.nutrition.fat_g }}</em><span>脂肪 g</span></div>
                  <div><em>{{ detail.nutrition.carb_g }}</em><span>碳水 g</span></div>
                  <div><em>{{ detail.nutrition.sodium_mg }}</em><span>钠 mg</span></div>
                </div>

                <h4>配料</h4>
                <p class="dim small">{{ detail.ingredients }}</p>

                <h4>过敏原</h4>
                <div class="tags">
                  <span v-if="!detail.allergens.length" class="tag success">包装无标注</span>
                  <span v-for="a in detail.allergens" :key="a" class="tag danger">{{ a }}</span>
                </div>

                <h4>促销</h4>
                <div class="tags">
                  <span v-if="!detail.promotions.length" class="tag">暂无单品促销</span>
                  <span v-for="p in detail.promotions" :key="p.desc" class="tag warn">{{ p.desc }}</span>
                </div>

                <h4>扫码直达</h4>
                <div class="qr-row">
                  <img class="qr-img" :src="qrUrl(detail.sku_id)" alt="本商品二维码" />
                  <div class="qr-tip">
                    <p class="dim small">用导购页「扫一扫」对准此码，直达本商品详情与搭配推荐。</p>
                    <p class="dim small footnote">可打印成货架标签 / 贴在商品区。</p>
                  </div>
                </div>

                <p class="dim footnote">营养与过敏原数据来自商品包装标注，请以实物为准。</p>
              </div>
            </div>
          </div>
        </transition>

        <!-- 扫码层：视频预览 + 手动输入兜底 -->
        <transition name="sheet">
          <div v-if="scanning || scanError" class="sheet-mask" @click.self="closeScanner">
            <div class="sheet scanner">
              <div class="sheet-head">
                <strong>扫码 / 输入 SKU</strong>
                <button class="ghost close" @click="closeScanner">关闭</button>
              </div>
              <div class="sheet-body scan-body">
                <div v-if="scanSupported && scanning" class="scan-video-wrap">
                  <video ref="videoEl" class="scan-video" playsinline muted />
                  <div class="scan-overlay">
                    <div class="scan-reticle" />
                    <p class="dim">把条码/二维码对准框内</p>
                  </div>
                </div>
                <div v-else class="scan-manual">
                  <p class="dim">
                    {{ scanError ? `摄像头不可用：${scanError}` : '当前浏览器不支持原生扫码，请手动输入 SKU（如 SKU001）。' }}
                  </p>
                  <div class="scan-input-row">
                    <input
                      v-model="manualSku"
                      placeholder="SKU001"
                      :disabled="submittingManual"
                      @keyup.enter="submitManualSku"
                    />
                    <button class="primary" :disabled="!manualSku.trim() || submittingManual" @click="submitManualSku">
                      {{ submittingManual ? '查询中' : '查询' }}
                    </button>
                  </div>
                  <p class="dim footnote">已贴条码样本：可口可乐 SKU001 / 百事可乐 SKU002 / 元气森林 SKU003 / 农夫山泉 SKU005 / 乐事黄瓜 SKU009 / 乐事原味 SKU010 / 蒙牛雪糕 SKU018 / 崂山啤酒 SKU028。</p>
                </div>
              </div>
            </div>
          </div>
        </transition>
      </div>
    </section>

    <aside class="side">
      <div class="card panel">
        <h3>A 部分 · 智能导购 Agent</h3>
        <p class="dim">
          顾客用自然语言提问，系统先识别意图，再调用商品工具检索真实数据，
          最后由大模型组织成自然语言流式输出，答案下方自动附带商品卡片，可直接点开查看详情。
        </p>
        <ul class="feat">
          <li><strong>位置问答</strong>精确到货架编号与层数</li>
          <li><strong>营养与过敏原</strong>数据来自商品主数据，附免责说明</li>
          <li><strong>促销与搭配</strong>基于组合规则与购买关联推荐</li>
          <li><strong>多轮上下文</strong>携带最近 6 轮对话</li>
        </ul>
      </div>

      <div class="card panel">
        <h3>运行模式</h3>
        <label class="switch">
          <input v-model="useLlm" type="checkbox" />
          <span>优先调用大模型（未配置 Key 时自动降级）</span>
        </label>
        <div class="kv">
          <span class="dim">本次引擎</span>
          <span class="tag" :class="lastMeta?.mode === 'llm' ? 'success' : 'warn'">
            {{ lastMeta?.mode === 'llm' ? '大模型' : '本地知识库' }}
          </span>
        </div>
        <div class="kv">
          <span class="dim">识别意图</span>
          <span>{{ lastMeta ? (INTENT_LABEL[lastMeta.intent || ''] || lastMeta.intent) : '—' }}</span>
        </div>
        <div class="kv">
          <span class="dim">响应耗时</span>
          <span>{{ lastMeta?.latency ? lastMeta.latency + ' ms' : '—' }}</span>
        </div>
      </div>

      <div class="card panel">
        <h3>可调用工具</h3>
        <div class="tools">
          <code>search_product</code>
          <code>get_location</code>
          <code>get_nutrition</code>
          <code>get_promotions</code>
          <code>recommend_pairing</code>
        </div>
        <p class="dim small">
          大模型只能通过这些工具取数，禁止凭记忆回答价格、营养成分与过敏原，从机制上抑制幻觉。
        </p>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.page {
  display: grid;
  grid-template-columns: minmax(420px, 1fr) 340px;
  gap: 24px;
  padding: 24px;
  min-height: 100%;
  align-items: start;
}

.stage {
  display: flex;
  justify-content: center;
}

/* ---------- 手机外框 ---------- */
.phone {
  position: relative;
  width: 390px;
  height: 760px;
  background: var(--surface);
  border: 10px solid #1e293b;
  border-radius: 42px;
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.statusbar {
  flex: none;
  height: 30px;
  padding: 0 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-3);
  background: var(--surface-2);
}

.notch {
  width: 62px;
  height: 16px;
  border-radius: 10px;
  background: #1e293b;
}

.shop {
  flex: none;
  padding: 12px 16px;
  display: flex;
  align-items: center;
  gap: 10px;
  border-bottom: 1px solid var(--border);
}

.shop-avatar {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: linear-gradient(135deg, #2563eb, #38bdf8);
  color: #fff;
  display: grid;
  place-items: center;
  font-size: 14px;
  font-weight: 600;
}

.shop-info {
  flex: 1;
  display: flex;
  flex-direction: column;
  line-height: 1.35;
}

.shop-info strong { font-size: 14px; }
.shop-info span { font-size: 11px; }

/* ---------- 对话区 ---------- */
.chat {
  flex: 1;
  overflow-y: auto;
  padding: 16px 14px;
  background: #f8fafc;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.row {
  display: flex;
  gap: 8px;
  animation: fade-up 0.25s ease;
}

.row.user {
  justify-content: flex-end;
}

.avatar {
  flex: none;
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: var(--brand-soft);
  color: var(--brand-dark);
  display: grid;
  place-items: center;
  font-size: 12px;
  font-weight: 600;
}

.bubble {
  max-width: 76%;
  padding: 10px 12px;
  border-radius: 14px;
  background: var(--surface);
  border: 1px solid var(--border);
  font-size: 13px;
  line-height: 1.65;
}

.row.user .bubble {
  background: var(--brand);
  border-color: var(--brand);
  color: #fff;
  border-bottom-right-radius: 4px;
}

.row.assistant .bubble { border-bottom-left-radius: 4px; }

.text {
  white-space: pre-wrap;
  word-break: break-word;
}

.caret {
  display: inline-block;
  width: 2px;
  height: 13px;
  margin-left: 2px;
  background: var(--brand);
  vertical-align: -2px;
  animation: blink 0.9s steps(2) infinite;
}

@keyframes blink { 50% { opacity: 0; } }

.intent { margin-bottom: 6px; }

.cards {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pcard {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--surface-2);
  text-align: left;
}

.pcard:hover { border-color: var(--brand); background: var(--brand-soft); }

.pcard-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.pcard-main strong { font-size: 12px; font-weight: 500; }
.pcard-main em { font-size: 11px; font-style: normal; }

.notice {
  margin-top: 8px;
  padding: 6px 8px;
  border-radius: 8px;
  background: var(--warn-soft);
  color: #b45309;
  font-size: 11px;
}

.latency { margin-top: 6px; font-size: 11px; }

/* ---------- 快捷问题与输入 ---------- */
.quick {
  flex: none;
  display: flex;
  gap: 6px;
  padding: 10px 12px 0;
  overflow-x: auto;
  background: var(--surface);
}

.chip {
  flex: none;
  padding: 5px 10px;
  border-radius: 999px;
  font-size: 12px;
  background: var(--surface-2);
  color: var(--text-2);
  white-space: nowrap;
}

.chip:hover { border-color: var(--brand); color: var(--brand-dark); }

.composer {
  flex: none;
  display: flex;
  gap: 8px;
  padding: 12px;
  background: var(--surface);
  border-top: 1px solid var(--border);
}

.composer input { flex: 1; }

.mic {
  flex: none;
  width: 38px;
  display: grid;
  place-items: center;
  padding: 0;
}

.mic.active {
  border-color: var(--brand);
  color: var(--brand);
  animation: pulse-ring 1.4s infinite;
}

.send { flex: none; }

/* ---------- 商品详情 ---------- */
.sheet-mask {
  position: absolute;
  inset: 0;
  background: rgba(15, 23, 42, 0.35);
  display: flex;
  align-items: flex-end;
}

.sheet {
  width: 100%;
  max-height: 78%;
  overflow-y: auto;
  background: var(--surface);
  border-radius: 18px 18px 0 0;
  padding: 16px;
  animation: fade-up 0.25s ease;
}

.sheet-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
}

.sheet-head div:first-of-type { flex: 1; }
.sheet-head p { margin: 2px 0 0; font-size: 12px; }
.close { flex: none; }

.sheet-body { padding-top: 12px; }
.sheet-body h4 { margin: 14px 0 6px; font-size: 13px; }

.price-row { display: flex; align-items: baseline; gap: 8px; }
.price { font-size: 20px; font-weight: 600; color: var(--danger); }
.strike { text-decoration: line-through; font-size: 12px; }
.loc { font-size: 12px; margin: 4px 0 0; }

.nutri {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 6px;
}

.nutri div {
  background: var(--surface-2);
  border-radius: 8px;
  padding: 8px 4px;
  text-align: center;
}

.nutri em {
  display: block;
  font-style: normal;
  font-size: 14px;
  font-weight: 600;
}

.nutri span { font-size: 10px; color: var(--text-3); }

.tags { display: flex; flex-wrap: wrap; gap: 6px; }
.small { font-size: 12px; }
.footnote { font-size: 11px; margin-top: 14px; }

/* 扫码直达二维码 */
.qr-row { display: flex; align-items: center; gap: 12px; }
.qr-img {
  width: 96px;
  height: 96px;
  padding: 4px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: #fff;
}
.qr-tip { flex: 1; display: flex; flex-direction: column; gap: 2px; }

.sheet-enter-active, .sheet-leave-active { transition: opacity 0.2s ease; }
.sheet-enter-from, .sheet-leave-to { opacity: 0; }

/* ---------- 侧栏 ---------- */
.side {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.panel { padding: 16px; }
.panel h3 { font-size: 14px; margin-bottom: 8px; }
.panel p { font-size: 12px; margin: 0; }

.feat {
  margin: 10px 0 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--text-2);
  line-height: 1.9;
}

.switch {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  margin-bottom: 10px;
  cursor: pointer;
}

.kv {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  padding: 5px 0;
}

.tools { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }

.tools code {
  font-size: 11px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 3px 7px;
  color: var(--brand-dark);
}

@media (max-width: 1080px) {
  .page { grid-template-columns: 1fr; }
}

/* ---------- 手机端 / 窄屏适配 ----------
   ≤640px：不再演示手机外壳，整个屏幕就是手机本身。
   侧栏放到抽屉里或者省略掉（演示场景下省略更干净）。 */
@media (max-width: 640px) {
  .page {
    grid-template-columns: 1fr;
    padding: 0;
    gap: 0;
  }

  .side { display: none; }   /* 手机端不显示「运行模式/工具」面板，导购核心就在手机里 */

  .stage { display: block; }

  .phone {
    width: 100vw;
    height: 100vh;
    height: 100dvh;          /* 适配 iOS 浏览器底部地址栏 */
    border: none;
    border-radius: 0;
    box-shadow: none;
  }

  .notch { display: none; }   /* 真手机上没有刘海占位 */
  .statusbar { display: none; } /* 真手机的系统状态栏由浏览器/系统接管 */

  .shop {
    padding: 10px 14px;
    gap: 8px;
  }
  .shop-avatar { width: 30px; height: 30px; font-size: 13px; }
  .shop-info strong { font-size: 14px; }
  .shop-info span { font-size: 11px; }

  .chat {
    padding: 12px 12px;
    gap: 12px;
    -webkit-overflow-scrolling: touch;
    overscroll-behavior: contain;
  }

  .bubble { max-width: 84%; }

  /* 消息气泡内嵌的商品卡片：单列，避免在小屏挤换行 */
  .cards { grid-template-columns: 1fr; }
  .pcard { padding: 8px 10px; }
  .pcard-main strong { font-size: 13px; }
  .pcard-main em { font-size: 11px; }

  /* 快捷问题：横向可滚动，避免被挤变形 */
  .quick {
    flex-wrap: nowrap;
    overflow-x: auto;
    overflow-y: hidden;
    padding: 6px 12px 4px;
    scrollbar-width: none;
  }
  .quick::-webkit-scrollbar { display: none; }
  .chip { white-space: nowrap; flex: none; }

  .composer {
    padding: 8px 10px calc(8px + env(safe-area-inset-bottom));
  }
  .composer input { font-size: 15px; }      /* iOS 16+ 才不会在聚焦时自动放大 */
  .composer .mic { width: 34px; height: 34px; }
  .composer .send { padding: 0 14px; height: 34px; font-size: 13px; }

  /* 商品详情抽屉：宽度顶满，避免被桌面尺寸卡住 */
  .sheet {
    max-width: 100%;
    border-radius: 16px 16px 0 0;
  }

  /* 输入时键盘弹出：用 visualViewport 缩短布局高度，避免页面被顶起 */
  html.ios-keyboard .phone { height: calc(100dvh - var(--vv-offset, 0px)); }
}

/* ---------- 极窄屏（如 iPhone SE 第一代 / Android 360dp） ----------
   把消息气泡进一步压缩；快捷问题只显示一行滚动 */
@media (max-width: 360px) {
  .bubble { max-width: 90%; padding: 9px 12px; font-size: 13px; }
  .row .avatar { width: 24px; height: 24px; font-size: 11px; }
  .composer input { font-size: 14px; }
}

/* ---------- 扫一扫按钮 ---------- */
.scan-btn {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 30px;
  padding: 0 10px;
  border-radius: 16px;
  border: 1px solid var(--brand);
  background: var(--brand-soft);
  color: var(--brand-dark);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: transform 0.15s ease, background 0.15s ease;
}
.scan-btn:hover { background: var(--brand); color: #fff; }
.scan-btn:active { transform: scale(0.97); }
.scan-btn svg { flex: none; }

/* ---------- 扫码层 ---------- */
.scanner { width: 360px; max-width: calc(100vw - 32px); }
.scan-body { padding: 14px 16px 16px; }

.scan-video-wrap {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  background: #0f172a;
  border-radius: 10px;
  overflow: hidden;
}
.scan-video {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.scan-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}
.scan-reticle {
  width: 70%;
  height: 50%;
  border: 2px solid #38bdf8;
  border-radius: 12px;
  box-shadow: 0 0 0 9999px rgba(15, 23, 42, 0.35);
}
.scan-overlay .dim { color: #cbd5e1; margin-top: 12px; font-size: 12px; }

.scan-manual { display: flex; flex-direction: column; gap: 12px; }
.scan-input-row { display: flex; gap: 8px; }
.scan-input-row input {
  flex: 1;
  height: 38px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 14px;
  background: var(--surface);
  color: var(--text);
}
.scan-input-row button { height: 38px; padding: 0 14px; }
.footnote { font-size: 11px; line-height: 1.5; margin-top: 4px; }
</style>
