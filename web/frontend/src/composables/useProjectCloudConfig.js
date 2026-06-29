import { computed, onBeforeUnmount, ref } from 'vue'
import { Cloud } from '../lib/api.js'
import { normalizeCloudConfig } from '../lib/contracts.js'

export function createProjectCloudConfigController({ cloudApi = Cloud } = {}) {
  const cloudConfig = ref(null)
  const cloudConfigLoading = ref(false)
  const cloudSaving = ref(false)
  let disposed = false

  const cloudEnabled = computed(() => Boolean(cloudConfig.value?.enabled))
  const cloudSwitchLabel = computed(() => {
    if (cloudSaving.value) return '保存中'
    if (cloudConfigLoading.value && !cloudConfig.value) return '云端...'
    return cloudEnabled.value ? '云端开启' : '云端关闭'
  })
  const cloudSwitchTitle = computed(() => (
    cloudEnabled.value ? '关闭本项目云端加速' : '开启本项目云端加速'
  ))

  async function fetchCloudConfig(baseName) {
    if (!baseName || disposed) return null
    cloudConfigLoading.value = true
    try {
      cloudConfig.value = normalizeCloudConfig(await cloudApi.projectConfig(baseName))
      return cloudConfig.value
    } catch (error) {
      cloudConfig.value = normalizeCloudConfig({ enabled: false })
      return cloudConfig.value
    } finally {
      cloudConfigLoading.value = false
    }
  }

  async function setCloudAcceleration(baseName, enabled) {
    if (!baseName || cloudSaving.value || cloudConfigLoading.value) return cloudConfig.value
    cloudSaving.value = true
    try {
      const response = enabled ? await cloudApi.enable(baseName) : await cloudApi.disable(baseName)
      cloudConfig.value = normalizeCloudConfig(response?.config || await cloudApi.projectConfig(baseName))
      return cloudConfig.value
    } finally {
      cloudSaving.value = false
    }
  }

  function resetCloudConfig() {
    cloudConfig.value = null
    cloudConfigLoading.value = false
    cloudSaving.value = false
  }

  function stopCloudConfig() {
    disposed = true
  }

  return {
    cloudConfig,
    cloudConfigLoading,
    cloudSaving,
    cloudEnabled,
    cloudSwitchLabel,
    cloudSwitchTitle,
    fetchCloudConfig,
    setCloudAcceleration,
    resetCloudConfig,
    stopCloudConfig,
  }
}

export function useProjectCloudConfig(options) {
  const controller = createProjectCloudConfigController(options)
  onBeforeUnmount(controller.stopCloudConfig)
  return controller
}
