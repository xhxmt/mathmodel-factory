<template>
  <section class="pp-panel panel">
    <div class="pp-head">
      <div><Icon name="git-branch" :size="15" /><strong>问题任务图</strong><span class="mono">problem-plan-v1</span></div>
      <button class="btn btn-icon btn-ghost btn-sm" :disabled="loading" title="刷新" @click="load">
        <Icon name="refresh" :size="13" :class="{ spin: loading }" />
      </button>
    </div>
    <div v-if="loading" class="pp-loading"><span class="spinner"></span> 正在加载任务图</div>
    <ContentBlockRenderer v-else :blocks="blocks" @open-file="$emit('open-file', $event)" />
    <div v-if="error" class="pp-error"><Icon name="alert-triangle" :size="13" />{{ error }}</div>
  </section>
</template>

<script>
import { onMounted, ref, watch } from 'vue'
import ContentBlockRenderer from './ContentBlockRenderer.vue'
import Icon from './Icon.vue'
import { Projects } from '../lib/api.js'

export default {
  name: 'ProblemPlanPanel',
  components: { ContentBlockRenderer, Icon },
  props: { base: { type: String, required: true } },
  emits: ['open-file'],
  setup(props) {
    const loading = ref(false)
    const blocks = ref([])
    const error = ref('')
    async function load() {
      if (!props.base) return
      loading.value = true
      error.value = ''
      try {
        const payload = await Projects.problemPlan(props.base)
        blocks.value = Array.isArray(payload.blocks) ? payload.blocks : []
      } catch (err) {
        error.value = err.response?.data?.detail || '问题任务图加载失败'
      } finally {
        loading.value = false
      }
    }
    watch(() => props.base, load)
    onMounted(load)
    return { loading, blocks, error, load }
  },
}
</script>

<style scoped>
.pp-panel { display: flex; flex-direction: column; gap: 12px; padding: 14px; }
.pp-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.pp-head > div { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.pp-head span { color: var(--ink-3); font-size: 9px; }
.pp-loading, .pp-error { display: flex; align-items: center; gap: 8px; padding: 12px; color: var(--ink-3); }
.pp-error { border: 1px solid var(--bad); border-radius: var(--r); color: var(--bad); background: var(--bad-dim); }
.spin { animation: spin .7s linear infinite; }
</style>
