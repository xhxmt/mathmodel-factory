<template>
  <Teleport to="body">
    <div class="modal-backdrop" @click.self="$emit('close')">
      <div class="modal cloud-modal">
        <div class="modal-head">
          <div class="mh-left">
            <div class="mh-icon"><Icon name="zap" :size="18" /></div>
            <div>
              <div class="mh-title">云端加速计算</div>
              <div class="mh-sub mono">GCP Cloud Run 并行求解</div>
            </div>
          </div>
          <button class="btn btn-icon btn-ghost" @click="$emit('close')">
            <Icon name="x" :size="16" />
          </button>
        </div>

        <div class="modal-body">
          <div class="alert alert-info">
            <Icon name="info" :size="16" />
            <div>
              <div class="alert-title">检测到大量并发计算任务</div>
              <div class="alert-text">
                当前项目 <code class="mono">{{ base }}</code> 在 Step {{ step }}
                预计需要执行多个耗时计算任务。使用 GCP Cloud Run 可以并行加速求解过程。
              </div>
            </div>
          </div>

          <div class="section">
            <div class="sec-title">预估效果</div>
            <div class="metrics">
              <div class="metric">
                <div class="metric-label">本地串行</div>
                <div class="metric-value">~{{ estimatedLocal }} 小时</div>
              </div>
              <div class="metric-arrow"><Icon name="arrow-right" :size="16" /></div>
              <div class="metric">
                <div class="metric-label">云端并行</div>
                <div class="metric-value accent">~{{ estimatedCloud }} 小时</div>
              </div>
              <div class="metric">
                <div class="metric-label">预计加速</div>
                <div class="metric-value ok">{{ speedup }}×</div>
              </div>
            </div>
          </div>

          <div class="section">
            <div class="sec-title">Cloud Run 配置</div>
            <div v-if="cloudStatus" class="status-grid">
              <div class="status-item">
                <span class="status-label">服务状态</span>
                <span class="status-value">
                  <span class="dot" :class="cloudStatus.available ? 'dot-ok' : 'dot-err'"></span>
                  {{ cloudStatus.available ? '可用' : '不可用' }}
                </span>
              </div>
              <div class="status-item">
                <span class="status-label">服务区域</span>
                <span class="status-value mono">{{ cloudStatus.region || 'N/A' }}</span>
              </div>
              <div class="status-item">
                <span class="status-label">并发实例</span>
                <span class="status-value">最多 {{ cloudStatus.max_instances || 10 }} 个</span>
              </div>
              <div class="status-item">
                <span class="status-label">支持求解器</span>
                <span class="status-value mono">{{ cloudStatus.solvers?.join(', ') || 'python, julia' }}</span>
              </div>
            </div>
            <div v-else class="status-loading">
              <div class="spinner"></div>
              <span>检查 Cloud Run 状态...</span>
            </div>
          </div>

          <div class="section">
            <div class="sec-title">注意事项</div>
            <ul class="notice-list">
              <li>云端计算会产生 GCP 费用（按实例运行时间计费）</li>
              <li>需要网络连接上传脚本和下载结果</li>
              <li>首次调用可能需要冷启动（~30-60 秒）</li>
              <li>可随时在项目设置中关闭云端加速</li>
            </ul>
          </div>
        </div>

        <div class="modal-foot">
          <button class="btn btn-ghost" @click="$emit('close')">
            继续本地计算
          </button>
          <button class="btn btn-primary" :disabled="!cloudAvailable || enabling" @click="enable">
            <Icon name="zap" :size="14" />
            {{ enabling ? '启用中...' : '启用云端加速' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script>
import Icon from './Icon.vue'
import { Cloud } from '../lib/api.js'
import { useToasts } from '../composables/useToasts.js'

export default {
  name: 'CloudAcceleratorDialog',
  components: { Icon },
  props: {
    base: { type: String, required: true },
    step: { type: Number, required: true },
    estimatedLocal: { type: Number, default: 8 },
    estimatedCloud: { type: Number, default: 2 },
  },
  emits: ['close', 'enabled'],
  setup() { return { toasts: useToasts() } },
  data() {
    return {
      cloudStatus: null,
      enabling: false,
    }
  },
  computed: {
    speedup() {
      return (this.estimatedLocal / this.estimatedCloud).toFixed(1)
    },
    cloudAvailable() {
      return this.cloudStatus?.available || false
    },
  },
  mounted() {
    this.checkStatus()
  },
  methods: {
    async checkStatus() {
      try {
        this.cloudStatus = await Cloud.status()
      } catch (e) {
        this.cloudStatus = { available: false, error: e.message }
      }
    },
    async enable() {
      if (!this.cloudAvailable || this.enabling) return
      this.enabling = true
      try {
        await Cloud.enable(this.base)
        this.toasts.success('云端加速已启用', this.base)
        this.$emit('enabled')
        this.$emit('close')
      } catch (e) {
        this.toasts.error(e.response?.data?.detail || '启用失败')
      } finally {
        this.enabling = false
      }
    },
  },
}
</script>

<style scoped>
.modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.7); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; z-index: 9999; padding: 20px; animation: fadeIn 0.2s var(--ease); }
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

.cloud-modal { background: var(--panel); border: 1px solid var(--line); border-radius: var(--r-lg); max-width: 620px; width: 100%; max-height: 90vh; display: flex; flex-direction: column; box-shadow: var(--shadow-lg); animation: slideUp 0.3s var(--ease); }
@keyframes slideUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }

.modal-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 18px 20px; border-bottom: 1px solid var(--line); }
.mh-left { display: flex; align-items: center; gap: 12px; }
.mh-icon { width: 40px; height: 40px; border-radius: var(--r); background: linear-gradient(135deg, var(--live), var(--live-2)); display: flex; align-items: center; justify-content: center; color: white; flex-shrink: 0; }
.mh-title { font-size: 16px; font-weight: 700; }
.mh-sub { font-size: 12px; color: var(--ink-3); margin-top: 2px; }

.modal-body { padding: 20px; overflow-y: auto; flex: 1; }

.alert { display: flex; gap: 12px; padding: 14px 16px; border-radius: var(--r); margin-bottom: 20px; }
.alert-info { background: var(--live-dim); border: 1px solid var(--live-line); color: var(--ink); }
.alert-title { font-weight: 600; margin-bottom: 4px; }
.alert-text { font-size: 13px; line-height: 1.6; color: var(--ink-2); }
.alert code { font-family: var(--mono); font-size: 0.9em; background: rgba(255,255,255,0.15); padding: 2px 6px; border-radius: var(--r-xs); }

.section { margin-bottom: 24px; }
.section:last-child { margin-bottom: 0; }
.sec-title { font-size: 13px; font-weight: 700; color: var(--ink); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.08em; }

.metrics { display: grid; grid-template-columns: 1fr auto 1fr 1fr; gap: 12px; align-items: center; }
.metric { text-align: center; padding: 14px; background: var(--panel-2); border: 1px solid var(--line); border-radius: var(--r); }
.metric-label { font-size: 11px; color: var(--ink-3); margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.06em; }
.metric-value { font-size: 18px; font-weight: 700; font-family: var(--mono); }
.metric-value.accent { color: var(--live); }
.metric-value.ok { color: var(--ok); }
.metric-arrow { color: var(--ink-3); }

.status-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
.status-item { padding: 10px 12px; background: var(--panel-2); border: 1px solid var(--line); border-radius: var(--r); }
.status-label { display: block; font-size: 11px; color: var(--ink-3); margin-bottom: 4px; }
.status-value { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--ink); }
.dot { width: 6px; height: 6px; border-radius: 50%; }
.dot-ok { background: var(--ok); box-shadow: 0 0 6px var(--ok); }
.dot-err { background: var(--err); }

.status-loading { display: flex; align-items: center; justify-content: center; gap: 12px; padding: 30px; color: var(--ink-3); }

.notice-list { margin: 0; padding-left: 20px; }
.notice-list li { margin: 8px 0; font-size: 13px; line-height: 1.6; color: var(--ink-2); }

.modal-foot { display: flex; align-items: center; justify-content: flex-end; gap: 10px; padding: 14px 20px; border-top: 1px solid var(--line); }

@media (max-width: 640px) {
  .metrics { grid-template-columns: 1fr; gap: 8px; }
  .metric-arrow { display: none; }
  .status-grid { grid-template-columns: 1fr; }
}
</style>
