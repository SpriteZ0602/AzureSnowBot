<template>
  <el-container class="layout-container">
    <el-aside width="220px" class="aside">
      <div class="logo">
        <h2>❄️ SnowBot</h2>
      </div>
      <el-menu
        :default-active="activeMenu"
        router
        background-color="#1d1e1f"
        text-color="#bbb"
        active-text-color="#409eff"
      >
        <el-menu-item index="/">
          <el-icon><DataBoard /></el-icon>
          <span>总览</span>
        </el-menu-item>
        <el-menu-item index="/tokens">
          <el-icon><Coin /></el-icon>
          <span>Token 用量</span>
        </el-menu-item>
        <el-menu-item index="/conversations">
          <el-icon><ChatDotRound /></el-icon>
          <span>对话浏览器</span>
        </el-menu-item>
        <el-menu-item index="/memory">
          <el-icon><Notebook /></el-icon>
          <span>记忆管理</span>
        </el-menu-item>
        <el-menu-item index="/personas">
          <el-icon><User /></el-icon>
          <span>人格管理</span>
        </el-menu-item>
        <el-menu-item index="/reminders">
          <el-icon><AlarmClock /></el-icon>
          <span>提醒管理</span>
        </el-menu-item>
        <el-menu-item index="/skills">
          <el-icon><MagicStick /></el-icon>
          <span>技能管理</span>
        </el-menu-item>
        <el-menu-item index="/config">
          <el-icon><Setting /></el-icon>
          <span>配置编辑</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <span class="page-title">{{ pageTitle }}</span>
        <el-dropdown @command="handleCommand">
          <span class="user-info">
            {{ auth.username || 'admin' }}
            <el-icon><ArrowDown /></el-icon>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>

      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const activeMenu = computed(() => route.path)

const pageTitles: Record<string, string> = {
  '/': '总览',
  '/tokens': 'Token 用量',
  '/conversations': '对话浏览器',
  '/memory': '记忆管理',
  '/personas': '人格管理',
  '/reminders': '提醒管理',
  '/skills': '技能管理',
  '/config': '配置编辑',
}
const pageTitle = computed(() => pageTitles[route.path] || 'Dashboard')

function handleCommand(cmd: string) {
  if (cmd === 'logout') {
    auth.logout()
    router.push('/login')
  }
}
</script>

<style scoped>
.layout-container {
  height: 100vh;
}
.aside {
  background-color: #1d1e1f;
  overflow-y: auto;
}
.logo {
  padding: 16px 20px;
  color: #fff;
  border-bottom: 1px solid #333;
}
.logo h2 {
  margin: 0;
  font-size: 18px;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid #eee;
  background: #fff;
}
.page-title {
  font-size: 18px;
  font-weight: 600;
}
.user-info {
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 4px;
}
.main {
  background: #f5f7fa;
}
</style>
