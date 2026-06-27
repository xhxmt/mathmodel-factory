<template>
  <div class="boot">
    <div class="boot-card panel rise">
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
        <label class="fl">
          <span class="fl-lbl label">用户名</span>
          <span class="fl-wrap">
            <Icon name="user" :size="15" class="fl-ic" />
            <input v-model="username" class="field fl-in" type="text" placeholder="admin" autocomplete="username" :disabled="loading" required />
          </span>
        </label>

        <label class="fl">
          <span class="fl-lbl label">密码</span>
          <span class="fl-wrap">
            <Icon name="lock" :size="15" class="fl-ic" />
            <input v-model="password" class="field fl-in" type="password" placeholder="••••••••" autocomplete="current-password" :disabled="loading" required />
          </span>
        </label>

        <div v-if="error" class="bc-err">
          <Icon name="alert-triangle" :size="14" /> {{ error }}
        </div>

        <button class="btn btn-amber bc-go" type="submit" :disabled="loading">
          <span v-if="loading" class="spinner sm"></span>
          <template v-else><Icon name="log-out" :size="15" style="transform: rotate(180deg)" /> 登录</template>
        </button>
      </form>

      <div class="bc-foot mono">
        请使用 <b>web/.env</b> 中显式配置的管理员口令登录
      </div>
    </div>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import { authLogin } from '../lib/api.js'

export default {
  name: 'LoginForm',
  components: { Icon },
  emits: ['login-success'],
  data() { return { username: '', password: '', loading: false, error: '' } },
  methods: {
    async submit() {
      this.loading = true
      this.error = ''
      try {
        const data = await authLogin(this.username, this.password)
        localStorage.setItem('access_token', data.access_token)
        localStorage.setItem('username', data.username)
        this.$emit('login-success', data)
      } catch (err) {
        this.error = err.response?.data?.detail || '登录失败，请检查用户名和密码'
      } finally {
        this.loading = false
      }
    },
  },
}
</script>

<style scoped>
.boot { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center; padding: 20px; }
.boot-card { width: 100%; max-width: 400px; padding: 30px; box-shadow: var(--shadow-lg); }

.bc-brand { display: flex; align-items: center; gap: 13px; margin-bottom: 22px; }
.mono-mark { width: 46px; height: 46px; border-radius: var(--r); background: var(--amber); color: var(--amber-ink); display: flex; align-items: center; justify-content: center; box-shadow: 0 0 24px var(--amber-glow); }
.bc-name { font-size: 18px; font-weight: 700; letter-spacing: 0.04em; }
.bc-sub { font-size: 11px; color: var(--ink-3); letter-spacing: 0.1em; margin-top: 2px; }

.bc-boot { font-size: 12px; color: var(--ink-3); padding: 10px 12px; background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--r-sm); margin-bottom: 22px; }
.bp { color: var(--ok); margin-right: 6px; }
.caret { animation: blink 1.1s step-end infinite; color: var(--amber); }

.bc-form { display: flex; flex-direction: column; gap: 15px; }
.fl { display: flex; flex-direction: column; gap: 7px; }
.fl-lbl { padding-left: 1px; }
.fl-wrap { position: relative; display: flex; align-items: center; }
.fl-ic { position: absolute; left: 12px; color: var(--ink-3); pointer-events: none; }
.fl-in { padding-left: 36px; }

.bc-err { display: flex; align-items: center; gap: 8px; padding: 10px 12px; background: var(--bad-dim); border: 1px solid var(--bad); border-radius: var(--r-sm); color: var(--bad); font-size: 12.5px; animation: shake 0.4s; }
@keyframes shake { 10%,90% { transform: translateX(-1px); } 30%,70% { transform: translateX(3px); } 50% { transform: translateX(-4px); } }

.bc-go { width: 100%; padding: 12px; margin-top: 4px; font-size: 14px; }
.spinner.sm { width: 16px; height: 16px; border-top-color: var(--amber-ink); }

.bc-foot { margin-top: 22px; padding-top: 16px; border-top: 1px solid var(--line); text-align: center; font-size: 11px; color: var(--ink-3); }
.bc-foot b { color: var(--amber); font-weight: 700; }
</style>
