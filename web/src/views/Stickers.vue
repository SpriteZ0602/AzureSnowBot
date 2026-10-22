<template>
  <div class="stickers-page">
    <el-card shadow="never">
      <template #header>
        <div class="toolbar">
          <strong>群表情包</strong>
          <el-button :loading="loading" @click="refresh">刷新</el-button>
        </div>
      </template>
      <div class="toolbar filters">
        <el-select v-model="groupId" placeholder="选择群聊" filterable class="group-select">
          <el-option v-for="group in groups" :key="group.group_id" :value="group.group_id"
            :label="`群 ${group.group_id} · ${group.count} 张`" />
        </el-select>
        <el-select v-model="tag" placeholder="全部标签" clearable filterable class="tag-select" :disabled="!groupId">
          <el-option v-for="(count, name) in tags" :key="name" :value="String(name)" :label="`${name}（${count}）`" />
        </el-select>
        <span v-if="groupId" class="auto-switch">自动配图
          <el-switch v-model="autoEnabled" :disabled="settingBusy || loading" @change="changeEnabled" />
        </span>
      </div>
      <div class="stats">
        <span>本群 {{ count }} / {{ maxCount }} 张</span>
        <span>本群占用 {{ bytesText(groupBytes) }}</span>
        <span>全库 {{ bytesText(globalBytes) }} / {{ (globalLimit / 1e9).toFixed(0) }} GB</span>
      </div>
      <p class="hint">引用图片并 @Bot 说“把这张图片存成开心”即可收藏。GIF 点击预览可播放；关闭自动配图后仍可明确索图。</p>
    </el-card>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <div v-loading="loading" class="gallery-container">
      <el-empty v-if="!loading && !entries.length" :description="groupId ? '还没有匹配的表情包' : '暂无群聊图库'" />
      <div class="gallery">
        <el-card v-for="item in entries" :key="`${groupId}:${item.id}`" shadow="hover" class="sticker-card">
          <StickerImage :group-id="groupId" :sticker-id="item.id" />
          <div class="item-title"><code>#{{ item.id.slice(0, 8) }}</code><el-tag v-if="item.mime === 'image/gif'" size="small" type="success">GIF</el-tag></div>
          <div class="tags"><el-tag v-for="label in item.tags" :key="label" size="small">{{ label }}</el-tag></div>
          <p v-if="item.description" class="description">{{ item.description }}</p>
          <div class="details">上传者：{{ item.uploader || '未知' }} · {{ bytesText(item.size) }}<br />
            收藏：{{ dateText(item.created_at) }}<br />最近使用：{{ dateText(item.last_used_at) }}</div>
          <div class="actions">
            <el-button size="small" @click="edit(item)">修改标签</el-button>
            <el-button size="small" type="danger" plain @click="remove(item)">删除</el-button>
          </div>
        </el-card>
      </div>
    </div>
    <el-pagination v-if="total > 0" v-model:current-page="page" :page-size="24" :total="total"
      layout="total, prev, pager, next" class="pagination" />

    <el-dialog v-model="editVisible" title="修改表情标签" width="min(480px, 92vw)">
      <el-select v-model="editTags" multiple filterable allow-create default-first-option :multiple-limit="5"
        placeholder="输入标签后按回车" style="width: 100%">
        <el-option v-for="name in Object.keys(tags)" :key="name" :value="name" :label="name" />
      </el-select>
      <p class="hint">最多 5 个标签，每个最多 16 字。</p>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="!editTags.length" @click="saveTags">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'
import StickerImage from '../components/StickerImage.vue'

interface Sticker { id: string; tags: string[]; description: string; uploader: string; size: number; mime: string; created_at: number; last_used_at: number | null }
interface Group { group_id: string; count: number }
const groups = ref<Group[]>([])
const groupId = ref('')
const tag = ref('')
const tags = ref<Record<string, number>>({})
const entries = ref<Sticker[]>([])
const page = ref(1)
const total = ref(0)
const count = ref(0)
const groupBytes = ref(0)
const globalBytes = ref(0)
const globalLimit = ref(5_000_000_000)
const maxCount = ref(200)
const autoEnabled = ref(true)
const settingBusy = ref(false)
const loading = ref(false)
const error = ref('')
const editVisible = ref(false)
const editTags = ref<string[]>([])
const editTarget = ref({ groupId: '', id: '' })
const saving = ref(false)
let requestId = 0

function bytesText(bytes: number) {
  if (!bytes) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB']
  const index = Math.min(3, Math.floor(Math.log(bytes) / Math.log(1024)))
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`
}
function dateText(value: number | null) { return value ? new Date(value * 1000).toLocaleString() : '尚未使用' }
function errorText(e: any) { return e.response?.data?.detail || e.message || '操作失败，请稍后重试' }

async function loadGroups() {
  const { data } = await api.get('/stickers/groups')
  groups.value = data.groups.sort((a: Group, b: Group) => b.count - a.count || a.group_id.localeCompare(b.group_id))
  globalBytes.value = data.total_bytes
  globalLimit.value = data.max_total_bytes
  maxCount.value = data.max_count
  if (!groupId.value && groups.value.length) groupId.value = groups.value[0]!.group_id
}
async function loadItems() {
  const current = ++requestId
  entries.value = []
  if (!groupId.value) { loading.value = false; return }
  loading.value = true
  error.value = ''
  try {
    const { data } = await api.get(`/stickers/${groupId.value}`, { params: { tag: tag.value, page: page.value, page_size: 24 } })
    if (current !== requestId) return
    entries.value = data.items
    total.value = data.total
    tags.value = data.tags
    count.value = data.count
    groupBytes.value = data.bytes
    autoEnabled.value = data.enabled
    if (page.value > 1 && !data.items.length) page.value = Math.max(1, Math.ceil(data.total / 24))
  } catch (e) { if (current === requestId) error.value = errorText(e) }
  finally { if (current === requestId) loading.value = false }
}
async function refresh() {
  try { await loadGroups(); await loadItems() }
  catch (e) { error.value = errorText(e) }
}
async function changeEnabled(value: string | number | boolean) {
  const gid = groupId.value
  settingBusy.value = true
  try {
    await api.put(`/stickers/${gid}/settings`, { enabled: Boolean(value) })
    ElMessage.success('自动表情设置已保存')
  } catch (e) {
    if (groupId.value === gid) autoEnabled.value = !Boolean(value)
    ElMessage.error(errorText(e))
  } finally { settingBusy.value = false }
}
function edit(item: Sticker) {
  editTarget.value = { groupId: groupId.value, id: item.id }
  editTags.value = [...item.tags]
  editVisible.value = true
}
async function saveTags() {
  saving.value = true
  try {
    await api.patch(`/stickers/${editTarget.value.groupId}/${editTarget.value.id}`, { tags: editTags.value })
    editVisible.value = false
    ElMessage.success('标签已更新')
    await refresh()
  } catch (e) { ElMessage.error(errorText(e)) }
  finally { saving.value = false }
}
async function remove(item: Sticker) {
  const gid = groupId.value
  try {
    await ElMessageBox.confirm(`删除 #${item.id.slice(0, 8)} 及其全部标签关联？`, '删除表情包', { type: 'warning' })
    await api.delete(`/stickers/${gid}/${item.id}`)
    ElMessage.success('已删除')
    await refresh()
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorText(e)) }
}

watch(groupId, () => { tag.value = ''; page.value = 1; tags.value = {}; void loadItems() })
watch(tag, () => { page.value = 1; void loadItems() })
watch(page, () => { void loadItems() })
onMounted(refresh)
</script>

<style scoped>
.stickers-page { display: flex; flex-direction: column; gap: 20px; }
.toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.filters { justify-content: flex-start; }
.group-select { width: 250px; }
.tag-select { width: 200px; }
.auto-switch { display: flex; align-items: center; gap: 10px; }
.stats { display: flex; flex-wrap: wrap; gap: 12px 28px; margin-top: 20px; font-weight: 500; }
.hint { color: #909399; font-size: 13px; line-height: 1.7; margin-bottom: 0; }
.gallery-container { min-height: 200px; }
.gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 18px; }
.item-title { display: flex; justify-content: space-between; align-items: center; margin: 12px 0; }
.tags { display: flex; gap: 6px; flex-wrap: wrap; min-height: 24px; }
.description { font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.details { color: #909399; font-size: 12px; line-height: 1.8; margin-top: 12px; }
.actions { display: flex; margin-top: 14px; }
.pagination { justify-content: center; }
@media (max-width: 600px) { .group-select, .tag-select { width: 100%; } }
</style>
