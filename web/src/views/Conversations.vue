<template>
  <div class="conversations-page">
    <el-row :gutter="20" style="height: calc(100vh - 160px)">
      <!-- 左侧会话列表 -->
      <el-col :span="8">
        <el-card shadow="hover" class="session-list" body-style="padding: 0">
          <template #header>会话列表</template>
          <!-- Admin 私聊 -->
          <div
            class="session-item"
            :class="{ active: activeSession === 'admin' }"
            @click="selectAdmin"
          >
            <el-icon><User /></el-icon>
            <span>Admin 私聊</span>
          </div>
          <!-- 群聊 -->
          <div
            v-for="g in groups"
            :key="g.group_id"
            class="session-item"
            :class="{ active: activeSession === g.group_id }"
            @click="selectGroup(g)"
          >
            <el-icon><ChatDotRound /></el-icon>
            <div class="session-info">
              <span>群 {{ g.group_id }}</span>
              <small>{{ g.active_persona }} · {{ g.last_message_at || '无记录' }}</small>
            </div>
          </div>
        </el-card>
      </el-col>

      <!-- 右侧消息区 -->
      <el-col :span="16">
        <el-card shadow="hover" class="message-area" body-style="padding: 0; display: flex; flex-direction: column; height: 100%;">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center;">
              <span>{{ activeTitle }}</span>
              <el-button size="small" @click="loadMore" :disabled="!hasMore" v-if="messages.length">
                加载更多
              </el-button>
            </div>
          </template>
          <div ref="msgContainer" class="messages-container">
            <div
              v-for="(msg, i) in displayMessages"
              :key="i"
              class="message-bubble"
              :class="msg.role"
            >
              <div class="bubble-label">{{ msg.role === 'user' ? '用户' : 'Bot' }}</div>
              <div class="bubble-content">{{ msg.content }}</div>
            </div>
            <el-empty v-if="!messages.length" description="选择一个会话查看消息" />
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import api from '../api'

const groups = ref<any[]>([])
const activeSession = ref('')
const activeTitle = ref('消息')
const messages = ref<any[]>([])
const page = ref(1)
const total = ref(0)
const pageSize = 50

const hasMore = computed(() => messages.value.length < total.value)
const displayMessages = computed(() => [...messages.value].reverse())

async function loadGroups() {
  const { data } = await api.get('/conversations/groups')
  groups.value = data
}

async function selectAdmin() {
  activeSession.value = 'admin'
  activeTitle.value = 'Admin 私聊'
  page.value = 1
  messages.value = []
  const { data } = await api.get('/conversations/admin', {
    params: { page: 1, size: pageSize },
  })
  messages.value = data.messages
  total.value = data.total
}

async function selectGroup(g: any) {
  activeSession.value = g.group_id
  activeTitle.value = `群 ${g.group_id} (${g.active_persona})`
  page.value = 1
  messages.value = []
  const { data } = await api.get(`/conversations/groups/${g.group_id}`, {
    params: { persona: g.active_persona, page: 1, size: pageSize },
  })
  messages.value = data.messages
  total.value = data.total
}

async function loadMore() {
  page.value++
  if (activeSession.value === 'admin') {
    const { data } = await api.get('/conversations/admin', {
      params: { page: page.value, size: pageSize },
    })
    messages.value.push(...data.messages)
  } else {
    const g = groups.value.find((g) => g.group_id === activeSession.value)
    if (!g) return
    const { data } = await api.get(`/conversations/groups/${g.group_id}`, {
      params: { persona: g.active_persona, page: page.value, size: pageSize },
    })
    messages.value.push(...data.messages)
  }
}

onMounted(loadGroups)
</script>

<style scoped>
.session-list {
  height: 100%;
  overflow-y: auto;
}
.session-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  cursor: pointer;
  border-bottom: 1px solid #f0f0f0;
  transition: background 0.2s;
}
.session-item:hover {
  background: #f5f7fa;
}
.session-item.active {
  background: #ecf5ff;
  color: #409eff;
}
.session-info {
  display: flex;
  flex-direction: column;
}
.session-info small {
  color: #999;
  font-size: 12px;
}
.message-area {
  height: 100%;
}
.messages-container {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}
.message-bubble {
  margin-bottom: 12px;
  max-width: 80%;
}
.message-bubble.user {
  margin-left: auto;
  text-align: right;
}
.message-bubble.assistant {
  margin-right: auto;
}
.bubble-label {
  font-size: 12px;
  color: #999;
  margin-bottom: 4px;
}
.bubble-content {
  display: inline-block;
  padding: 10px 14px;
  border-radius: 12px;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 14px;
  line-height: 1.5;
}
.user .bubble-content {
  background: #409eff;
  color: #fff;
}
.assistant .bubble-content {
  background: #f0f0f0;
  color: #333;
}
</style>
