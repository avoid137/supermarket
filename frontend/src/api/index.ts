import type {
  AgentEvent,
  AuditDetail,
  AuditItem,
  Bestseller,
  ChatTurn,
  HealthInfo,
  InventoryAlerts,
  OverviewResponse,
  Product,
  RecognizeResponse,
  SalesTrendPoint,
  SceneInfo,
  SessionSnapshot,
} from '@/types'

const BASE = '/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!resp.ok) {
    const detail = await resp.text().catch(() => '')
    throw new Error(`请求失败 ${resp.status}：${detail || resp.statusText}`)
  }
  return resp.json() as Promise<T>
}

export const api = {
  health: () => request<HealthInfo>('/health'),

  products: (keyword = '') =>
    request<Product[]>(`/catalog/products${keyword ? `?keyword=${encodeURIComponent(keyword)}` : ''}`),

  /** 按 SKU ID 取单个商品详情（手动输入兜底用） */
  getProduct: (skuId: string) =>
    request<Product>(`/catalog/products/${encodeURIComponent(skuId)}`),

  /** 按条形码查商品（扫码接口）。未找到时抛错含 404 detail。 */
  lookupByBarcode: (barcode: string) =>
    request<Product>(
      `/catalog/sku?barcode=${encodeURIComponent(barcode)}`,
    ),

  /** 搭配推荐：扫码后自动给顾客开场白用。 */
  getRecommendations: (skuId: string, topK = 3) =>
    request<Product[]>(
      `/catalog/products/${encodeURIComponent(skuId)}/recommendations?top_k=${topK}`,
    ),

  scenes: () => request<SceneInfo[]>('/checkout/scenes'),

  createSession: () => request<SessionSnapshot>('/checkout/sessions', { method: 'POST' }),

  getSession: (id: string) => request<SessionSnapshot>(`/checkout/sessions/${id}`),

  recognize: (id: string, payload: {
    scene_id?: string | null
    image_base64?: string | null
    /** 连拍多帧，后端据此做投票聚合；传了它 image_base64 会被忽略 */
    images?: string[] | null
    tray_weight_g?: number | null
    use_vision_model: boolean
    /** 逐件录入：本次结果追加到已有清单而非整体替换 */
    append?: boolean
  }) =>
    request<RecognizeResponse>(`/checkout/sessions/${id}/recognize`, {
      method: 'POST',
      body: JSON.stringify({ ...payload, append: payload.append ?? false }),
    }),

  clarify: (id: string, boxId: string, skuId: string) =>
    request<SessionSnapshot>(`/checkout/sessions/${id}/clarify`, {
      method: 'POST',
      body: JSON.stringify({ box_id: boxId, sku_id: skuId }),
    }),

  /** 识别失败或漏检时，不依赖检测框直接补录一件商品。 */
  addManualItem: (id: string, skuId: string, quantity = 1) =>
    request<SessionSnapshot>(`/checkout/sessions/${id}/manual`, {
      method: 'POST',
      body: JSON.stringify({ sku_id: skuId, quantity }),
    }),

  /** 逐件录入：调整某条目数量（同款多件无需反复拍摄）。 */
  adjustQuantity: (id: string, boxId: string, delta: number) =>
    request<SessionSnapshot>(`/checkout/sessions/${id}/items/${boxId}/quantity`, {
      method: 'PATCH',
      body: JSON.stringify({ delta }),
    }),

  confirm: (id: string, member = true) =>
    request<SessionSnapshot>(`/checkout/sessions/${id}/confirm?member=${member}`, { method: 'POST' }),

  pay: (id: string, method = 'wechat') =>
    request<SessionSnapshot>(`/checkout/sessions/${id}/pay`, {
      method: 'POST',
      body: JSON.stringify({ method }),
    }),

  reset: (id: string) => request<SessionSnapshot>(`/checkout/sessions/${id}/reset`, { method: 'POST' }),

  /** 按 box_id 移除一笔检测项（待确认 / 人工补录 / 已入账皆可），用于纠正重复或误识别。 */
  removeItem: (id: string, boxId: string) =>
    request<SessionSnapshot>(`/checkout/sessions/${id}/items/${encodeURIComponent(boxId)}`, {
      method: 'DELETE',
    }),

  // ---------------- 后台看板 ----------------
  // token 存 localStorage；undefined 表示未设置，不发送 Authorization
  adminOverview: (days: number, token?: string) =>
    request<OverviewResponse>(
      `/admin/overview?days=${days}`,
      token ? { headers: { Authorization: `Bearer ${token}` } } : {},
    ),
  adminBestsellers: (days: number, limit: number, token?: string) =>
    request<Bestseller[]>(
      `/admin/bestsellers?days=${days}&limit=${limit}`,
      token ? { headers: { Authorization: `Bearer ${token}` } } : {},
    ),
  adminInventoryAlerts: (token?: string) =>
    request<InventoryAlerts>(
      `/admin/inventory/alerts`,
      token ? { headers: { Authorization: `Bearer ${token}` } } : {},
    ),
  adminSalesTrend: (days: number, token?: string) =>
    request<SalesTrendPoint[]>(
      `/admin/sales/trend?days=${days}`,
      token ? { headers: { Authorization: `Bearer ${token}` } } : {},
    ),
  adminChatsRecent: (limit: number, token?: string) =>
    request<ChatTurn[]>(
      `/admin/chats/recent?limit=${limit}`,
      token ? { headers: { Authorization: `Bearer ${token}` } } : {},
    ),

  // ---------------- 审计追溯 ----------------
  adminAuditList: (params: { days: number; q?: string; cleared?: 'all' | 'yes' | 'no'; limit?: number; offset?: number }, token?: string) => {
    const qs = new URLSearchParams({
      days: String(params.days),
      cleared: params.cleared ?? 'all',
      limit: String(params.limit ?? 20),
      offset: String(params.offset ?? 0),
    })
    if (params.q) qs.set('q', params.q)
    return request<{ total: number; window_days: number; items: AuditItem[] }>(
      `/admin/audit?${qs.toString()}`,
      token ? { headers: { Authorization: `Bearer ${token}` } } : {},
    )
  },
  adminAuditDetail: (orderId: string, token?: string) =>
    request<AuditDetail>(
      `/admin/audit/${encodeURIComponent(orderId)}`,
      token ? { headers: { Authorization: `Bearer ${token}` } } : {},
    ),
  adminAuditClear: (orderId: string, operator: string, note: string, token?: string) =>
    request<AuditDetail>(
      `/admin/audit/${encodeURIComponent(orderId)}/clear`,
      {
        method: 'POST',
        body: JSON.stringify({ operator, note }),
        ...(token ? { headers: { Authorization: `Bearer ${token}` } } : {}),
      },
    ),
  adminAuditDelete: (orderId: string, token?: string) =>
    request<{ deleted: boolean; order_id: string }>(
      `/admin/audit/${encodeURIComponent(orderId)}`,
      {
        method: 'DELETE',
        ...(token ? { headers: { Authorization: `Bearer ${token}` } } : {}),
      },
    ),

  /** 直连后台原图二进制（<img src> 用），无需 tokenArg 拼接 */
  adminAuditPhotoUrl: (orderId: string, token?: string) => {
    const url = `${BASE}/admin/audit/${encodeURIComponent(orderId)}/photo`
    return token ? `${url}?token=${encodeURIComponent(token)}` : url
  },
}

/** 导购问答：SSE 流式读取，每个事件回调一次。 */
export async function askAgent(
  payload: { question: string; history: { role: string; content: string }[]; use_llm: boolean },
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`${BASE}/agent/ask`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  })
  if (!resp.ok || !resp.body) {
    throw new Error(`导购服务不可用（${resp.status}）`)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const chunks = buffer.split('\n\n')
    buffer = chunks.pop() ?? ''
    for (const chunk of chunks) {
      const line = chunk.trim()
      if (!line.startsWith('data:')) continue
      try {
        onEvent(JSON.parse(line.slice(5).trim()) as AgentEvent)
      } catch {
        // 忽略无法解析的心跳行
      }
    }
  }
}
