<template>
  <div class="personas-page">
    <el-card shadow="hover">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <span>人格列表</span>
          <el-button type="primary" size="small" @click="showCreate = true">新建人格</el-button>
        </div>
      </template>

      <h4>通用人格</h4>
      <el-row :gutter="16">
        <el-col :span="8" v-for="p in personas.global" :key="p.name">
          <el-card shadow="hover" class="persona-card" @click="viewPersona(p.name)">
            <div class="persona-name">{{ p.name }}</div>
            <p class="persona-preview">{{ p.prompt_preview }}</p>
          </el-card>
        </el-col>
      </el-row>

      <h4 v-if="personas.group.length">群私有人格</h4>
      <el-row :gutter="16">
        <el-col :span="8" v-for="p in personas.group" :key="`${p.group_id}-${p.name}`">
          <el-card shadow="hover" class="persona-card" @click="viewPersona(p.name, p.group_id)">
            <div class="persona-name">
              {{ p.name }}
              <el-tag v-if="p.is_active" type="success" size="small">活跃</el-tag>
            </div>
            <small>群 {{ p.group_id }}</small>
            <p class="persona-preview">{{ p.prompt_preview }}</p>
          </el-card>
        </el-col>
      </el-row>
    </el-card>

    <!-- 查看/编辑弹窗 -->
    <el-dialog v-model="showEdit" :title="`人格: ${editName}`" width="600px">
      <el-input
        v-model="editPrompt"
        type="textarea"
        :autosize="{ minRows: 10, maxRows: 25 }"
      />
      <template #footer>
        <el-button @click="showEdit = false">取消</el-button>
        <el-button type="danger" @click="deletePersona" v-if="editName !== 'default'">删除</el-button>
        <el-button type="primary" @click="savePersona">保存</el-button>
      </template>
    </el-dialog>

    <!-- 新建弹窗 -->
    <el-dialog v-model="showCreate" title="新建人格" width="500px">
      <el-form :model="createForm" label-width="80px">
        <el-form-item label="名称">
          <el-input v-model="createForm.name" placeholder="英文名称" />
        </el-form-item>
        <el-form-item label="作用域">
          <el-select v-model="createForm.group_id" placeholder="通用" clearable>
            <el-option value="" label="通用（所有群共享）" />
            <el-option
              v-for="gid in groupIds"
              :key="gid"
              :value="gid"
              :label="`群 ${gid}`"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="Prompt">
          <el-input v-model="createForm.prompt" type="textarea" :rows="8" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreate = false">取消</el-button>
        <el-button type="primary" @click="createPersona">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import api from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

const personas = ref<{ global: any[]; group: any[] }>({ global: [], group: [] })
const showEdit = ref(false)
const showCreate = ref(false)
const editName = ref('')
const editGroupId = ref('')
const editPrompt = ref('')

const createForm = reactive({ name: '', group_id: '', prompt: '' })

// 从群私有人格列表 + 群会话列表中提取所有群号
const groupIds = computed(() => {
  const ids = new Set<string>()
  for (const p of personas.value.group) {
    if (p.group_id) ids.add(p.group_id)
  }
  for (const gid of allGroupIds.value) {
    ids.add(gid)
  }
  return [...ids].sort()
})

const allGroupIds = ref<string[]>([])

async function loadGroups() {
  try {
    const { data } = await api.get('/conversations/groups')
    allGroupIds.value = data.map((g: any) => g.group_id)
  } catch { /* ignore */ }
}

async function loadPersonas() {
  const { data } = await api.get('/personas')
  personas.value = data
}

async function viewPersona(name: string, groupId = '') {
  const { data } = await api.get(`/personas/${name}`, { params: { group_id: groupId } })
  editName.value = name
  editGroupId.value = groupId
  editPrompt.value = data.prompt
  showEdit.value = true
}

async function savePersona() {
  try {
    await api.put(`/personas/${editName.value}`, {
      prompt: editPrompt.value,
    }, { params: { group_id: editGroupId.value } })
    ElMessage.success('保存成功')
    showEdit.value = false
    loadPersonas()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '保存失败')
  }
}

async function deletePersona() {
  await ElMessageBox.confirm('确认删除该人格？', '删除确认', { type: 'warning' })
  try {
    await api.delete(`/personas/${editName.value}`, { params: { group_id: editGroupId.value } })
    ElMessage.success('已删除')
    showEdit.value = false
    loadPersonas()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '删除失败')
  }
}

async function createPersona() {
  try {
    await api.post('/personas', createForm)
    ElMessage.success('创建成功')
    showCreate.value = false
    createForm.name = ''
    createForm.prompt = ''
    loadPersonas()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '创建失败')
  }
}

onMounted(() => {
  loadPersonas()
  loadGroups()
})
</script>

<style scoped>
.persona-card {
  cursor: pointer;
  margin-bottom: 16px;
  transition: border-color 0.2s;
}
.persona-card:hover {
  border-color: #409eff;
}
.persona-name {
  font-weight: 600;
  margin-bottom: 4px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.persona-preview {
  font-size: 13px;
  color: #888;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  margin: 4px 0 0;
}
</style>
