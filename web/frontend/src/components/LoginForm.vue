<template>
  <div class="boot" role="dialog" aria-modal="true" aria-label="账号登录">
    <div class="boot-card panel rise">
      <button v-if="closable" type="button" class="bc-close" @click="$emit('close')" title="返回论文展厅">
        <Icon name="x" :size="16" />
      </button>
      <div class="bc-brand">
        <div class="mono-mark"><Icon name="layers" :size="22" /></div>
        <div>
          <div class="bc-name mono">PAPER_FACTORY</div>
          <div class="bc-sub mono">建模工坊 · 控制台</div>
        </div>
      </div>

      <div class="bc-boot mono">
        <span class="bp">$</span> authenticate operator<span class="caret">_</span>
      </div>

      <form @submit.prevent="submit" class="bc-form">
        <div class="seg">
          <button type="button" class="seg-b" :class="{ on: mode === 'login' }" @click="mode = 'login'" :disabled="loading">登录</button>
          <button type="button" class="seg-b" :class="{ on: mode === 'register' }" @click="mode = 'register'" :disabled="loading">注册</button>
        </div>

        <label class="fl">
          <span class="fl-lbl label">用户名</span>
          <span class="fl-wrap">
            <Icon name="user" :size="15" class="fl-ic" />
            <input v-model="username" class="field fl-in" type="text" placeholder="admin" autocomplete="username" :disabled="loading" required />
          </span>
        </label>

        <label v-if="mode === 'register'" class="fl">
          <span class="fl-lbl label">显示名</span>
          <span class="fl-wrap">
            <Icon name="user" :size="15" class="fl-ic" />
            <input v-model="displayName" class="field fl-in" type="text" autocomplete="name" :disabled="loading" />
          </span>
        </label>

        <label class="fl">
          <span class="fl-lbl label">密码</span>
          <span class="fl-wrap">
            <Icon name="lock" :size="15" class="fl-ic" />
            <input v-model="password" class="field fl-in" type="password" placeholder="••••••••" :autocomplete="mode === 'register' ? 'new-password' : 'current-password'" :disabled="loading" required />
          </span>
        </label>

        <label v-if="mode === 'register'" class="fl">
          <span class="fl-lbl label">确认密码</span>
          <span class="fl-wrap">
            <Icon name="lock" :size="15" class="fl-ic" />
            <input v-model="confirmPassword" class="field fl-in" type="password" autocomplete="new-password" :disabled="loading" required />
          </span>
        </label>

        <div v-if="error" class="bc-err">
          <Icon name="alert-triangle" :size="14" /> {{ error }}
        </div>
        <div v-if="notice" class="bc-ok">
          <Icon name="check-circle" :size="14" /> {{ notice }}
        </div>

        <button class="btn btn-amber bc-go" type="submit" :disabled="loading">
          <span v-if="loading" class="spinner sm"></span>
          <template v-else><Icon :name="mode === 'register' ? 'check' : 'log-out'" :size="15" :style="mode === 'login' ? 'transform: rotate(180deg)' : ''" /> {{ mode === 'register' ? '提交注册' : '登录' }}</template>
        </button>
      </form>

      <div class="bc-foot mono">
        <button v-if="closable" type="button" class="guest-return" @click="$emit('close')">
          <Icon name="book-open" :size="13" /> 默认用户 · 返回论文展厅
        </button>
        <template v-else>{{ mode === 'register' ? '注册后需管理员批准才能登录' : '请使用已批准账号登录' }}</template>
      </div>
    </div>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import { authLogin, authRegister } from '../lib/api.js'

export default {
  name: 'LoginForm',
  components: { Icon },
  props: { closable: { type: Boolean, default: false } },
  emits: ['login-success', 'close'],
  data() {
    return {
      mode: 'login',
      username: '',
      displayName: '',
      password: '',
      confirmPassword: '',
      loading: false,
      error: '',
      notice: '',
    }
  },
  methods: {
    async submit() {
      this.loading = true
      this.error = ''
      this.notice = ''
      try {
        if (this.mode === 'register') {
          if (this.password !== this.confirmPassword) {
            this.error = '两次密码不一致'
            return
          }
          await authRegister({ username: this.username, password: this.password, display_name: this.displayName })
          this.notice = '账号等待管理员批准'
          this.mode = 'login'
          this.password = ''
          this.confirmPassword = ''
          return
        }
        const data = await authLogin(this.username, this.password)
        localStorage.setItem('access_token', data.access_token)
        localStorage.setItem('username', data.username)
        this.$emit('login-success', data)
      } catch (err) {
        const detail = err.response?.data?.detail
        const messages = {
          USER_PENDING: '账号等待管理员批准',
          USER_REJECTED: '账号申请已被拒绝',
          USER_DISABLED: '账号已停用',
          USER_EXISTS: '用户名已存在',
          INVALID_USERNAME: '用户名仅允许字母、数字、下划线、连字符',
        }
        this.error = messages[detail] || detail || '登录失败，请检查用户名和密码'
      } finally {
        this.loading = false
      }
    },
  },
}
</script>

<style scoped>
.boot { position: fixed; inset: 0; z-index: 400; display: flex; align-items: center; justify-content: center; padding: 20px; background: color-mix(in srgb, var(--bg) 94%, transparent); backdrop-filter: blur(8px); }
.boot-card { position: relative; width: 100%; max-width: 400px; padding: 30px; box-shadow: var(--shadow-lg); }
.bc-close { position: absolute; top: 13px; right: 13px; display: flex; padding: 7px; border: 0; border-radius: 50%; background: transparent; color: var(--ink-3); cursor: pointer; }
.bc-close:hover { color: var(--ink); background: var(--panel-2); }

.bc-brand { display: flex; align-items: center; gap: 13px; margin-bottom: 22px; }
.mono-mark { width: 46px; height: 46px; border-radius: var(--r); background: var(--amber); color: var(--amber-ink); display: flex; align-items: center; justify-content: center; box-shadow: 0 0 24px var(--amber-glow); }
.bc-name { font-size: 18px; font-weight: 700; letter-spacing: 0.04em; }
.bc-sub { font-size: 11px; color: var(--ink-3); letter-spacing: 0.1em; margin-top: 2px; }

.bc-boot { font-size: 12px; color: var(--ink-3); padding: 10px 12px; background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--r-sm); margin-bottom: 22px; }
.bp { color: var(--ok); margin-right: 6px; }
.caret { animation: blink 1.1s step-end infinite; color: var(--amber); }

.bc-form { display: flex; flex-direction: column; gap: 15px; }
.seg { display: flex; gap: 4px; padding: 4px; background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--r); }
.seg-b { flex: 1; min-height: 34px; background: none; border: none; border-radius: var(--r-sm); color: var(--ink-3); font: 700 12.5px/1 var(--sans); cursor: pointer; }
.seg-b:hover:not(:disabled) { color: var(--ink); background: var(--panel-2); }
.seg-b.on { background: var(--panel-3); color: var(--ink); }
.seg-b:disabled { cursor: not-allowed; opacity: 0.6; }
.fl { display: flex; flex-direction: column; gap: 7px; }
.fl-lbl { padding-left: 1px; }
.fl-wrap { position: relative; display: flex; align-items: center; }
.fl-ic { position: absolute; left: 12px; color: var(--ink-3); pointer-events: none; }
.fl-in { padding-left: 36px; }

.bc-err, .bc-ok { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-radius: var(--r-sm); font-size: 12.5px; }
.bc-err { background: var(--bad-dim); border: 1px solid var(--bad); color: var(--bad); animation: shake 0.4s; }
.bc-ok { background: var(--ok-dim); border: 1px solid color-mix(in srgb, var(--ok) 45%, transparent); color: var(--ok); }
@keyframes shake { 10%,90% { transform: translateX(-1px); } 30%,70% { transform: translateX(3px); } 50% { transform: translateX(-4px); } }

.bc-go { width: 100%; padding: 12px; margin-top: 4px; font-size: 14px; }
.spinner.sm { width: 16px; height: 16px; border-top-color: var(--amber-ink); }

.bc-foot { margin-top: 22px; padding-top: 16px; border-top: 1px solid var(--line); text-align: center; font-size: 11px; color: var(--ink-3); }
.bc-foot b { color: var(--amber); font-weight: 700; }
.guest-return { display: inline-flex; align-items: center; gap: 7px; padding: 3px; border: 0; background: transparent; color: var(--ink-2); font: inherit; cursor: pointer; }
.guest-return:hover { color: var(--live); }
</style>
