import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const backendTarget = process.env.PF_BACKEND_TARGET || 'http://127.0.0.1:8000'
const wsTarget = backendTarget.replace(/^http/, 'ws')

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: backendTarget,
        changeOrigin: true
      },
      '/ws': {
        target: wsTarget,
        ws: true
      }
    }
  }
})
