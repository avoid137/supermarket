<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ProductVisual from '@/components/ProductVisual.vue'
import { api } from '@/api'
import type { Detection, Product, SceneInfo, SessionSnapshot } from '@/types'

const session = ref<SessionSnapshot | null>(null)
const scenes = ref<SceneInfo[]>([])
const products = ref<Product[]>([])
const activeScene = ref<string | null>(null)
const busy = ref(false)
const error = ref('')
const initFailed = ref(false)
const recognizeMs = ref(0)
const framesUsed = ref(0)
const visionEnabled = ref(false)
const useVision = ref(true)
/** 模拟称重传感器读数（克）。用户在拍照前填入整盘商品的净重，
 *  后端用它做相似组规格终裁（OCR/尺子都分不清时按重量唯一确定 40g/70g 等）。 */
const trayWeightInput = ref<number | null>(null)

/**
 * 逐件录入模式：开启后，每次「拍照识别」只录入一件（或当帧识别出的若干件），
 * 结果追加到已有账单，摄像头在「无待确认项」时自动重开，准备下一件；
 * 用户点「录入完成」结束本轮。判断逻辑（auto/clarify/review）与整盘识别完全一致。
 * entryActive 标记「录入轮次已激活」，避免页面刚加载时 watcher 就自动开摄像头。
 */
const entryMode = ref(true)
const entryActive = ref(false)
/** 最近一次拍摄新增的检测框 box_id，用于「本件重拍」一键丢弃并重新拍摄。 */
const lastCaptureBoxIds = ref<string[]>([])

const cameraOn = ref(false)
const capturedImage = ref('')
const manualPicker = ref<Detection | null>(null)
/** 识别整体失败、画面里一个检测框都没有时的凭空补录入口。 */
const manualAddOpen = ref(false)
const videoEl = ref<HTMLVideoElement | null>(null)
const canvasEl = ref<HTMLCanvasElement | null>(null)
let stream: MediaStream | null = null

/** 连拍帧数。3 帧是投票机制的最小可用值：能容一帧出错，又不至于让等待变长。 */
const VOTE_FRAMES = 3
/** 帧间隔。间隔太短会拍到几乎一样的画面，投票就失去了意义。 */
const FRAME_INTERVAL_MS = 150

const STATE_FLOW = ['CREATED', 'RECOGNIZING', 'NEED_CLARIFY', 'CONFIRMED', 'PAYING', 'PAID']
const STATE_LABEL: Record<string, string> = {
  CREATED: '等待放入商品',
  CAPTURING: '正在拍摄',
  RECOGNIZING: '识别完成',
  NEED_CLARIFY: '待顾客确认',
  REVIEWING: '转人工复核',
  CONFIRMED: '账单已确认',
  PAYING: '支付中',
  PAID: '支付完成',
  FAILED: '异常',
}

const productMap = computed(() => {
  const map: Record<string, Product> = {}
  for (const p of products.value) map[p.sku_id] = p
  return map
})

const detections = computed(() => session.value?.detections ?? [])

/**
 * 人工录入的条目没有画面位置（w/h 都是 0），画出来只是个空点，
 * 还会让顾客误以为画面上真有这么个框，所以不参与检测框渲染。
 */
const visibleDetections = computed(() => detections.value.filter((d) => !d.box_id.startsWith('M')))

const bill = computed(() => session.value?.bill ?? null)

const pendingClarify = computed(() =>
  detections.value.filter((d) => d.state === 'clarify' && !session.value?.resolved[d.box_id]),
)

// 后端不会改写 detection.state，只把确认结果记在 resolved 里，
// 所以这里必须排除已确认项，否则补入账单后仍会停留在待复核列表
const unresolvedReview = computed(() =>
  detections.value.filter((d) => d.state === 'review' && !session.value?.resolved[d.box_id]),
)

const manuallyAdded = computed(() =>
  detections.value.filter((d) => d.state === 'review' && session.value?.resolved[d.box_id]),
)

const weightCheck = computed(() => session.value?.weight_check ?? null)

/**
 * 识别模式决定该给顾客看多少信任，三种语义必须分清楚：
 *   vision         —— 真实模型识别成功，结果可信
 *   vision-failed  —— 真实模型调用失败，画面里一个框都没有，必须让人来录
 *   demo:S1        —— 示例场景的预设数据，只用于演示界面流程
 * 混为一谈的代价是顾客拿到的账单跟他买的东西对不上，
 * 他却以为机器只是"认错了"——这比直接报失败更伤信任。
 */
const recognizeMode = computed(() => session.value?.mode ?? '')
/**
 * 真实识别现在有三种子模式，都算可信：
 *   vision          整图识别（框是按数量均分的占位框）
 *   vision-vote     连拍多帧投票
 *   vision-cascade  检测器出真实框 + 大模型判类别
 * 唯独 vision-failed 不可信，必须如实提示顾客转人工。
 */
const isRealVision = computed(
  () => recognizeMode.value.startsWith('vision') && recognizeMode.value !== 'vision-failed',
)
const isVisionFailed = computed(() => recognizeMode.value === 'vision-failed')
const isCascade = computed(() => recognizeMode.value.startsWith('vision-cascade'))
const isDemo = computed(() => recognizeMode.value.startsWith('demo'))

/**
 * 真实识别成功返回、但一个框都没检测到的场景（与 vision-failed 区分开）：
 * vision-failed 是模型调用失败，这里模型跑通了但画面里确实没商品——可能是漏拍、
 * 摆放不当或空盘，需要醒目提示而不是安静地显示「共 0 个目标」。
 */
const isEmptyDetection = computed(
  () => isRealVision.value && detections.value.length === 0,
)

function skuOf(d: Detection) {
  return session.value?.resolved[d.box_id] || d.candidates[0]?.sku_id || ''
}

function boxStyle(d: Detection) {
  return {
    left: `${d.x}%`,
    top: `${d.y}%`,
    width: `${d.w}%`,
    height: `${d.h}%`,
  }
}

async function initSession() {
  try {
    session.value = await api.createSession()
    initFailed.value = false
    error.value = ''
  } catch (e) {
    initFailed.value = true
    error.value = `无法连接后端服务：${(e as Error).message}`
  }
}

/**
 * 保证会话可用。
 * 不能让 session 为空时静默 return——那样用户点任何按钮都毫无反应，
 * 还以为是界面坏了。初始化失败要能自愈，自愈不了要明确告知。
 */
async function ensureSession() {
  if (session.value) return session.value
  try {
    session.value = await api.createSession()
    initFailed.value = false
    error.value = ''
  } catch (e) {
    initFailed.value = true
    error.value = `无法连接结账服务：${(e as Error).message}`
  }
  return session.value
}

async function runRecognize(
  payload: {
    scene_id?: string
    image_base64?: string
    images?: string[]
    tray_weight_g?: number | null
  },
  append = false,
) {
  const current = await ensureSession()
  if (!current || busy.value) return
  busy.value = true
  error.value = ''

  try {
    const resp = await api.recognize(current.session_id, {
      scene_id: payload.scene_id ?? null,
      image_base64: payload.image_base64 ?? null,
      images: payload.images ?? null,
      tray_weight_g: payload.tray_weight_g ?? null,
      use_vision_model: useVision.value,
      append,
    })
    recognizeMs.value = resp.elapsed_ms
    framesUsed.value = resp.frames
    session.value = await api.getSession(current.session_id)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

async function pickScene(scene: SceneInfo) {
  entryActive.value = false
  lastCaptureBoxIds.value = []
  activeScene.value = scene.scene_id
  stopCamera()
  capturedImage.value = ''
  await runRecognize({ scene_id: scene.scene_id, tray_weight_g: scene.tray_weight_g })
}

async function chooseCandidate(boxId: string, skuId: string) {
  const current = await ensureSession()
  if (!current || busy.value) return
  busy.value = true
  try {
    session.value = await api.clarify(current.session_id, boxId, skuId)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

/**
 * 移除一笔检测项（待确认 / 人工复核 / 已入账皆可）。
 * 检测器偶发的重复框或顾客误选会让账单多出不该有的条目，
 * 之前只能整单重置，现在可以按 box_id 精确摘除并同步刷新账单。
 */
async function removeDetection(boxId: string) {
  const current = await ensureSession()
  if (!current || busy.value) return
  busy.value = true
  try {
    session.value = await api.removeItem(current.session_id, boxId)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

/**
 * 账单行是按 sku 聚合的，本身不带 box_id，所以先反查当前为这个 sku
 * 贡献账单的第一笔检测项，再摘除它。重复识别时这样就能精准删掉多出来的那件。
 */
async function removeBillRow(skuId: string) {
  const current = await ensureSession()
  if (!current || !session.value || busy.value) return
  const target = session.value.detections.find((d) => {
    const chosen = session.value!.resolved[d.box_id] || d.candidates[0]?.sku_id
    if (chosen !== skuId) return false
    if (d.state === 'review' && !session.value!.resolved[d.box_id]) return false
    return true
  })
  if (target) await removeDetection(target.box_id)
}

/**
 * 同款多件时调数量，不必反复拍摄录入。
 * 账单按 sku 聚合，这里反查第一个为该 sku 贡献账单的已确认检测项，
 * 调它的 quantity，再由后端统一重算账单/称重/状态。
 */
async function adjustRowQty(skuId: string, delta: number) {
  const current = await ensureSession()
  if (!current || !session.value || busy.value) return
  const target = session.value.detections.find((d) => {
    const chosen = session.value!.resolved[d.box_id] || d.candidates[0]?.sku_id
    if (chosen !== skuId) return false
    if (d.state === 'review' && !session.value!.resolved[d.box_id]) return false
    return true
  })
  if (!target) return
  busy.value = true
  try {
    session.value = await api.adjustQuantity(current.session_id, target.box_id, delta)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

function closeManualPicker() {
  manualPicker.value = null
  manualAddOpen.value = false
}

/**
 * 人工指定商品，两种情况共用同一个选择面板：
 *   有检测框 —— 遮挡严重、模型连候选都给不准，走复核确认
 *   没有框   —— 识别整体失败，画面空空如也，只能凭空补录
 */
async function pickManual(skuId: string) {
  const target = manualPicker.value
  closeManualPicker()
  if (target) {
    await chooseCandidate(target.box_id, skuId)
    return
  }

  const current = await ensureSession()
  if (!current || busy.value) return
  busy.value = true
  try {
    session.value = await api.addManualItem(current.session_id, skuId, 1)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

async function confirmAndPay() {
  const current = await ensureSession()
  if (!current || !bill.value?.items.length) return
  busy.value = true
  error.value = ''
  try {
    session.value = await api.confirm(current.session_id, true)
    session.value = await api.pay(current.session_id, 'wechat')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

async function restart() {
  if (!session.value) return
  entryActive.value = false
  lastCaptureBoxIds.value = []
  stopCamera()
  capturedImage.value = ''
  activeScene.value = null
  recognizeMs.value = 0
  framesUsed.value = 0
  session.value = await api.reset(session.value.session_id)
  error.value = ''
}

/* ---------------- 摄像头 ---------------- */

async function startCamera() {
  if (cameraOn.value) return
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { width: 1280, height: 720 } })
    cameraOn.value = true
    await Promise.resolve()
    if (videoEl.value) {
      videoEl.value.srcObject = stream
      await videoEl.value.play()
    }
  } catch {
    error.value = '无法访问摄像头，可直接使用示例购物盘场景'
    cameraOn.value = false
  }
}

function stopCamera() {
  if (stream) {
    stream.getTracks().forEach((t) => t.stop())
    stream = null
  }
  cameraOn.value = false
}

async function capture() {
  const video = videoEl.value
  const canvas = canvasEl.value
  if (!video || !canvas) return
  canvas.width = video.videoWidth || 1280
  canvas.height = video.videoHeight || 720
  const ctx = canvas.getContext('2d')

  // 连拍多帧。单帧判断容易被那一帧的反光、遮挡或角度带偏，
  // 多帧取共识是成本最低的稳定性提升，代价只是多等几百毫秒。
  const shots: string[] = []
  for (let i = 0; i < VOTE_FRAMES; i++) {
    if (i > 0) await new Promise((r) => setTimeout(r, FRAME_INTERVAL_MS))
    ctx?.drawImage(video, 0, 0, canvas.width, canvas.height)
    shots.push(canvas.toDataURL('image/jpeg', 0.85).split(',')[1])
  }

  // 拍照后定格画面：关掉实时流，用抓拍图当背景，识别框叠加其上，贴近真实结算台体验。
  // 逐件录入模式下相机由 watch 在「无待确认项」时自动重开，准备录入下一件；
  // 整盘识别模式则停在原地，等待用户处理本帧的核对项。
  capturedImage.value = `data:image/jpeg;base64,${shots[0]}`
  stopCamera()

  // 两种模式下拍照都「累加」到当前账单：
  // 之前非逐件录入模式每次拍照都整盘替换，沿用「放一件拍一件」的习惯时，
  // 每拍一次就把上一轮的商品清空，于是看上去「只能识别到一件」。
  // 改成始终追加后，无论整盘一次拍下还是逐件多次拍，账单只会越攒越多；
  // 想整盘重扫，用「重新结算」清空会话即可。逐件录入的开关只决定拍完是否自动开摄像头。
  const beforeIds = new Set((session.value?.detections ?? []).map((d) => d.box_id))
  await runRecognize({ images: shots, tray_weight_g: trayWeightInput.value ?? null }, true)
  if (entryMode.value && session.value) {
    lastCaptureBoxIds.value = session.value.detections
      .filter((d) => !beforeIds.has(d.box_id))
      .map((d) => d.box_id)
  }
}

/**
 * 本件识别不准时，撤销最近一次拍摄追加的所有条目并重新开摄像头。
 * 复用已有的按 box_id 移除接口，无需新后端逻辑；移除后 watch 会在无待确认项时
 * 自动重开摄像头，自然进入「重新拍这一件」的状态。
 */
async function reshootLast() {
  const current = await ensureSession()
  if (!current || busy.value || !lastCaptureBoxIds.value.length) return
  busy.value = true
  try {
    for (const bid of lastCaptureBoxIds.value) {
      session.value = await api.removeItem(current.session_id, bid)
    }
    lastCaptureBoxIds.value = []
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

/**
 * 进入逐件录入轮次：标记激活并打开摄像头。
 * entryActive 是 watch 自动重开摄像头的前置条件，避免页面加载即弹摄像头权限。
 */
function startEntry() {
  entryActive.value = true
  startCamera()
}

/** 录入完成：结束本轮，关闭摄像头，保留已累计的账单。 */
function finishEntry() {
  entryActive.value = false
  lastCaptureBoxIds.value = []
  stopCamera()
}

/** 录入中手动暂停：关闭摄像头但保持 entryActive，避免 watcher 立刻重开。 */
function pauseEntry() {
  entryActive.value = false
  stopCamera()
}

/** 模式开关切换：切回整盘识别时，清掉录入激活态并关摄像头。 */
function onEntryToggle() {
  if (!entryMode.value) {
    entryActive.value = false
    stopCamera()
  }
}

/**
 * 逐件录入的「自动准备下一件」核心：只要没有待确认/待复核项、
 * 不在处理中、摄像头已关、未支付，就重开摄像头。
 * 用户每解完一个 clarify/review 项，pending 列表变空，这里立刻重新亮起摄像头。
 */
watch(
  () => [
    pendingClarify.value.length,
    unresolvedReview.value.length,
    busy.value,
    cameraOn.value,
    session.value?.state,
    entryActive.value,
    entryMode.value,
  ],
  () => {
    const ready = pendingClarify.value.length === 0 && unresolvedReview.value.length === 0
    const paid = session.value?.state === 'PAID'
    if (entryMode.value && entryActive.value && ready && !busy.value && !cameraOn.value && !paid) {
      startCamera()
    }
  },
)

onMounted(async () => {
  try {
    products.value = await api.products()
    scenes.value = await api.scenes()
    const health = await api.health()
    visionEnabled.value = health.vision_enabled
    await initSession()
  } catch (e) {
    error.value = (e as Error).message
  }
})

onBeforeUnmount(stopCamera)
</script>

<template>
  <div class="page">
    <div class="kiosk">
      <!-- 顶栏 -->
      <header class="kiosk-top">
        <div class="kiosk-title">
          <strong>自助结算台 · 03 号</strong>
          <span class="dim">
            {{
              entryMode
                ? '逐件录入：放一件 → 拍照 → 自动准备下一件，全部录完点「录入完成」'
                : '可整盘一次拍下，也可放一件拍一件多次累加；想整盘重扫请先点「重新结算」'
            }}
          </span>
        </div>
        <div class="flow">
          <span
            v-for="s in STATE_FLOW"
            :key="s"
            class="flow-node"
            :class="{ done: session && STATE_FLOW.indexOf(session.state) >= STATE_FLOW.indexOf(s) }"
          >
            {{ STATE_LABEL[s] }}
          </span>
        </div>
        <div class="kiosk-meta">
          <span class="tag">{{ session?.session_id || '未创建会话' }}</span>
          <span class="tag" :class="isRealVision ? 'success' : isVisionFailed ? 'danger' : 'warn'">
            {{
              isRealVision
                ? '视觉大模型'
                : isVisionFailed
                  ? '识别失败'
                  : isDemo
                    ? '演示数据'
                    : '模拟识别'
            }}
          </span>
          <button class="ghost" :disabled="busy" @click="restart">重新结算</button>
        </div>
      </header>

      <div class="kiosk-body">
        <!-- 左：购物盘 -->
        <section class="tray-area">
          <div v-if="!session" class="alert">
            <div class="alert-text">
              <strong>未连接到结账服务</strong>
              <span class="dim small">{{ error || '正在初始化会话…' }}</span>
            </div>
            <button v-if="initFailed" class="primary" :disabled="busy" @click="initSession">
              重试
            </button>
          </div>

          <div v-if="isVisionFailed" class="alert">
            <div class="alert-text">
              <strong>没能识别出来，请手动录入商品</strong>
              <span class="dim small">
                视觉模型调用失败。宁可不给结果，也不能给错账单——系统不会拿示例数据顶替，
                请在右侧逐件录入，或用下方示例场景体验流程。
              </span>
            </div>
            <button class="primary" :disabled="busy" @click="manualAddOpen = true">
              手动录入
            </button>
          </div>

          <div v-else-if="isDemo" class="alert warn">
            <div class="alert-text">
              <strong>当前展示的是示例场景数据</strong>
              <span class="dim small">内置演示数据，仅用于体验界面流程，不代表真实识别结果</span>
            </div>
          </div>

          <div v-else-if="isEmptyDetection" class="alert warn">
            <div class="alert-text">
              <strong>未识别到任何商品</strong>
              <span class="dim small">画面中没有检测到商品，请重新摆放后重拍，或在右侧手动录入</span>
            </div>
            <button class="primary" :disabled="busy" @click="manualAddOpen = true">
              手动录入
            </button>
          </div>

          <div class="tray-head">
            <h3>购物盘实拍</h3>
            <span v-if="entryMode && bill" class="entry-count">已录入 {{ bill.total_quantity }} 件</span>
            <label class="mode-toggle" :class="{ on: entryMode }">
              <input type="checkbox" v-model="entryMode" @change="onEntryToggle" />
              <span>逐件录入</span>
            </label>
            <label
              class="weight-input"
              title="模拟称重传感器：填入整盘商品的净重（克），后端用它区分同款不同规格（如 40g/70g 薯片）"
            >
              <span class="dim small">模拟称重</span>
              <input
                type="number"
                min="0"
                step="1"
                placeholder="整盘净重 g"
                v-model.number="trayWeightInput"
                :disabled="busy"
              />
            </label>
            <div class="tray-actions">
              <button v-if="!cameraOn" @click="startEntry">开启摄像头</button>
              <button v-else class="primary" :disabled="busy" @click="capture">
                {{ busy ? '识别中…' : '拍照识别' }}
              </button>
              <button
                v-if="entryMode && entryActive && lastCaptureBoxIds.length && !busy"
                class="ghost"
                @click="reshootLast"
              >
                本件重拍
              </button>
              <button
                v-if="cameraOn && entryMode && entryActive"
                class="ghost"
                :disabled="busy"
                @click="finishEntry"
              >
                录入完成
              </button>
              <button
                v-if="cameraOn && !(entryMode && entryActive)"
                class="ghost"
                @click="stopCamera"
              >
                关闭
              </button>
              <button
                v-if="cameraOn && entryMode && entryActive"
                class="ghost"
                :disabled="busy"
                @click="pauseEntry"
              >
                暂停
              </button>
            </div>
          </div>

          <div class="tray" :class="{ scanning: busy }">
            <video v-show="cameraOn" ref="videoEl" class="camera" muted playsinline />
            <canvas ref="canvasEl" class="hidden" />
            <img
              v-if="!cameraOn && capturedImage"
              :src="capturedImage"
              class="captured"
              alt="抓拍画面"
            />

            <template v-if="!cameraOn">
              <div v-if="!capturedImage" class="tray-plate" />
              <div
                v-for="d in visibleDetections"
                :key="d.box_id"
                class="box"
                :class="d.state"
                :style="boxStyle(d)"
              >
                <div class="box-visual">
                  <ProductVisual
                    v-if="productMap[skuOf(d)]"
                    :visual="productMap[skuOf(d)].visual"
                    :size="40"
                  />
                </div>
                <span class="box-label">
                  {{ productMap[skuOf(d)]?.visual.label || '未识别' }}
                  <em v-if="d.quantity > 1">×{{ d.quantity }}</em>
                </span>
                <span class="box-score">{{ Math.round((d.candidates[0]?.score ?? 0) * 100) }}%</span>
              </div>
              <div v-if="busy" class="scan-line" />
              <p v-if="!detections.length && !busy && !capturedImage" class="empty dim">
                选择下方示例场景，或开启摄像头拍摄
              </p>
            </template>
          </div>

          <p v-if="cameraOn && !visionEnabled" class="dim small">
            未配置视觉大模型 Key，拍照后将使用模拟识别引擎，结果来自内置示例商品库
          </p>

          <div class="scenes">
            <button
              v-for="s in scenes"
              :key="s.scene_id"
              class="scene"
              :class="{ active: activeScene === s.scene_id }"
              :disabled="busy"
              @click="pickScene(s)"
            >
              <strong>{{ s.scene_id }} · {{ s.title }}</strong>
              <em class="dim">{{ s.item_count }} 件 · {{ s.tray_weight_g }}g</em>
            </button>
          </div>

          <div v-if="weightCheck" class="weight" :class="weightCheck.status">
            <strong>称重校验</strong>
            <span>{{ weightCheck.message }}</span>
          </div>
          <p v-if="recognizeMs" class="dim small">
            识别耗时 {{ recognizeMs }}ms · 共 {{ detections.length }} 个目标<template
              v-if="framesUsed > 1"
            >
              · {{ framesUsed }} 帧投票</template
            ><template v-if="isCascade"> · 检测器出框</template>
          </p>
          <p v-if="error" class="error">{{ error }}</p>
        </section>

        <!-- 右：账单 -->
        <section class="bill-area">
          <div class="bill-head">
            <h3>电子账单</h3>
            <div class="bill-head-right">
              <span class="tag">{{ STATE_LABEL[session?.state || 'CREATED'] }}</span>
              <button class="ghost" :disabled="busy" @click="manualAddOpen = true">
                + 手动添加
              </button>
            </div>
          </div>

          <div class="bill-list">
            <div v-for="item in bill?.items || []" :key="item.sku_id" class="bill-row">
              <ProductVisual
                v-if="productMap[item.sku_id]"
                :visual="productMap[item.sku_id].visual"
                :size="32"
                :show-label="false"
              />
              <div class="bill-main">
                <strong>{{ item.name }}</strong>
                <em class="dim">{{ item.spec }} · ¥{{ item.unit_price }} × {{ item.quantity }}</em>
                <div v-if="item.promotions.length || item.source !== 'auto'" class="row-tags">
                  <span v-if="item.source === 'manual'" class="tag danger">人工确认</span>
                  <span v-else-if="item.source === 'clarify'" class="tag brand">顾客确认</span>
                  <span v-for="p in item.promotions" :key="p" class="tag warn">{{ p }}</span>
                </div>
              </div>
              <div class="qty-stepper">
                <button
                  class="ghost tiny"
                  title="减少一件"
                  :disabled="busy || session?.state === 'PAID' || item.quantity <= 1"
                  @click="adjustRowQty(item.sku_id, -1)"
                >
                  −
                </button>
                <span class="qty-num">{{ item.quantity }}</span>
                <button
                  class="ghost tiny"
                  title="增加一件（同款多件无需重拍）"
                  :disabled="busy || session?.state === 'PAID'"
                  @click="adjustRowQty(item.sku_id, 1)"
                >
                  +
                </button>
              </div>
              <div class="bill-amount">
                <span>¥{{ (item.subtotal - item.discount).toFixed(2) }}</span>
                <span v-if="item.discount" class="dim strike">¥{{ item.subtotal.toFixed(2) }}</span>
              </div>
              <button
                class="ghost danger tiny"
                title="移除该商品"
                :disabled="busy || session?.state === 'PAID'"
                @click="removeBillRow(item.sku_id)"
              >
                移除
              </button>
            </div>
            <p v-if="!bill?.items.length" class="empty dim">账单为空</p>
          </div>

          <div v-if="unresolvedReview.length" class="review-box">
            <div class="review-head">
              <strong>{{ unresolvedReview.length }} 件商品已转人工复核</strong>
              <span class="dim small">未计入账单，可由顾客指认或店员远程核对后补入</span>
            </div>
            <div v-for="d in unresolvedReview" :key="d.box_id" class="review-item">
              <span class="dim small">{{ d.box_id }} · {{ d.message }}</span>
              <p v-if="d.evidence" class="evidence">包装文字：{{ d.evidence }}</p>
              <div class="options">
                <button
                  v-for="c in d.candidates"
                  :key="c.sku_id"
                  :disabled="busy"
                  @click="chooseCandidate(d.box_id, c.sku_id)"
                >
                  {{ c.name }}
                  <em class="dim">{{ Math.round(c.score * 100) }}%</em>
                </button>
                <button class="primary" :disabled="busy" @click="manualPicker = d">
                  从全部商品指定…
                </button>
                <button class="ghost danger" :disabled="busy" @click="removeDetection(d.box_id)">
                  移除
                </button>
              </div>
            </div>
          </div>

          <div v-if="manuallyAdded.length" class="review-done">
            <strong>已人工补入 {{ manuallyAdded.length }} 件</strong>
            <span class="dim small">
              {{ manuallyAdded.map((d) => productMap[skuOf(d)]?.name).join('、') }}
            </span>
            <button
              v-for="d in manuallyAdded"
              :key="d.box_id"
              class="ghost danger tiny"
              :disabled="busy"
              @click="removeDetection(d.box_id)"
            >
              移除{{ productMap[skuOf(d)]?.name }}
            </button>
          </div>

          <div class="bill-total">
            <div class="total-row">
              <span class="dim">商品金额</span>
              <span>¥{{ (bill?.origin_amount ?? 0).toFixed(2) }}</span>
            </div>
            <div class="total-row">
              <span class="dim">优惠合计</span>
              <span class="save">-¥{{ (bill?.discount_amount ?? 0).toFixed(2) }}</span>
            </div>
            <div class="total-row payable">
              <span>应付金额</span>
              <strong>¥{{ (bill?.payable ?? 0).toFixed(2) }}</strong>
            </div>
          </div>

          <p v-if="unresolvedReview.length" class="review-hint">
            还有 {{ unresolvedReview.length }} 件商品待人工确认 / 未识别，请先在上方复核区指认或移除后再支付
          </p>
          <button
            class="primary large pay"
            :disabled="!bill?.items.length || busy || session?.state === 'PAID' || unresolvedReview.length > 0"
            @click="confirmAndPay"
          >
            {{ session?.state === 'PAID' ? '已完成支付' : '确认并支付' }}
          </button>
        </section>
      </div>

      <!-- 底部追问区 -->
      <footer v-if="pendingClarify.length" class="clarify">
        <div class="clarify-head">
          <strong>{{ pendingClarify.length }} 件商品需要你确认</strong>
          <span class="dim small">相似包装难以自动区分，点选正确商品后账单立即更新</span>
        </div>
        <div class="clarify-list">
          <div v-for="d in pendingClarify" :key="d.box_id" class="clarify-item">
            <span class="dim small">{{ d.box_id }} · {{ d.message }}</span>
            <p v-if="d.evidence" class="evidence">包装文字：{{ d.evidence }}</p>
            <div class="options">
              <button
                v-for="c in d.candidates"
                :key="c.sku_id"
                :disabled="busy"
                @click="chooseCandidate(d.box_id, c.sku_id)"
              >
                {{ c.name }}
                <em class="dim">{{ Math.round(c.score * 100) }}%</em>
              </button>
              <button class="ghost danger" :disabled="busy" @click="removeDetection(d.box_id)">
                移除
              </button>
            </div>
          </div>
        </div>
      </footer>

      <!-- 人工复核：从全店商品指定 -->
      <transition name="fade">
        <div v-if="manualPicker || manualAddOpen" class="pick-mask" @click.self="closeManualPicker">
          <div class="pick-card">
            <div class="pick-head">
              <div>
                <strong>{{ manualPicker ? '人工复核 · 指定商品' : '手动录入 · 选择商品' }}</strong>
                <p class="dim small">
                  {{
                    manualPicker
                      ? `${manualPicker.box_id} · ${manualPicker.message}`
                      : '识别未能返回结果，请逐件选择您购买的商品'
                  }}
                </p>
              </div>
              <button class="ghost" :disabled="busy" @click="closeManualPicker">取消</button>
            </div>
            <div class="pick-grid">
              <button
                v-for="p in products"
                :key="p.sku_id"
                class="pick-item"
                :disabled="busy"
                @click="pickManual(p.sku_id)"
              >
                <ProductVisual :visual="p.visual" :size="34" :show-label="false" />
                <span class="pick-name">{{ p.name }}</span>
                <em class="dim">¥{{ p.member_price }}</em>
              </button>
            </div>
          </div>
        </div>
      </transition>

      <!-- 支付成功 -->
      <transition name="fade">
        <div v-if="session?.state === 'PAID'" class="paid-mask">
          <div class="paid-card">
            <div class="paid-icon">✓</div>
            <h2>支付成功</h2>
            <p class="dim">感谢惠顾，欢迎再次光临</p>
            <div class="paid-amount">¥{{ (bill?.payable ?? 0).toFixed(2) }}</div>
            <div class="paid-meta">
              <div><span class="dim">订单号</span><strong>{{ session.session_id }}</strong></div>
              <div><span class="dim">支付方式</span><strong>微信支付</strong></div>
              <div><span class="dim">商品件数</span><strong>{{ bill?.total_quantity ?? 0 }} 件</strong></div>
            </div>
            <button class="primary large" @click="restart">开始下一单</button>
          </div>
        </div>
      </transition>
    </div>
  </div>
</template>

<style scoped>
.page { padding: 20px; }

/*
 * 必须是定位祖先：.pick-mask 与 .paid-mask 都是 position:absolute; inset:0，
 * 一旦这里没有定位，遮罩就会逃逸到视口，连顶部导航一起盖住，整个页面点不动。
 */
.kiosk {
  position: relative;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  min-height: 720px;
}

.kiosk-top {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-2);
}

.kiosk-title strong { display: block; font-size: 15px; }
.kiosk-title span { font-size: 12px; }

.flow { display: flex; gap: 4px; flex: 1; justify-content: center; }

.flow-node {
  font-size: 11px;
  padding: 3px 9px;
  border-radius: 999px;
  background: var(--surface);
  border: 1px solid var(--border);
  color: var(--text-3);
  white-space: nowrap;
}

.flow-node.done {
  background: var(--brand-soft);
  border-color: #bfdbfe;
  color: var(--brand-dark);
}

.kiosk-meta { display: flex; align-items: center; gap: 8px; }

.kiosk-body {
  flex: 1;
  display: grid;
  grid-template-columns: 1.35fr 1fr;
  min-height: 0;
}

/* ---------- 购物盘 ---------- */
.tray-area {
  padding: 16px 20px;
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 0;
}

.alert {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  border: 1px solid #fecaca;
}

.alert-text strong { font-size: 12px; color: #b91c1c; display: block; }
.alert-text span { font-size: 11px; }

/* 降级提示用警示色而非红色：不是故障，是结果不可信，需要说清楚 */
.alert.warn { background: var(--warn-soft); border-color: #fde68a; }
.alert.warn .alert-text strong { color: #b45309; }

.tray-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.tray-head h3 { font-size: 14px; }
.entry-count {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 999px;
  background: var(--brand-soft);
  color: var(--brand-dark);
  white-space: nowrap;
}
.tray-actions { display: flex; gap: 8px; }

.mode-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--surface);
  font-size: 12px;
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}
.mode-toggle.on { border-color: var(--brand); background: var(--brand-soft); color: var(--brand-dark); }
.mode-toggle input { accent-color: var(--brand); cursor: pointer; }

.weight-input {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--surface);
  font-size: 12px;
  white-space: nowrap;
}
.weight-input input {
  width: 92px;
  border: none;
  background: transparent;
  font-size: 13px;
  color: var(--text);
  outline: none;
}
.weight-input input::-webkit-outer-spin-button,
.weight-input input::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
.weight-input input[type=number] { -moz-appearance: textfield; }

.tray {
  position: relative;
  flex: 1;
  min-height: 300px;
  border-radius: var(--radius);
  background: linear-gradient(180deg, #f8fafc, #eef2f7);
  border: 1px solid var(--border);
  overflow: hidden;
}

.tray.scanning { border-color: var(--brand); }

.tray-plate {
  position: absolute;
  left: 4%;
  right: 4%;
  top: 18%;
  bottom: 10%;
  border-radius: 50%;
  background: #e2e8f0;
  box-shadow: inset 0 6px 20px rgba(15, 23, 42, 0.08);
}

.camera { width: 100%; height: 100%; object-fit: cover; }

.captured {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.hidden { display: none; }

.box {
  position: absolute;
  border-radius: 10px;
  border: 2px solid var(--success);
  background: rgba(16, 185, 129, 0.08);
  display: grid;
  place-items: center;
  transition: all 0.2s ease;
  animation: fade-up 0.3s ease;
}

.box.clarify { border-color: var(--warn); background: rgba(245, 158, 11, 0.1); }
.box.review { border-color: var(--danger); background: rgba(239, 68, 68, 0.1); border-style: dashed; }

.box-visual { pointer-events: none; }

.box-label {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  bottom: -9px;
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--success);
  color: #fff;
  font-size: 10px;
  white-space: nowrap;
}

.box.clarify .box-label { background: var(--warn); }
.box.review .box-label { background: var(--danger); }

.box-score {
  position: absolute;
  top: -9px;
  right: -6px;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--surface);
  border: 1px solid var(--border);
  font-size: 10px;
  color: var(--text-2);
}

.scan-line {
  position: absolute;
  left: 0;
  right: 0;
  height: 3px;
  background: linear-gradient(90deg, transparent, var(--brand), transparent);
  animation: scan 1.4s ease-in-out infinite;
}

/*
 * 纯提示文案，铺满父容器只是为了居中。
 * 加 pointer-events:none 作兜底：万一将来被放进没有定位的容器，
 * 最多是位置跑偏，不会再变成一块盖住全屏、让整页点不动的隐形挡板。
 */
.empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  margin: 0;
  font-size: 13px;
  pointer-events: none;
}

/* 场景数量会随数据增加，用自适应列数而非写死 3 列，避免第 4 个场景折行 */
.scenes { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; }

.scene {
  padding: 10px;
  text-align: left;
  border-radius: var(--radius-sm);
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.scene strong { font-size: 12px; }
.scene em { font-size: 11px; font-style: normal; }
.scene.active { border-color: var(--brand); background: var(--brand-soft); }

.weight {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  background: var(--success-soft);
  color: #047857;
  border: 1px solid #d1fae5;
}

.weight strong { font-size: 12px; }
.weight.suspicious_high, .weight.suspicious_low {
  background: var(--warn-soft);
  color: #b45309;
  border-color: #fde68a;
}

.small { font-size: 11px; margin: 0; }
.error { margin: 0; font-size: 12px; color: var(--danger); }

/* ---------- 账单 ---------- */
.bill-area {
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 0;
}

.bill-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.bill-head h3 { font-size: 14px; }
.bill-head-right { display: flex; align-items: center; gap: 8px; }

/* 同为定位祖先：空状态提示 .empty 是 absolute inset:0，需要被限制在本区域内 */
.bill-list {
  position: relative;
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 120px;
}

.bill-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  border: 1px solid var(--border);
}

.bill-main { flex: 1; min-width: 0; }
.bill-main strong { display: block; font-size: 12.5px; font-weight: 500; }
.bill-main em { font-size: 11px; font-style: normal; }
.row-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }

.bill-amount { text-align: right; display: flex; flex-direction: column; }
.bill-amount span { font-size: 13px; font-weight: 500; }
.strike { text-decoration: line-through; font-size: 11px; }

.qty-stepper {
  display: flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 1px 4px;
  background: var(--surface);
}
.qty-stepper button { min-width: 22px; height: 22px; padding: 0; line-height: 1; font-size: 14px; }
.qty-num { min-width: 18px; text-align: center; font-size: 12.5px; font-weight: 500; }

.review-box {
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  border: 1px solid #fecaca;
}

.review-box strong { font-size: 12px; color: #b91c1c; }
.review-box span { display: block; margin-top: 2px; font-size: 11px; }

.review-head { display: flex; flex-direction: column; gap: 2px; }

.review-item {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed #fecaca;
}

/* OCR 依据：展示模型从包装上读到的文字，让识别结论可验证 */
.evidence {
  margin: 0;
  font-size: 11px;
  color: #92400e;
  background: #fef3c7;
  border-radius: 4px;
  padding: 3px 8px;
}

.review-done {
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--success-soft);
  border: 1px solid #d1fae5;
}

.review-hint {
  color: #b91c1c;
  font-size: 11px;
  line-height: 1.4;
  margin: 0 0 8px;
}

.review-done strong { font-size: 12px; color: #047857; }
.review-done span { display: block; font-size: 11px; }

/* ---------- 人工指定商品 ---------- */
.pick-mask {
  position: absolute;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: grid;
  place-items: center;
  z-index: 25;
}

.pick-card {
  width: 520px;
  max-width: 90%;
  background: var(--surface);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
  animation: fade-up 0.25s ease;
}

.pick-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
}

.pick-head p { margin: 2px 0 0; font-size: 11px; }

.pick-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  padding: 14px 16px;
  max-height: 320px;
  overflow-y: auto;
}

.pick-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  padding: 8px 4px;
  text-align: center;
}

.pick-item:hover { border-color: var(--brand); background: var(--brand-soft); }

.pick-name {
  font-size: 11px;
  line-height: 1.3;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.pick-item em { font-size: 11px; font-style: normal; }

.bill-total { border-top: 1px dashed var(--border); padding-top: 10px; }

.total-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  padding: 3px 0;
}

.total-row.save { color: var(--danger); }
.save { color: var(--danger); }

.total-row.payable { margin-top: 6px; font-size: 14px; }
.total-row.payable strong { font-size: 22px; color: var(--danger); }

.pay { width: 100%; }

/* ---------- 追问 ---------- */
.clarify {
  flex: none;
  padding: 12px 20px;
  border-top: 1px solid var(--border);
  background: var(--warn-soft);
}

.clarify-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 8px; }
.clarify-head strong { font-size: 13px; color: #b45309; }

.clarify-list { display: flex; flex-direction: column; gap: 8px; max-height: 132px; overflow-y: auto; }

.clarify-item { display: flex; flex-direction: column; gap: 5px; }
.options { display: flex; flex-wrap: wrap; gap: 6px; }

.options button { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.options button em { font-style: normal; font-size: 11px; }

/* ---------- 支付成功 ---------- */
.paid-mask {
  position: absolute;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: grid;
  place-items: center;
  z-index: 20;
}

.paid-card {
  width: 320px;
  padding: 28px;
  border-radius: var(--radius-lg);
  background: var(--surface);
  text-align: center;
  box-shadow: var(--shadow-lg);
  animation: fade-up 0.3s ease;
}

.paid-icon {
  width: 54px;
  height: 54px;
  margin: 0 auto 12px;
  border-radius: 50%;
  background: var(--success-soft);
  color: var(--success);
  display: grid;
  place-items: center;
  font-size: 26px;
}

.paid-card h2 { font-size: 18px; margin-bottom: 4px; }
.paid-card p { margin: 0; font-size: 12px; }

.paid-amount {
  margin: 14px 0;
  font-size: 30px;
  font-weight: 600;
  color: var(--danger);
}

.paid-meta {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 18px;
  font-size: 12px;
}

.paid-meta div { display: flex; justify-content: space-between; }

.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

@media (max-width: 1180px) {
  .kiosk-body { grid-template-columns: 1fr; }
  .tray-area { border-right: none; border-bottom: 1px solid var(--border); }
}
</style>
