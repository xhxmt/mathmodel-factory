// Explicit build-time opt-in for the reviewed, non-authoritative shadow UI.
export const optionalWorkspaceExtensionEnabled = true
export const optionalWorkspaceExtensionKey = 'phase6'
export const optionalWorkspaceExtensionTab = Object.freeze({
  key: optionalWorkspaceExtensionKey,
  label: '验证快照',
  icon: 'shield',
})
export const optionalWorkspaceExtensionLoader = () => (
  import('../components/Phase6ProjectSnapshotPanel.vue')
)
