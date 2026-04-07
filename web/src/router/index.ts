import { createRouter, createWebHashHistory } from 'vue-router'

const router = createRouter({
  history: createWebHashHistory('/dashboard/'),
  routes: [
    {
      path: '/login',
      name: 'Login',
      component: () => import('../views/Login.vue'),
      meta: { noAuth: true },
    },
    {
      path: '/',
      component: () => import('../layout/MainLayout.vue'),
      children: [
        { path: '', name: 'Overview', component: () => import('../views/Overview.vue') },
        { path: 'tokens', name: 'Tokens', component: () => import('../views/Tokens.vue') },
        { path: 'conversations', name: 'Conversations', component: () => import('../views/Conversations.vue') },
        { path: 'memory', name: 'Memory', component: () => import('../views/Memory.vue') },
        { path: 'personas', name: 'Personas', component: () => import('../views/Personas.vue') },
        { path: 'reminders', name: 'Reminders', component: () => import('../views/Reminders.vue') },
        { path: 'skills', name: 'Skills', component: () => import('../views/Skills.vue') },
        { path: 'config', name: 'Config', component: () => import('../views/Config.vue') },
      ],
    },
  ],
})

// 路由守卫
router.beforeEach((to) => {
  const token = localStorage.getItem('access_token')
  if (!to.meta.noAuth && !token) {
    return '/login'
  }
})

export default router
