import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '@/views/HomeView.vue'
import CheckoutView from '@/views/CheckoutView.vue'
import GuideView from '@/views/GuideView.vue'
import AdminView from '@/views/AdminView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView },
    { path: '/guide', name: 'guide', component: GuideView },
    { path: '/checkout', name: 'checkout', component: CheckoutView },
    // 后台看板：默认隐藏入口（顶栏不放 Tab），通过 URL 直访。
    // 进入即要求输入 token（dev 模式 ADMIN_TOKEN 为空时跳过校验）
    { path: '/admin', name: 'admin', component: AdminView },
  ],
})

export default router
