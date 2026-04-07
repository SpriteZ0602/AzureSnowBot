<template>
  <div class="skills-page">
    <el-card shadow="hover">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <span>技能目录</span>
          <el-button type="primary" size="small" @click="showCreate = true">新建技能</el-button>
        </div>
      </template>
      <el-row :gutter="16">
        <el-col :span="8" v-for="s in skills" :key="s.name">
          <el-card shadow="hover" class="skill-card" @click="viewSkill(s.name)">
            <div class="skill-header">
              <span class="skill-name">{{ s.name }}</span>
              <el-tag v-if="s.admin_only" type="danger" size="small">Admin</el-tag>
            </div>
            <p class="skill-desc">{{ s.description }}</p>
            <div v-if="s.references.length" class="skill-refs">
              <el-tag v-for="r in s.references" :key="r" size="small" type="info">{{ r }}</el-tag>
            </div>
          </el-card>
        </el-col>
      </el-row>
      <el-empty v-if="!skills.length" description="暂无技能" />
    </el-card>

    <!-- 查看/编辑弹窗 -->
    <el-dialog v-model="showDetail" :title="`技能: ${detailName}`" width="700px">
      <el-form label-width="80px">
        <el-form-item label="描述">
          <el-input v-model="detailDesc" />
        </el-form-item>
        <el-form-item label="SKILL.md">
          <el-input v-model="detailBody" type="textarea" :autosize="{ minRows: 12, maxRows: 25 }" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showDetail = false">取消</el-button>
        <el-button type="danger" @click="deleteSkill">删除</el-button>
        <el-button type="primary" @click="saveSkill">保存</el-button>
      </template>
    </el-dialog>

    <!-- 新建弹窗 -->
    <el-dialog v-model="showCreate" title="新建技能" width="600px">
      <el-form :model="createForm" label-width="80px">
        <el-form-item label="名称">
          <el-input v-model="createForm.name" placeholder="英文名称，如 weather" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="createForm.description" placeholder="技能描述，LLM 用来判断何时触发" />
        </el-form-item>
        <el-form-item label="Admin">
          <el-switch v-model="createForm.admin_only" />
          <span style="margin-left: 8px; color: #999; font-size: 12px;">仅 Admin 私聊可用</span>
        </el-form-item>
        <el-form-item label="SKILL.md">
          <el-input v-model="createForm.body" type="textarea" :rows="10" placeholder="技能的完整指令正文..." />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreate = false">取消</el-button>
        <el-button type="primary" @click="createSkill">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import api from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

const skills = ref<any[]>([])
const showDetail = ref(false)
const showCreate = ref(false)
const detailName = ref('')
const detailDesc = ref('')
const detailBody = ref('')

const createForm = reactive({ name: '', description: '', body: '', admin_only: false })

async function loadSkills() {
  const { data } = await api.get('/skills')
  skills.value = data
}

async function viewSkill(name: string) {
  const { data } = await api.get(`/skills/${name}`)
  detailName.value = name
  detailDesc.value = data.description
  detailBody.value = data.body
  showDetail.value = true
}

async function saveSkill() {
  try {
    await api.put(`/skills/${detailName.value}`, {
      description: detailDesc.value,
      body: detailBody.value,
    })
    ElMessage.success('保存成功')
    showDetail.value = false
    loadSkills()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '保存失败')
  }
}

async function deleteSkill() {
  await ElMessageBox.confirm('确认删除该技能？目录将被完全删除。', '删除确认', { type: 'warning' })
  try {
    await api.delete(`/skills/${detailName.value}`)
    ElMessage.success('已删除')
    showDetail.value = false
    loadSkills()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '删除失败')
  }
}

async function createSkill() {
  if (!createForm.name || !createForm.description) {
    ElMessage.warning('名称和描述不能为空')
    return
  }
  try {
    await api.post('/skills', createForm)
    ElMessage.success('创建成功')
    showCreate.value = false
    createForm.name = ''
    createForm.description = ''
    createForm.body = ''
    createForm.admin_only = false
    loadSkills()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '创建失败')
  }
}

onMounted(loadSkills)
</script>

<style scoped>
.skill-card {
  cursor: pointer;
  margin-bottom: 16px;
  transition: border-color 0.2s;
}
.skill-card:hover {
  border-color: #409eff;
}
.skill-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}
.skill-name {
  font-weight: 600;
}
.skill-desc {
  font-size: 13px;
  color: #666;
  margin: 4px 0;
}
.skill-refs {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  margin-top: 6px;
}
.skill-body {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.6;
  max-height: 500px;
  overflow-y: auto;
  background: #f9f9f9;
  padding: 16px;
  border-radius: 8px;
}
</style>
