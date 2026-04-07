<template>
  <div class="tokens-page">
    <!-- 日期范围选择 -->
    <el-card shadow="hover" class="filter-card">
      <el-row :gutter="16" align="middle">
        <el-col :span="6">
          <el-select v-model="days" @change="loadData" style="width: 100%">
            <el-option :value="7" label="最近 7 天" />
            <el-option :value="14" label="最近 14 天" />
            <el-option :value="30" label="最近 30 天" />
            <el-option :value="90" label="最近 90 天" />
          </el-select>
        </el-col>
      </el-row>
    </el-card>

    <!-- 每日趋势图 -->
    <el-card shadow="hover" class="chart-card">
      <template #header>每日 Token 用量趋势</template>
      <v-chart :option="dailyChartOption" autoresize style="height: 350px" />
    </el-card>

    <!-- 今日来源分布 -->
    <el-row :gutter="20">
      <el-col :span="12">
        <el-card shadow="hover" class="chart-card">
          <template #header>来源分布 (Token)</template>
          <v-chart :option="sourceChartOption" autoresize style="height: 300px" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="hover" class="chart-card">
          <template #header>费用趋势 (USD)</template>
          <v-chart :option="costChartOption" autoresize style="height: 300px" />
        </el-card>
      </el-col>
    </el-row>

    <!-- 费用明细表 -->
    <el-card shadow="hover" class="chart-card">
      <template #header>费用明细</template>
      <el-table :data="costData" stripe size="small">
        <el-table-column prop="date" label="日期" width="120" />
        <el-table-column label="输入 Token">
          <template #default="{ row }">{{ row.prompt_tokens.toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="输出 Token">
          <template #default="{ row }">{{ row.completion_tokens.toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="费用 (USD)">
          <template #default="{ row }">${{ row.cost_usd.toFixed(4) }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, PieChart, BarChart } from 'echarts/charts'
import {
  TitleComponent, TooltipComponent, LegendComponent,
  GridComponent, DataZoomComponent,
} from 'echarts/components'
import api from '../api'

use([
  CanvasRenderer, LineChart, PieChart, BarChart,
  TitleComponent, TooltipComponent, LegendComponent,
  GridComponent, DataZoomComponent,
])

const days = ref(30)
const dailyData = ref<any[]>([])
const sourceData = ref<Record<string, any>>({})
const costData = ref<any[]>([])

async function loadData() {
  const [daily, source, cost] = await Promise.all([
    api.get('/tokens/daily', { params: { days: days.value } }),
    api.get('/tokens/by-source'),
    api.get('/tokens/cost', { params: { days: days.value } }),
  ])
  dailyData.value = daily.data
  sourceData.value = source.data.sources || {}
  costData.value = cost.data
}

const dailyChartOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  legend: { data: ['输入', '输出'] },
  grid: { left: 60, right: 30, bottom: 60 },
  dataZoom: [{ type: 'slider', start: 0, end: 100 }],
  xAxis: {
    type: 'category',
    data: dailyData.value.map((d: any) => d.date),
  },
  yAxis: { type: 'value', name: 'Tokens' },
  series: [
    {
      name: '输入',
      type: 'line',
      data: dailyData.value.map((d: any) => d.prompt),
      smooth: true,
      areaStyle: { opacity: 0.2 },
    },
    {
      name: '输出',
      type: 'line',
      data: dailyData.value.map((d: any) => d.completion),
      smooth: true,
      areaStyle: { opacity: 0.2 },
    },
  ],
}))

const sourceChartOption = computed(() => ({
  tooltip: { trigger: 'item' },
  series: [{
    type: 'pie',
    radius: ['40%', '70%'],
    data: Object.entries(sourceData.value).map(([name, s]: [string, any]) => ({
      name,
      value: s.total || 0,
    })),
    emphasis: {
      itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.3)' },
    },
  }],
}))

const costChartOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  grid: { left: 60, right: 30, bottom: 30 },
  xAxis: {
    type: 'category',
    data: costData.value.map((d: any) => d.date),
  },
  yAxis: { type: 'value', name: 'USD' },
  series: [{
    type: 'bar',
    data: costData.value.map((d: any) => d.cost_usd),
    itemStyle: { color: '#67c23a' },
  }],
}))

onMounted(loadData)
</script>

<style scoped>
.filter-card {
  margin-bottom: 20px;
}
.chart-card {
  margin-bottom: 20px;
}
</style>
