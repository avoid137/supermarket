export interface Nutrition {
  energy_kj: number
  protein_g: number
  fat_g: number
  carb_g: number
  sodium_mg: number
}

export interface ShelfLocation {
  aisle: string
  level: number
  desc: string
}

export interface Promotion {
  type: string
  desc: string
  with_skus: string[]
  groups: string[][]
  bundle_price: number | null
  threshold_amount: number | null
  discount: number | null
}

export interface VisualProfile {
  color: string
  color2: string
  shape: string
  label: string
  weight_g: number
}

export interface Product {
  sku_id: string
  name: string
  brand: string
  category: string
  spec: string
  price: number
  member_price: number
  shelf: ShelfLocation
  nutrition: Nutrition
  ingredients: string
  allergens: string[]
  tags: string[]
  promotions: Promotion[]
  visual: VisualProfile
  stock: number
}

export interface Candidate {
  sku_id: string
  name: string
  score: number
}

export type DetectionState = 'auto' | 'clarify' | 'review'

export interface Detection {
  box_id: string
  x: number
  y: number
  w: number
  h: number
  candidates: Candidate[]
  quantity: number
  state: DetectionState
  message: string
  /** 识别依据：模型从包装上读到的文字，读不到时为 null */
  evidence: string | null
}

export interface WeightCheck {
  status: 'ok' | 'suspicious_high' | 'suspicious_low'
  tray_weight_g: number
  expected_weight_g: number
  diff_g: number
  tolerance_g: number
  message: string
}

export interface RecognizeResponse {
  session_id: string
  mode: string
  detections: Detection[]
  need_clarify: boolean
  need_review: boolean
  weight_check: WeightCheck | null
  elapsed_ms: number
  /** 参与投票的帧数，1 表示单帧识别 */
  frames: number
}

export interface CheckoutItem {
  sku_id: string
  name: string
  spec: string
  unit_price: number
  quantity: number
  subtotal: number
  discount: number
  source: string
  confidence: number
  promotions: string[]
}

export interface Bill {
  session_id: string
  items: CheckoutItem[]
  total_quantity: number
  origin_amount: number
  discount_amount: number
  payable: number
}

export type SessionState =
  | 'CREATED'
  | 'CAPTURING'
  | 'RECOGNIZING'
  | 'NEED_CLARIFY'
  | 'REVIEWING'
  | 'CONFIRMED'
  | 'PAYING'
  | 'PAID'
  | 'FAILED'

export interface SessionSnapshot {
  session_id: string
  state: SessionState
  detections: Detection[]
  bill: Bill | null
  mode: string
  created_at: number
  resolved: Record<string, string>
  member: boolean
  tray_weight_g: number | null
  weight_check: WeightCheck | null
  paid_method: string | null
}

export interface SceneInfo {
  scene_id: string
  title: string
  desc: string
  item_count: number
  tray_weight_g: number
}

export interface Citation {
  sku_id: string
  name: string
  reason: string
}

export type AgentEvent =
  | { type: 'meta'; mode: string; intent: string }
  | { type: 'citations'; items: Citation[] }
  | { type: 'delta'; text: string }
  | { type: 'notice'; text: string }
  | { type: 'done'; latency_ms: number }

export interface HealthInfo {
  status: string
  service: string
  product_count: number
  llm_enabled: boolean
  llm_model: string | null
  vision_enabled: boolean
  vision_model: string | null
  /** 视觉模型最近一次真实调用的报错，用于排查「为什么在走模拟识别」 */
  vision_error: string | null
}

// ---------------- 后台看板 ----------------

export interface OverviewResponse {
  window_days: number
  orders: number
  revenue: number
  avg_basket: number
  items_sold: number
  customers: number
  delta: { orders_pct: number | null; revenue_pct: number | null }
}

export interface Bestseller {
  sku_id: string
  name: string
  qty_sold: number
  revenue: number
}

export interface InventoryAlert {
  sku_id: string
  name: string
  category: string
  stock: number
  price: number
}

export interface InventoryAlerts {
  sold_out: InventoryAlert[]
  low_stock: InventoryAlert[]
  threshold: number
}

export interface SalesTrendPoint {
  date: string
  revenue: number
  orders: number
}

export interface ChatTurn {
  session_id: string | null
  intent: string
  mode: string
  question: string
  answer: string
  citations: Citation[]
  created_at: number
}

// ---------------- 审计追溯 ----------------

export interface AuditItem {
  order_id: string
  session_id: string
  captured_at: number
  created_at: number
  expires_at: number
  bill_payable: number
  bill_origin: number
  bill_discount: number
  pay_method: string | null
  recognize_mode: string
  scene_id: string | null
  has_photo: boolean
  items_count: number
  cleared: boolean
  cleared_at: number | null
  cleared_by: string | null
}

export interface AuditItemRow {
  sku_id: string
  name: string
  spec: string
  quantity: number
  unit_price: number
  subtotal: number
  source: string
  confidence: number
  promotions: string[]
}

export interface AuditDetail extends AuditItem {
  items_snapshot: AuditItemRow[]
  clear_note: string
  /** 缩略图 base64（photo_path 为空时为空串），原图走 /photo 接口拉 */
  photo_inline_b64: string
}
