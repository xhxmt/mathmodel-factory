export const CONTENT_BLOCK_WIDGETS = Object.freeze({
  notice: 'notice',
  key_value: 'key-value',
  method_cards: 'method-cards',
  dag: 'dag',
  markdown: 'markdown',
  artifact_link: 'artifact-link',
  table: 'table',
})

export function blockWidget(block = {}) {
  return CONTENT_BLOCK_WIDGETS[String(block.render_type || '')] || 'unsupported'
}

export function normalizeContentBlocks(blocks) {
  if (!Array.isArray(blocks)) return []
  return blocks
    .filter((block) => block && typeof block === 'object' && block.id)
    .map((block) => ({
      id: String(block.id),
      type: String(block.type || 'data'),
      label: String(block.label || '未命名内容'),
      render_type: String(block.render_type || ''),
      content: block.content ?? null,
      children: normalizeContentBlocks(block.children),
      actions: Array.isArray(block.actions) ? block.actions : [],
      data_key: block.data_key || null,
      meta: block.meta && typeof block.meta === 'object' ? block.meta : {},
    }))
}

export function artifactRequestFromBlock(block = {}) {
  const content = block.content && typeof block.content === 'object' ? block.content : {}
  const path = String(content.path || '')
  if (!path || path.startsWith('/') || path.split('/').includes('..')) return null
  return {
    path,
    name: String(content.name || path.split('/').pop() || path),
    type: path.endsWith('.json') ? 'json' : path.endsWith('.md') ? 'markdown' : 'text',
  }
}
