<template>
  <div class="overview">
    <!-- 统计卡片 -->
    <el-row :gutter="20" class="stat-cards">
      <el-col :span="6">
        <el-card shadow="hover">
          <el-statistic title="今日 Token" :value="data.today_tokens" />
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <el-statistic title="今日调用" :value="data.today_calls" suffix="次" />
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <el-statistic title="活跃群聊" :value="data.active_groups.length" suffix="个" />
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <el-statistic title="待执行提醒" :value="data.reminder_count" suffix="个" />
        </el-card>
      </el-col>
    </el-row>

    <!-- 运行时间 -->
    <el-card shadow="hover" class="uptime-card">
      <template #header>Bot 运行状态</template>
      <p>运行时间：{{ formatUptime(data.uptime_seconds) }}</p>
    </el-card>

    <!-- 今日 Token 来源分布 -->
    <el-card shadow="hover" class="section-card" v-if="Object.keys(data.today_stats).length">
      <template #header>今日 Token 来源分布</template>
      <el-table :data="sourceTableData" stripe size="small">
        <el-table-column prop="source" label="来源" width="150" />
        <el-table-column prop="prompt" label="输入 Token" />
        <el-table-column prop="completion" label="输出 Token" />
        <el-table-column prop="total" label="总计" />
        <el-table-column prop="calls" label="调用次数" width="100" />
      </el-table>
    </el-card>

    <!-- 最近工具调用 -->
    <el-card shadow="hover" class="section-card">
      <template #header>最近工具调用</template>
      <el-table :data="data.recent_tool_calls" stripe size="small">
        <el-table-column prop="ts" label="时间" width="180">
          <template #default="{ row }">{{ formatTime(row.ts) }}</template>
        </el-table-column>
        <el-table-column prop="source" label="来源" width="100" />
        <el-table-column prop="tool" label="工具" width="200" />
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.ok ? 'success' : 'danger'" size="small">
              {{ row.ok ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="result_len" label="结果长度" width="100" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, computed } from 'vue'
import api from '../api'

const data = reactive({
  uptime_seconds: 0,
  today_tokens: 0,
  today_calls: 0,
  today_stats: {} as Record<string, any>,
  active_groups: [] as any[],
  reminder_count: 0,
  recent_tool_calls: [] as any[],
})

const sourceTableData = computed(() =>
  Object.entries(data.today_stats).map(([source, s]: [string, any]) => ({
    source,
    prompt: s.prompt?.toLocaleString() ?? 0,
    completion: s.completion?.toLocaleString() ?? 0,
    total: s.total?.toLocaleString() ?? 0,
    calls: s.calls ?? 0,
  }))
)

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  return `${h}小时 ${m}分 ${s}秒`
}

function formatTime(ts: string): string {
  if (!ts) return ''
  return ts.replace('T', ' ').slice(0, 19)
}

onMounted(async () => {
  try {
    const { data: d } = await api.get('/overview')
    Object.assign(data, d)
  } catch {
    // ignore
  }
})
</script>

<style scoped>
.stat-cards {
  margin-bottom: 20px;
}
.uptime-card {
  margin-bottom: 20px;
}
.section-card {
  margin-bottom: 20px;
}
</style>
