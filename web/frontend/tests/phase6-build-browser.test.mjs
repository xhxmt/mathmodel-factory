import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createServer as createHttpServer } from 'node:http'
import { mkdtemp, readFile, readdir, stat } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, extname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import vue from '@vitejs/plugin-vue'
import { chromium } from 'playwright'
import { build } from 'vite'

const TEST_DIR = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(TEST_DIR, '..')
const VITE_BIN = resolve(FRONTEND_ROOT, 'node_modules/.bin/vite')
const PHASE6_SOURCE = 'src/components/Phase6ProjectSnapshotPanel.vue'

function launchChromium() {
  const executablePath = process.env.PHASE6_CHROMIUM_EXECUTABLE
  return chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
  })
}

function run(command, args, options = {}) {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(command, args, {
      cwd: FRONTEND_ROOT,
      env: options.env || process.env,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (chunk) => { stdout += chunk })
    child.stderr.on('data', (chunk) => { stderr += chunk })
    child.on('error', rejectPromise)
    child.on('close', (code) => {
      if (code === 0) resolvePromise({ stdout, stderr })
      else rejectPromise(new Error(
        `command failed (${code}): ${command} ${args.join(' ')}\n${stdout}\n${stderr}`,
      ))
    })
  })
}

async function productionBuild(outDir, enabled) {
  const env = { ...process.env }
  delete env.VITE_PHASE6_FULL_SHADOW_ENABLED
  if (enabled) env.VITE_PHASE6_FULL_SHADOW_ENABLED = 'true'
  await run(VITE_BIN, [
    'build',
    '--configLoader', 'runner',
    '--manifest',
    '--emptyOutDir',
    '--outDir', outDir,
  ], { env })
  return JSON.parse(await readFile(join(outDir, '.vite/manifest.json'), 'utf8'))
}

async function filesBelow(root) {
  const result = []
  for (const entry of await readdir(root, { withFileTypes: true })) {
    const path = join(root, entry.name)
    if (entry.isDirectory()) result.push(...await filesBelow(path))
    else if (entry.isFile()) result.push(path)
  }
  return result
}

const CONTENT_TYPES = Object.freeze({
  '.css': 'text/css',
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json',
  '.ttf': 'font/ttf',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
})

async function serveDirectory(root) {
  const canonicalRoot = resolve(root)
  const server = createHttpServer(async (request, response) => {
    try {
      const requested = new URL(request.url, 'http://127.0.0.1').pathname
      const relativePath = decodeURIComponent(requested === '/' ? '/index.html' : requested)
      const target = resolve(canonicalRoot, `.${relativePath}`)
      if (target !== canonicalRoot && !target.startsWith(`${canonicalRoot}/`)) {
        response.writeHead(403).end('forbidden')
        return
      }
      const metadata = await stat(target)
      if (!metadata.isFile()) throw new Error('not a file')
      response.writeHead(200, {
        'content-type': CONTENT_TYPES[extname(target)] || 'application/octet-stream',
        'cache-control': 'no-store',
      })
      response.end(await readFile(target))
    } catch {
      response.writeHead(404, { 'content-type': 'text/plain' })
      response.end('not found')
    }
  })
  await new Promise((resolvePromise) => server.listen(0, '127.0.0.1', resolvePromise))
  const address = server.address()
  return {
    origin: `http://127.0.0.1:${address.port}`,
    close: () => new Promise((resolvePromise, rejectPromise) => {
      server.close((error) => (error ? rejectPromise(error) : resolvePromise()))
    }),
  }
}

function readyPayload() {
  const snapshotId = 'c'.repeat(64)
  return {
    schema_version: 'phase6-project-snapshot-web-v1',
    state: 'ready',
    project_id: 'demo',
    snapshot_id: snapshotId,
    revision: 7,
    server_revision: 7,
    coordinate: { snapshot_id: snapshotId, revision: 7 },
    sections: [{ key: 'summary', data: { status: 'ok' } }],
    actions: [
      { id: 'open-first', title: '第一项', severity: 'warning' },
      { id: 'blocked-middle', title: '不可用项', severity: 'critical', disabled: true },
      { id: 'open-last', title: '最后一项', severity: 'info' },
    ],
    authoritative: false,
    authority_transferred: false,
    dispatch_performed: false,
  }
}

test('real production builds prove default-off module, route, chunk, and network isolation', async () => {
  const buildRoot = await mkdtemp(join(tmpdir(), 'phase6-production-gate-'))
  const offDir = join(buildRoot, 'off')
  const onDir = join(buildRoot, 'on')
  const offManifest = await productionBuild(offDir, false)
  const onManifest = await productionBuild(onDir, true)

  const offManifestText = JSON.stringify(offManifest)
  assert.equal(offManifestText.toLowerCase().includes('phase6'), false)
  assert.equal(Object.hasOwn(offManifest, PHASE6_SOURCE), false)
  const offWorkspace = Object.values(offManifest).find(({ src }) => src?.endsWith('ProjectWorkspace.vue'))
  assert.ok(offWorkspace)
  assert.equal(offWorkspace.dynamicImports?.some((path) => path.toLowerCase().includes('phase6')), false)

  const offFiles = await filesBelow(offDir)
  assert.equal(offFiles.some((path) => relative(offDir, path).toLowerCase().includes('phase6')), false)
  const offJavaScript = (await Promise.all(
    offFiles.filter((path) => extname(path) === '.js').map((path) => readFile(path, 'utf8')),
  )).join('\n')
  for (const forbidden of [
    'Phase6ProjectSnapshotPanel',
    'phase6-project-snapshot-web-v1',
    '/phase6-snapshot',
    'phase6',
  ]) {
    assert.equal(offJavaScript.includes(forbidden), false, `default build leaked ${forbidden}`)
  }

  const onWorkspace = Object.values(onManifest).find(({ src }) => src?.endsWith('ProjectWorkspace.vue'))
  assert.ok(onWorkspace.dynamicImports.includes(PHASE6_SOURCE))
  assert.ok(onManifest[PHASE6_SOURCE])
  const onPanelChunk = await readFile(join(onDir, onManifest[PHASE6_SOURCE].file), 'utf8')
  assert.match(onPanelChunk, /phase6-snapshot/)
  assert.match(onPanelChunk, /快照行动中心/)

  const staticSite = await serveDirectory(offDir)
  let browser = null
  try {
    browser = await launchChromium()
    const page = await browser.newPage()
    const requested = []
    page.on('request', (request) => requested.push({
      type: request.resourceType(),
      url: request.url(),
    }))
    await page.goto(`${staticSite.origin}/?tab=phase6`, { waitUntil: 'networkidle' })
    const resources = await page.evaluate(() => performance.getEntriesByType('resource').map(({ name }) => name))
    assert.ok(resources.some((url) => url.endsWith('.js')))
    assert.equal(
      requested.some(({ type, url }) => (
        type !== 'document' && url.toLowerCase().includes('phase6')
      )),
      false,
    )
    assert.equal(requested.some(({ url }) => /\/api\/.*phase6/i.test(url)), false)
    assert.equal(resources.some((url) => url.toLowerCase().includes('phase6')), false)
  } finally {
    if (browser) await browser.close()
    await staticSite.close()
  }
})

test('production-compiled Vue panel exercises real keyboard, ARIA, error, and request flows', async () => {
  const harnessRoot = await mkdtemp(join(tmpdir(), 'phase6-mounted-browser-'))
  await build({
    root: FRONTEND_ROOT,
    configFile: false,
    plugins: [vue()],
    base: '/',
    logLevel: 'silent',
    build: {
      outDir: harnessRoot,
      emptyOutDir: true,
      manifest: true,
      rollupOptions: {
        input: join(TEST_DIR, 'phase6-panel.harness.html'),
      },
    },
  })
  const site = await serveDirectory(harnessRoot)
  let browser = null
  let mode = 'initial'
  let releaseInitial
  const initialGate = new Promise((resolvePromise) => { releaseInitial = resolvePromise })
  const apiRequests = []
  const consoleMessages = []
  try {
    browser = await launchChromium()
    const page = await browser.newPage()
    page.on('console', (message) => consoleMessages.push(message.text()))
    await page.route('**/api/projects/demo/phase6-snapshot*', async (route) => {
      apiRequests.push(route.request().url())
      if (mode === 'initial') await initialGate
      if (mode === 'unknown') {
        await route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({
            detail: {
              code: 'SQL_DRIVER_STACK',
              message: 'Bearer secret /srv/private/auth.db SELECT token FROM acl',
            },
          }),
        })
        return
      }
      if (mode === 'known') {
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'PHASE6_SNAPSHOT_TAMPERED' }),
        })
        return
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(readyPayload()),
      })
    })

    await page.goto(`${site.origin}/tests/phase6-panel.harness.html`, { waitUntil: 'domcontentloaded' })
    const panel = page.locator('.phase6-snapshot')
    await panel.waitFor()
    assert.equal(await panel.getAttribute('aria-busy'), 'true')
    assert.equal(await panel.locator('button[aria-label="刷新 Phase 6 项目快照"]').isDisabled(), true)
    assert.equal(await panel.locator('[role="status"]').count(), 1)

    mode = 'ready'
    releaseInitial()
    await page.waitForFunction(() => document.querySelector('.phase6-snapshot')?.getAttribute('aria-busy') === 'false')
    assert.equal(apiRequests.length, 1)
    assert.match(apiRequests[0], /\/api\/projects\/demo\/phase6-snapshot$/)

    const toolbar = panel.locator('[role="toolbar"]')
    assert.equal(await toolbar.getAttribute('aria-orientation'), 'vertical')
    const first = toolbar.locator('[data-action-id="open-first"]')
    const disabled = toolbar.locator('[data-action-id="blocked-middle"]')
    const last = toolbar.locator('[data-action-id="open-last"]')
    assert.equal(await first.getAttribute('tabindex'), '0')
    assert.equal(await disabled.isDisabled(), true)
    assert.equal(await disabled.getAttribute('aria-disabled'), 'true')
    assert.equal(await last.getAttribute('tabindex'), '-1')

    await first.focus()
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.actionId), 'open-first')
    await page.keyboard.press('ArrowDown')
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.actionId), 'open-last')
    await page.keyboard.press('ArrowDown')
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.actionId), 'open-first')
    await page.keyboard.press('ArrowUp')
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.actionId), 'open-last')
    await page.keyboard.press('Home')
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.actionId), 'open-first')
    await page.keyboard.press('End')
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.actionId), 'open-last')

    await first.focus()
    await page.keyboard.press('Tab')
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.testid), 'after-panel')
    await page.keyboard.press('Shift+Tab')
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.actionId), 'open-first')

    await page.keyboard.press('Enter')
    await last.focus()
    await page.keyboard.press('Space')
    await disabled.click({ force: true })
    assert.equal(await page.locator('[data-testid="navigations"]').textContent(), 'open-first,open-last')

    mode = 'unknown'
    await panel.locator('button[aria-label="刷新 Phase 6 项目快照"]').click()
    const unknownAlert = panel.locator('[role="alert"]')
    await unknownAlert.waitFor()
    const unknownText = await unknownAlert.textContent()
    assert.match(unknownText, /项目快照服务暂不可用/)
    assert.doesNotMatch(unknownText, /SQL|Bearer|\/srv|SELECT|token|STACK/)
    assert.equal(await unknownAlert.evaluate((element) => element === document.activeElement), true)

    mode = 'known'
    await unknownAlert.locator('button').click()
    const knownAlert = panel.locator('[role="alert"]')
    await knownAlert.waitFor()
    const knownText = await knownAlert.textContent()
    assert.match(knownText, /快照完整性校验失败/)
    assert.doesNotMatch(knownText, /PHASE6_SNAPSHOT_TAMPERED/)

    mode = 'ready'
    await knownAlert.locator('button').click()
    await toolbar.waitFor()
    assert.match(apiRequests.at(-1), /expected_revision=7/)
    assert.equal(consoleMessages.join('\n').includes('/srv/private'), false)
    assert.equal(consoleMessages.join('\n').includes('Bearer secret'), false)

    const timeoutPage = await browser.newPage()
    let releaseLateResponse
    const lateResponse = new Promise((resolvePromise) => {
      releaseLateResponse = resolvePromise
    })
    await timeoutPage.route('**/api/projects/demo/phase6-snapshot*', async (route) => {
      await lateResponse
      try {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(readyPayload()),
        })
      } catch {
        // The product deadline may already have canceled this browser request.
      }
    })
    await timeoutPage.goto(
      `${site.origin}/tests/phase6-panel.harness.html?deadlineMs=300`,
      { waitUntil: 'domcontentloaded' },
    )
    const timeoutPanel = timeoutPage.locator('.phase6-snapshot')
    await timeoutPanel.waitFor()
    assert.equal(await timeoutPanel.getAttribute('aria-busy'), 'true')
    const timeoutRefresh = timeoutPanel.locator(
      'button[aria-label="刷新 Phase 6 项目快照"]',
    )
    assert.equal(await timeoutRefresh.isDisabled(), true)
    await timeoutPage.waitForFunction(
      () => document.querySelector('.phase6-snapshot')?.getAttribute('aria-busy') === 'false',
      null,
      { timeout: 3_000 },
    )
    assert.equal(await timeoutRefresh.isDisabled(), false)
    const timeoutAlert = timeoutPanel.locator('[role="alert"]')
    await timeoutAlert.waitFor()
    assert.match(await timeoutAlert.textContent(), /项目快照服务暂不可用/)
    assert.equal(
      await timeoutAlert.evaluate((element) => element === document.activeElement),
      true,
    )
    releaseLateResponse()
    await timeoutPage.waitForTimeout(100)
    assert.equal(await timeoutPanel.getAttribute('aria-busy'), 'false')
    assert.equal(await timeoutPanel.locator('[role="alert"]').count(), 1)
    assert.equal(await timeoutPanel.locator('[role="toolbar"]').count(), 0)
    await timeoutPage.close()
  } finally {
    if (browser) await browser.close()
    await site.close()
  }
})
