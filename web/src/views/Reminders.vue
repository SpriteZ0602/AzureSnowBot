<template>
  <div class="reminders-page">
    <el-card shadow="hover">
      <template #header>提醒列表</template>
      <el-empty v-if="!reminders.length" description="暂无提醒" />
      <el-table v-else :data="reminders" stripe>
        <el-table-column prop="id" label="ID" width="100" />
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            <el-tag :type="row.recurring === 'daily' ? 'warning' : 'info'" size="small">
              {{ row.recurring === 'daily' ? '每日' : '一次' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="目标" width="120">
          <template #default="{ row }">
            {{ row.chat_type === 'group' ? `群${row.target_id}` : '私聊' }}
          </template>
        </el-table-column>
        <el-table-column prop="creator_name" label="创建者" width="100" />
        <el-table-column prop="message" label="内容" show-overflow-tooltip />
        <el-table-column label="触发时间" width="180">
          <template #default="{ row }">
            {{ row.recurring === 'daily' ? `每天 ${row.daily_time}` : formatTime(row.fire_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button type="danger" size="small" text @click="cancelReminder(row.id)">
              取消
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import api from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

const reminders = ref<any[]>([])

async function loadReminders() {
  const { data } = await api.get('/reminders')
  reminders.value = data
}

function formatTime(iso: string): string {
  if (!iso) return ''
  return iso.replace('T', ' ').slice(0, 16)
}

async function cancelReminder(id: string) {
  await ElMessageBox.confirm('确认取消该提醒？', '取消确认', { type: 'warning' })
  try {
    await api.delete(`/reminders/${id}`)
    ElMessage.success('已取消')
    loadReminders()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '取消失败')
  }
}

onMounted(loadReminders)
</script>
