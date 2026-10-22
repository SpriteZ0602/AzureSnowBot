<template>
  <div ref="container" class="sticker-image">
    <el-image v-if="src" :src="src" :preview-src-list="[src]" preview-teleported fit="contain" />
    <span v-else-if="failed">图片暂时不可用</span>
    <span v-else>加载预览…</span>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import api from '../api'

const props = defineProps<{ groupId: string; stickerId: string }>()
const container = ref<HTMLElement>()
const src = ref('')
const failed = ref(false)
const controller = new AbortController()
let observer: IntersectionObserver | undefined

async function load() {
  observer?.disconnect()
  try {
    const { data } = await api.get(`/stickers/${props.groupId}/${props.stickerId}/image`, {
      responseType: 'blob', signal: controller.signal,
    })
    if (!controller.signal.aborted) src.value = URL.createObjectURL(data)
  } catch {
    if (!controller.signal.aborted) failed.value = true
  }
}

onMounted(() => {
  observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) void load()
  }, { rootMargin: '200px' })
  if (container.value) observer.observe(container.value)
})

onBeforeUnmount(() => {
  controller.abort()
  observer?.disconnect()
  if (src.value) URL.revokeObjectURL(src.value)
})
</script>

<style scoped>
.sticker-image { height: 180px; display: flex; align-items: center; justify-content: center; color: #909399; background: #f5f7fa; border-radius: 6px; }
.el-image { width: 100%; height: 100%; }
</style>
