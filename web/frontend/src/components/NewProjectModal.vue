<template>
  <div class="ov" @click.self="$emit('close')" @dragover.prevent="onModalDragOver" @drop.prevent="onModalDrop">
    <div v-if="modalDrag" class="dragmask" @dragleave.prevent="modalDrag = false" @drop.prevent="onModalDrop">
      <Icon name="upload" :size="40" />
      <div class="dm-t">释放以上传题目文件或压缩包</div>
      <div class="dm-s mono">PDF / Markdown / ZIP / TAR.GZ</div>
    </div>

    <div class="modal panel rise">
      <div class="m-head">
        <div class="mh-l"><Icon name="plus" :size="16" /><span>新建建模项目</span></div>
        <button class="btn btn-icon btn-ghost btn-sm" @click="$emit('close')"><Icon name="x" :size="15" /></button>
      </div>

      <form class="m-body" @submit.prevent="submit">
        <div class="fg">
          <label class="fg-lbl label">项目名称 <span class="req">*</span></label>
          <input v-model="form.base_name" class="field mono" placeholder="cumcm2024_a" pattern="[a-zA-Z0-9_-]+" required :disabled="loading" />
          <p class="hint">仅限字母、数字、下划线、连字符</p>
        </div>

        <div class="fg">
          <label class="fg-lbl label">题目文件 <span class="req">*</span></label>
          <div class="seg">
            <button type="button" class="seg-b" :class="{ on: method === 'file' }" @click="method = 'file'" :disabled="loading"><Icon name="upload" :size="13" /> 上传</button>
            <button type="button" class="seg-b" :class="{ on: method === 'path' }" @click="method = 'path'" :disabled="loading"><Icon name="folder" :size="13" /> 服务器路径</button>
          </div>

          <div v-if="method === 'file'">
            <div v-if="!file" class="drop" :class="{ over: drag }" @click="$refs.fi.click()" @dragover.prevent="drag = true" @dragleave.prevent="drag = false" @drop.prevent="onDrop">
              <input ref="fi" type="file" accept=".pdf,.md,.PDF,.MD,.zip,.tar,.gz,.bz2,.xz,.tgz,.tar.gz,.tar.bz2,.tar.xz" hidden @change="onPick" />
              <Icon name="file-text" :size="26" />
              <div class="drop-t">点击或拖拽题目文件/压缩包</div>
              <div class="drop-s mono">PDF / Markdown / ZIP / TAR.GZ</div>
            </div>
            <div v-else class="picked">
              <Icon :name="file.name.toLowerCase().endsWith('.pdf') ? 'file-text' : 'file'" :size="18" />
              <div class="pk-info"><div class="pk-name mono">{{ file.name }}</div><div class="pk-size mono">{{ fmt(file.size) }}</div></div>
              <button type="button" class="btn btn-icon btn-sm btn-ghost" @click="clearFile" :disabled="loading"><Icon name="x" :size="13" /></button>
            </div>
            <div v-if="progress > 0 && progress < 100" class="prog"><div class="prog-bar"><div class="prog-fill" :style="{ width: progress + '%' }"></div></div><span class="mono">{{ progress }}%</span></div>
          </div>

          <input v-else v-model="form.problem_path" class="field mono" placeholder="/home/user/problems/2024_A.pdf" :disabled="loading" />
          <p v-if="method === 'path'" class="hint">服务器上题目文件的完整路径</p>
        </div>

        <label class="chk"><input type="checkbox" v-model="form.no_start" :disabled="loading" /><span>仅创建，不自动开始</span></label>
        <label class="chk"><input type="checkbox" v-model="form.consult" :disabled="loading" /><span>启用人工咨询（关键决策点暂停等待人工）</span></label>

        <div v-if="error" class="m-err"><Icon name="alert-triangle" :size="14" /> {{ error }}</div>

        <div class="m-foot">
          <button type="button" class="btn btn-ghost" @click="$emit('close')" :disabled="loading">取消</button>
          <button type="submit" class="btn btn-amber" :disabled="loading">
            <span v-if="loading" class="spinner sm"></span>
            <template v-else><Icon name="plus" :size="14" /> 创建项目</template>
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import api, { Projects, formatBytes } from '../lib/api.js'

export default {
  name: 'NewProjectModal',
  components: { Icon },
  emits: ['close', 'project-created'],
  data() {
    return {
      form: { base_name: '', problem_path: '', no_start: false, consult: false },
      method: 'file', file: null, progress: 0, drag: false, modalDrag: false, loading: false, error: '',
    }
  },
  methods: {
    fmt: formatBytes,
    onPick(e) { const f = e.target.files?.[0]; if (f) this.accept(f) },
    onDrop(e) { this.drag = false; const f = e.dataTransfer.files?.[0]; if (f) this.accept(f) },
    onModalDragOver(e) { if (e.dataTransfer?.types?.includes('Files')) this.modalDrag = true },
    onModalDrop(e) { this.modalDrag = false; const f = e.dataTransfer.files?.[0]; if (f) this.accept(f) },
    accept(f) {
      if (!/\.(pdf|md|zip|tar|gz|bz2|xz|tgz)$/i.test(f.name)) { this.error = '仅支持 PDF、Markdown 或压缩包（.zip, .tar.gz 等）'; return }
      this.file = f; this.method = 'file'; this.error = ''
      if (!this.form.base_name) this.form.base_name = f.name.replace(/\.(tar\.(gz|bz2|xz)|[^/.]+)$/i, '').replace(/[^a-zA-Z0-9_-]/g, '_')
    },
    clearFile() { this.file = null; this.progress = 0; if (this.$refs.fi) this.$refs.fi.value = '' },
    async upload(file) {
      const fd = new FormData()
      fd.append('file', file)
      this.progress = 0
      const { data } = await api.post('/api/upload/problem', fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: (e) => { this.progress = Math.round((e.loaded * 100) / (e.total || 1)) },
      })
      return data.file_path
    },
    async submit() {
      this.loading = true; this.error = ''
      try {
        if (this.method === 'file') {
          if (!this.file) { this.error = '请选择题目文件'; this.loading = false; return }
          this.form.problem_path = await this.upload(this.file)
        } else if (!this.form.problem_path) {
          this.error = '请输入文件路径'; this.loading = false; return
        }
        const result = await Projects.create(this.form)
        this.$emit('project-created', result)
        this.$emit('close')
      } catch (err) {
        this.error = err.response?.data?.detail || err.message || '创建项目失败'
      } finally {
        this.loading = false
      }
    },
  },
}
</script>

<style scoped>
.ov { position: fixed; inset: 0; z-index: 300; background: rgba(0, 0, 0, 0.5); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; padding: 20px; }
.modal { width: 100%; max-width: 560px; max-height: 92vh; overflow-y: auto; box-shadow: var(--shadow-lg); }

.dragmask { position: fixed; inset: 22px; z-index: 310; background: color-mix(in srgb, var(--bg) 88%, transparent); border: 2px dashed var(--amber); border-radius: var(--r-xl); display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; color: var(--amber); }
.dm-t { font-size: 18px; font-weight: 700; }
.dm-s { font-size: 12px; color: var(--ink-3); }

.m-head { display: flex; align-items: center; justify-content: space-between; padding: 15px 18px; border-bottom: 1px solid var(--line); }
.mh-l { display: flex; align-items: center; gap: 9px; font-size: 15px; font-weight: 700; }
.m-body { padding: 18px; display: flex; flex-direction: column; gap: 16px; }

.fg { display: flex; flex-direction: column; gap: 7px; }
.fg-lbl { display: block; }
.req { color: var(--amber); }
.hint { font-size: 11.5px; color: var(--ink-3); }

.seg { display: flex; gap: 4px; padding: 4px; background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--r); margin-bottom: 4px; }
.seg-b { flex: 1; display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 8px; background: none; border: none; border-radius: var(--r-sm); color: var(--ink-3); font: 600 12.5px/1 var(--sans); cursor: pointer; }
.seg-b.on { background: var(--panel-3); color: var(--ink); }

.drop { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 28px; border: 1.5px dashed var(--line-2); border-radius: var(--r); cursor: pointer; color: var(--ink-3); transition: all 0.15s var(--ease); }
.drop:hover, .drop.over { border-color: var(--amber); color: var(--amber); background: var(--amber-dim); }
.drop-t { font-size: 13px; color: var(--ink-2); }
.drop-s { font-size: 11px; }

.picked { display: flex; align-items: center; gap: 11px; padding: 12px 14px; background: var(--panel-2); border: 1px solid var(--line); border-left: 2px solid var(--ok); border-radius: var(--r); color: var(--ok); }
.pk-info { flex: 1; min-width: 0; }
.pk-name { font-size: 12.5px; color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pk-size { font-size: 10.5px; color: var(--ink-3); margin-top: 2px; }

.prog { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.prog-bar { flex: 1; height: 5px; background: var(--bg-2); border-radius: 5px; overflow: hidden; }
.prog-fill { height: 100%; background: var(--amber); transition: width 0.2s var(--ease); }
.prog .mono { font-size: 11px; color: var(--ink-3); }

.chk { display: flex; align-items: center; gap: 9px; font-size: 13px; color: var(--ink-2); cursor: pointer; }
.chk input { accent-color: var(--amber); width: 15px; height: 15px; }

.m-err { display: flex; align-items: center; gap: 8px; padding: 10px 12px; background: var(--bad-dim); border: 1px solid var(--bad); border-radius: var(--r-sm); color: var(--bad); font-size: 12.5px; }

.m-foot { display: flex; justify-content: flex-end; gap: 10px; padding-top: 6px; }
.spinner.sm { width: 15px; height: 15px; border-top-color: var(--amber-ink); }
</style>
