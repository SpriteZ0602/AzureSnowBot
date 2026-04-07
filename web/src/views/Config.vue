<template>
  <div class="config-page">
    <el-tabs v-model="activeTab">
      <!-- .env 编辑 -->
      <el-tab-pane label=".env 配置" name="env">
        <el-card shadow="hover">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center;">
              <span>环境变量</span>
              <el-tag type="warning" size="small">修改后需重启 Bot 生效</el-tag>
            </div>
          </template>
          <el-table :data="envEntries" stripe size="small">
            <el-table-column label="配置项" width="260">
              <template #default="{ row }">
                <span v-if="row.is_comment" class="comment-text">{{ row.raw }}</span>
                <span v-else>{{ row.key }}</span>
              </template>
            </el-table-column>
            <el-table-column label="值">
              <template #default="{ row }">
                <template v-if="!row.is_comment">
                  <el-input
                    v-if="editingKey === row.key"
                    v-model="editingValue"
                    size="small"
                    @keyup.enter="saveEnvField"
                    @blur="editingKey = ''"
                  >
                    <template #append>
                      <el-button @click="saveEnvField" size="small">保存</el-button>
                    </template>
                  </el-input>
                  <span
                    v-else
                    class="editable-value"
                    @click="startEdit(row)"
                  >
                    {{ row.value || '(空)' }}
                    <el-icon v-if="!row.is_sensitive"><Edit /></el-icon>
                  </span>
                </template>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- Admin context files -->
      <el-tab-pane
        v-for="f in adminFiles"
        :key="f.filename"
        :label="f.filename"
        :name="f.filename"
      >
        <el-card shadow="hover">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center;">
              <span>{{ f.filename }}</span>
              <el-button type="primary" size="small" @click="saveAdminFile(f.filename)" :loading="saving">
                保存
              </el-button>
            </div>
          </template>
          <el-input
            v-model="fileContents[f.filename]"
            type="textarea"
            :autosize="{ minRows: 15, maxRows: 35 }"
          />
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import api from '../api'
import { ElMessage } from 'element-plus'

const activeTab = ref('env')
const envEntries = ref<any[]>([])
const adminFiles = ref<any[]>([])
const fileContents = reactive<Record<string, string>>({})
const editingKey = ref('')
const editingValue = ref('')
const saving = ref(false)

async function loadEnv() {
  const { data } = await api.get('/config/env')
  envEntries.value = data.entries
}

async function loadAdminFiles() {
  const { data } = await api.get('/config/admin')
  adminFiles.value = data
  // 逐个加载内容
  for (const f of data) {
    if (f.exists) {
      const { data: fd } = await api.get(`/config/admin/${f.filename}`)
      fileContents[f.filename] = fd.content
    } else {
      fileContents[f.filename] = ''
    }
  }
}

function startEdit(row: any) {
  if (row.is_sensitive) return
  editingKey.value = row.key
  editingValue.value = row.value
}

async function saveEnvField() {
  if (!editingKey.value) return
  try {
    await api.put('/config/env', {
      key: editingKey.value,
      value: editingValue.value,
    })
    ElMessage.success('已更新')
    editingKey.value = ''
    loadEnv()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '更新失败')
  }
}

async function saveAdminFile(filename: string) {
  saving.value = true
  try {
    await api.put(`/config/admin/${filename}`, {
      content: fileContents[filename] || '',
    })
    ElMessage.success(`${filename} 已保存`)
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  loadEnv()
  loadAdminFiles()
})
</script>

<style scoped>
.comment-text {
  color: #999;
  font-style: italic;
}
.editable-value {
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 4px;
}
.editable-value:hover {
  color: #409eff;
}
</style>
