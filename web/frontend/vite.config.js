import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'

const backendTarget = process.env.PF_BACKEND_TARGET || 'http://127.0.0.1:8000'
const wsTarget = backendTarget.replace(/^http/, 'ws')
const optionalSnapshotModule = fileURLToPath(new URL(
  process.env.VITE_PHASE6_FULL_SHADOW_ENABLED === 'true'
    ? './src/lib/optionalSnapshotFeature.enabled.js'
    : './src/lib/optionalSnapshotFeature.disabled.js',
  import.meta.url,
))

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      'virtual:optional-workspace-snapshot': optionalSnapshotModule,
    },
  },
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
