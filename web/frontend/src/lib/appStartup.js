async function warmModels(loadModels, onModelWarmupError = () => {}) {
  if (typeof loadModels !== 'function') return
  try {
    await loadModels()
  } catch (error) {
    onModelWarmupError(error)
  }
}

export async function runLoginFlow(
  { login, refreshProjects, loadModels, connectWS, onModelWarmupError },
  data,
) {
  login(data)
  await refreshProjects()
  await connectWS()
  void warmModels(loadModels, onModelWarmupError)
}

export async function runAuthenticatedStartup(
  { bootstrap, refreshProjects, loadModels, connectWS, onModelWarmupError },
) {
  const ok = await bootstrap()
  if (!ok) return false

  await refreshProjects()
  await connectWS()
  void warmModels(loadModels, onModelWarmupError)
  return true
}
