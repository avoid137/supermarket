<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, RouterView } from 'vue-router'
import { api } from '@/api'
import type { HealthInfo } from '@/types'

const health = ref<HealthInfo | null>(null)
const offline = ref(false)

onMounted(async () => {
  try {
    health.value = await api.health()
  } catch {
    offline.value = true
  }
})
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <div class="logo">智</div>
        <div class="brand-text">
          <h1>智选无人超市</h1>
          <p class="dim tagline">智能导购与自适应视觉结账一体化系统</p>
        </div>
      </div>

      <nav class="tabs">
        <RouterLink to="/" class="tab" exact-active-class="active-home">
          <span class="badge">H</span>
          <span class="tab-label">商城</span>
        </RouterLink>
        <RouterLink to="/guide" class="tab">
          <span class="badge">A</span>
          <span class="tab-label">智能导购</span>
        </RouterLink>
        <RouterLink to="/checkout" class="tab checkout-tab">
          <span class="badge">B</span>
          <span class="tab-label">视觉结账</span>
        </RouterLink>
      </nav>

      <div class="status">
        <template v-if="offline">
          <span class="tag danger">后端未启动</span>
        </template>
        <template v-else-if="health">
          <span class="tag" :class="health.llm_enabled ? 'success' : 'warn'">
            导购：{{ health.llm_enabled ? health.llm_model : '本地知识库' }}
          </span>
          <span
            class="tag"
            :class="health.vision_enabled ? 'success' : 'warn'"
            :title="health.vision_error || '视觉模型未启用，拍照将使用内置示例数据'"
          >
            视觉：{{ health.vision_enabled ? health.vision_model : '模拟识别' }}
          </span>
        </template>
      </div>
    </header>

    <main class="content">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.shell {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.topbar {
  flex: none;
  height: 62px;
  padding: 0 24px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.logo {
  width: 34px;
  height: 34px;
  flex: none;
  border-radius: 10px;
  background: var(--brand);
  color: #fff;
  display: grid;
  place-items: center;
  font-weight: 600;
  font-size: 16px;
}

.brand-text { min-width: 0; }
.brand h1 {
  font-size: 15px;
  line-height: 1.2;
}
.brand .tagline {
  margin: 2px 0 0;
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tabs {
  display: flex;
  gap: 4px;
  background: var(--surface-2);
  padding: 4px;
  border-radius: 10px;
  border: 1px solid var(--border);
}

.tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: 7px;
  font-size: 13px;
  color: var(--text-2);
  text-decoration: none;
  transition: all 0.15s ease;
}

.tab:hover {
  color: var(--text);
}

.tab.router-link-active,
.tab.active-home {
  background: var(--surface);
  color: var(--text);
  font-weight: 500;
  box-shadow: var(--shadow-sm);
}

.badge {
  width: 18px;
  height: 18px;
  flex: none;
  border-radius: 5px;
  background: var(--brand-soft);
  color: var(--brand-dark);
  font-size: 11px;
  display: grid;
  place-items: center;
  font-weight: 600;
}

.tab.router-link-active .badge,
.tab.active-home .badge {
  background: var(--brand);
  color: #fff;
}

.status {
  display: flex;
  gap: 6px;
  flex: none;
}

.content {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

/* ---------- 窄屏（手机）适配 ---------- */
@media (max-width: 700px) {
  .topbar {
    height: 56px;
    padding: 0 12px;
    gap: 8px;
  }
  .brand .tagline,
  .status,
  .tabs .badge { display: none; }
  /* 手机端隐藏「视觉结账」入口：现场视觉结账台是独立横屏硬件，
     顾客手机里只看商品和导购；点这个 Tab 容易让 demo 演示与手机混淆 */
  .tabs .checkout-tab { display: none; }
  .tabs { padding: 3px; gap: 2px; }
  .tab {
    padding: 6px 10px;
    font-size: 12px;
  }
  .brand h1 { font-size: 13px; }
}
</style>
