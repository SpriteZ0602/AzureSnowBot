<template>
  <div class="memory-page">
    <!-- 范围选择 -->
    <el-card shadow="hover" class="scope-card">
      <el-row :gutter="16" align="middle">
        <el-col :span="6">
          <el-select v-model="scope" @change="onScopeChange" style="width: 100%">
            <el-option
              v-for="s in scopes"
              :key="s.id"
              :value="s.id"
              :label="s.label"
            >
              {{ s.label }}
              <el-tag v-if="!s.exists" size="small" type="info" style="margin-left: 8px">空</el-tag>
            </el-option>
          </el-select>
        </el-col>
      </el-row>
    </el-card>

    <el-row :gutter="20">
      <!-- 编辑器 -->
      <el-col :span="14">
        <el-card shadow="hover">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center;">
              <span>MEMORY.md — {{ scopeLabel }}</span>
              <el-button type="primary" @click="saveMemory" :loading="saving">保存</el-button>
            </div>
          </template>
          <el-input
            v-model="content"
            type="textarea"
            :autosize="{ minRows: 20, maxRows: 40 }"
            placeholder="记忆内容..."
          />
        </el-card>
      </el-col>

      <!-- 搜索面板（仅 admin） -->
      <el-col :span="10">
        <el-card v-if="scope === 'admin'" shadow="hover" class="search-card">
          <template #header>语义搜索</template>
          <el-input
            v-model="searchQuery"
            placeholder="输入查询..."
            @keyup.enter="doSearch"
            clearable
          >
            <template #append>
              <el-button @click="doSearch" :loading="searching">搜索</el-button>
            </template>
          </el-input>

          <div v-if="searchResults.length" class="search-results">
            <div v-for="(r, i) in searchResults" :key="i" class="result-item">
              <div class="result-header">
                <el-tag size="small">{{ r.source }}</el-tag>
                <span class="score">{{ (r.score * 100).toFixed(1) }}%</span>
              </div>
              <p class="result-text">{{ r.text }}</p>
            </div>
          </div>
          <el-empty v-else-if="searched" description="没有找到相关记忆" />
        </el-card>

        <!-- 索引状态（仅 admin） -->
        <el-card v-if="scope === 'admin'" shadow="hover" class="index-card">
          <template #header>索引状态</template>
          <el-descriptions :column="1" size="small">
            <el-descriptions-item label="状态">
              <el-tag :type="indexStatus.exists ? 'success' : 'info'" size="small">
                {{ indexStatus.exists ? '已建立' : '未建立' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="分块数">{{ indexStatus.chunks }}</el-descriptions-item>
          </el-descriptions>
        </el-card>

        <el-card v-if="scope !== 'admin'" shadow="hover">
          <el-empty description="群记忆暂不支持语义搜索" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import api from '../api'
import { ElMessage } from 'element-plus'

const scopes = ref<any[]>([])
const scope = ref('admin')
const content = ref('')
const saving = ref(false)
const searchQuery = ref('')
const searching = ref(false)
const searched = ref(false)
const searchResults = ref<any[]>([])
const indexStatus = ref({ exists: false, chunks: 0 })

const scopeLabel = computed(() => {
  const s = scopes.value.find((s) => s.id === scope.value)
  return s?.label || scope.value
})

async function loadScopes() {
  const { data } = await api.get('/memory/scopes')
  scopes.value = data
}

async function loadMemory() {
  const { data } = await api.get('/memory/content', { params: { scope: scope.value } })
  content.value = data.content
}

function onScopeChange() {
  loadMemory()
  searchResults.value = []
  searched.value = false
  if (scope.value === 'admin') loadIndexStatus()
}

async function saveMemory() {
  saving.value = true
  try {
    await api.put('/memory/content', { content: content.value }, { params: { scope: scope.value } })
    ElMessage.success('保存成功')
    if (scope.value === 'admin') loadIndexStatus()
    loadScopes()
  } catch {
    ElMessage.error('保存失败')
  } finally {
    saving.value = false
  }
}

async function doSearch() {
  if (!searchQuery.value.trim()) return
  searching.value = true
  searched.value = true
  try {
    const { data } = await api.post('/memory/search', {
      query: searchQuery.value,
      max_results: 10,
    })
    searchResults.value = data.results
  } catch {
    ElMessage.error('搜索失败')
  } finally {
    searching.value = false
  }
}

async function loadIndexStatus() {
  try {
    const { data } = await api.get('/memory/index-status')
    indexStatus.value = data
  } catch {
    // ignore
  }
}

onMounted(() => {
  loadScopes()
  loadMemory()
  loadIndexStatus()
})
</script>

<style scoped>
.search-card {
  margin-bottom: 16px;
}
.index-card {
  margin-top: 16px;
}
.search-results {
  margin-top: 16px;
}
.result-item {
  padding: 12px;
  border: 1px solid #eee;
  border-radius: 8px;
  margin-bottom: 8px;
}
.result-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}
.score {
  font-size: 12px;
  color: #999;
}
.result-text {
  font-size: 13px;
  color: #555;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
</style>
