<template>
  <div class="group" :class="{ open }">
    <div class="head" role="button" tabindex="0" :aria-expanded="open ? 'true' : 'false'" @click="open = !open" @keydown.enter.self="open = !open" @keydown.space.self.prevent="open = !open">
      <Icon name="chevron-right" :size="14" class="caret" />
      <div class="main">
        <div class="name">{{ archive.title }}</div>
        <div class="sub">
          {{ archive.run_count }} 次运行 · {{ archive.completed_count }} 次完成<template v-if="archive.running_count"> · {{ archive.running_count }} 次运行中</template><template v-if="archive.needs_attention_count"> · <span class="warn">{{ archive.needs_attention_count }} 次待你处理</span></template>
        </div>
      </div>
      <span class="latest">最近一次</span>
      <span class="pill" :class="tone">{{ label }}</span>
      <span class="time tnum">{{ rel(archive.latest.last_updated) }}</span>
    </div>
    <div v-if="open" class="runs">
      <ProjectRow
        v-for="run in archive.runs"
        :key="run.base_name"
        :project="run"
        nested
        @open="(...args) => $emit('open', ...args)"
        @action="(...args) => $emit('action', ...args)"
      />
    </div>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import ProjectRow from './ProjectRow.vue'
import { relativeTime } from '../lib/api.js'
import { executionLabel, statusTone } from '../lib/projectState.js'

export default {
  name: 'ProblemGroup',
  components: { Icon, ProjectRow },
  props: { archive: { type: Object, required: true } },
  emits: ['open', 'action'],
  data() { return { open: false } },
  computed: {
    tone() { return statusTone(this.archive.latest) },
    label() { return executionLabel(this.archive.latest) },
  },
  methods: { rel: relativeTime },
}
</script>

<style scoped>
.group { border-top: 1px solid var(--line); }
.group:first-child { border-top: 0; }
.head {
  display: grid;
  grid-template-columns: 14px minmax(0, 1fr) 150px 84px 84px 128px;
  align-items: center;
  gap: 14px;
  padding: 12px 16px;
  cursor: pointer;
  transition: background 0.12s var(--ease);
}
.head:hover { background: var(--panel-2); }
.caret { justify-self: center; color: var(--ink-3); transition: transform 0.15s var(--ease); }
.open .caret { transform: rotate(90deg); }
.main { min-width: 0; }
.name { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sub { font-size: 12.5px; color: var(--ink-2); margin-top: 2px; }
.warn { color: var(--amber); }
.latest { font-size: 12px; color: var(--ink-3); text-align: right; }
.pill { justify-self: start; }
.time { font-size: 12px; color: var(--ink-3); text-align: right; white-space: nowrap; }
.runs { border-top: 1px solid var(--line); }

@media (max-width: 860px) {
  .head { grid-template-columns: 14px minmax(0, 1fr) auto; }
  .latest, .time { display: none; }
}
</style>
