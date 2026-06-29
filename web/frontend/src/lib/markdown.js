// Compact, dependency-light Markdown → HTML renderer with KaTeX math.
// Handles: fenced code, LaTeX math ($$…$$, $…$, \[…\], \(…\)), headings, bold,
// inline code, links, lists, blockquotes, horizontal rules, tables, paragraphs.
// Math and code are extracted to sentinel tokens BEFORE inline processing so their
// contents (*, _, <, >, &) are never mangled. Underscore-emphasis is intentionally
// unsupported so snake_case identifiers and file_names stay intact.
//
// KaTeX JS is imported statically (it is real JS, fine in any runtime). Its CSS is
// loaded lazily on first render via a dynamic import so it lands in this module's
// chunk and stays out of the initial dashboard bundle. In a non-bundler runtime
// (the node test harness) the dynamic CSS import rejects and is silently ignored.
import katex from 'katex'

let katexCssLoaded = false
function loadKatexCss() {
  if (katexCssLoaded) return
  katexCssLoaded = true
  import('katex/dist/katex.min.css').then(() => {}, () => {})
}

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

function safeHref(raw) {
  const value = String(raw || '').trim()
  if (!value) return null
  const lower = value.toLowerCase()
  if (
    lower.startsWith('http://') ||
    lower.startsWith('https://') ||
    lower.startsWith('mailto:')
  ) {
    return encodeURI(value)
      .replace(/"/g, '%22')
      .replace(/'/g, '%27')
      .replace(/=/g, '%3D')
      .replace(/</g, '%3C')
      .replace(/>/g, '%3E')
  }
  return null
}

function renderMath(tex, display) {
  try {
    return katex.renderToString(tex.trim(), {
      displayMode: display,
      throwOnError: false,
      output: 'htmlAndMathml',
      strict: false,
    })
  } catch (e) {
    return `<code class="md-mathraw">${esc((display ? '$$' : '$') + tex + (display ? '$$' : '$'))}</code>`
  }
}

function inline(t) {
  return esc(t)
    .replace(/`([^`]+)`/g, '<code class="md-code">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, href) => {
      const safe = safeHref(href)
      if (!safe) return label
      return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${label}</a>`
    })
}

function splitRow(line) {
  return line.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((s) => s.trim())
}

export function renderMarkdown(src) {
  if (!src) return ''
  loadKatexCss()
  src = String(src).replace(/\r\n/g, '\n')

  // 1) pull fenced code blocks out first (so $ inside code is not treated as math)
  const code = []
  src = src.replace(/```[^\n]*\n([\s\S]*?)```/g, (m, c) => {
    code.push(`<pre class="md-pre"><code>${esc(c.replace(/\n+$/, ''))}</code></pre>`)
    return `\n@@CODE${code.length - 1}@@\n`
  })

  // 2) protect escaped dollars, then extract math (display before inline)
  src = src.replace(/\\\$/g, '@@DOLLAR@@')
  const math = []
  const stash = (tex, display) => { math.push(renderMath(tex, display)); return `@@MATH${math.length - 1}@@` }
  src = src.replace(/\$\$([\s\S]+?)\$\$/g, (m, t) => `\n\n${stash(t, true)}\n\n`)
  src = src.replace(/\\\[([\s\S]+?)\\\]/g, (m, t) => `\n\n${stash(t, true)}\n\n`)
  src = src.replace(/\\\(([\s\S]+?)\\\)/g, (m, t) => stash(t, false))
  src = src.replace(/\$([^\n$]+?)\$/g, (m, t) => stash(t, false))

  const lines = src.split('\n')
  const out = []
  let i = 0
  let para = []
  const flush = () => {
    if (para.length) { out.push(`<p>${inline(para.join(' '))}</p>`); para = [] }
  }

  while (i < lines.length) {
    const line = lines[i]
    const trimmed = line.trim()

    if (/^@@(?:CODE|MATH)\d+@@$/.test(trimmed)) { flush(); out.push(trimmed); i++; continue }
    if (!trimmed) { flush(); i++; continue }

    let m
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flush(); const l = m[1].length
      out.push(`<h${l} class="md-h md-h${l}">${inline(m[2])}</h${l}>`); i++; continue
    }
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(trimmed)) { flush(); out.push('<hr class="md-hr">'); i++; continue }

    // table: header row followed by a |---|---| separator
    if (line.includes('|') && i + 1 < lines.length &&
        /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes('-')) {
      flush()
      const header = splitRow(line)
      i += 2
      const rows = []
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) { rows.push(splitRow(lines[i])); i++ }
      let t = '<table class="md-table"><thead><tr>' +
        header.map((h) => `<th>${inline(h)}</th>`).join('') + '</tr></thead><tbody>'
      for (const r of rows) t += '<tr>' + r.map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>'
      t += '</tbody></table>'
      out.push(t); continue
    }

    if (/^>\s?/.test(line)) {
      flush(); const buf = []
      while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^>\s?/, '')); i++ }
      out.push(`<blockquote class="md-quote">${inline(buf.join(' '))}</blockquote>`); continue
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      flush(); const items = []
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*[-*+]\s+/, '')); i++ }
      out.push('<ul class="md-ul">' + items.map((it) => `<li>${inline(it)}</li>`).join('') + '</ul>'); continue
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      flush(); const items = []
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*\d+\.\s+/, '')); i++ }
      out.push('<ol class="md-ol">' + items.map((it) => `<li>${inline(it)}</li>`).join('') + '</ol>'); continue
    }

    para.push(trimmed); i++
  }
  flush()

  return out.join('\n')
    .replace(/@@CODE(\d+)@@/g, (m, n) => code[+n])
    .replace(/@@MATH(\d+)@@/g, (m, n) => math[+n])
    .replace(/@@DOLLAR@@/g, '$')
}
