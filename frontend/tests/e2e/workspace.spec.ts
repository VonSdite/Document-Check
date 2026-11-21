import { expect, test } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'
import { dirname } from 'node:path'
import { pathToFileURL } from 'node:url'

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => {
    throw error
  })
})

test('各业务页面加载对应组件，管理入口遵循当前权限', async ({ page }) => {
  await page.goto('/__frontend__/admin')
  await expect(page.locator('.app-sidebar')).toBeVisible()
  await expect(page.locator('.app-nav a[title="用户权限设置"]')).toBeVisible()
  await page.goto('/__frontend__/user')
  await expect(page.locator('.app-nav')).toBeVisible()
  await expect(page.locator('.app-nav a[title="用户权限设置"]')).toHaveCount(0)
  for (const route of ['/', '/consistency', '/language-consistency', '/images', '/videos']) {
    await page.goto(route)
    await expect(page.locator('[data-component-ready="true"] .ui-task-composer')).toBeVisible()
    const selected = await page.locator('input[name=checks]:checked').count()
    expect(selected).toBeGreaterThan(0)
    if (route === '/consistency') expect(selected).toBe(1)
    if (route === '/language-consistency')
      await expect(page.locator('input[name=submission_token]')).not.toHaveValue('')
  }
})

test('上传控件合并重复选择、删除文件并提交真实任务', async ({ page }) => {
  await page.goto('/')
  const form = page.locator('.ui-task-composer')
  await expect(form).toBeVisible()
  const input = form.locator('input[type=file]')
  const first = {
    name: '组件化验证甲.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('设备额定电压为220V。使用前需要断开电源。'),
  }
  const second = {
    name: '组件化验证乙.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('设备安装完成后应检查连接是否牢固。'),
  }
  await input.setInputFiles(first)
  await input.setInputFiles(second)
  await expect(form.locator('.ui-file-row')).toHaveCount(2)
  await form.getByRole('button', { name: '移除 组件化验证乙.txt' }).click()
  expect(await input.evaluate((node: HTMLInputElement) => node.files?.length)).toBe(1)
  await form.getByRole('button', { name: '提交检查', exact: true }).click()
  await expect(page.locator('[data-refresh-region=task-list]')).toContainText('组件化验证甲.txt')
})

test('筛选和刷新任务列表时保留新建任务草稿', async ({ page }) => {
  await page.goto('/')
  const file = page.locator('.ui-task-composer input[type=file]')
  await file.setInputFiles({
    name: '未提交草稿.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('保留草稿'),
  })
  const original = await page.locator('.ui-task-composer input[name=checks]:checked').count()
  await page.locator('[data-task-filters] input[name=keyword]').fill('report')
  await page.locator('[data-task-filters] button[type=submit]').click()
  await expect(page).toHaveURL(/keyword=report/)
  await expect(page.locator('[data-refresh-region=task-list]')).toContainText('report.txt')
  expect(await file.evaluate((node: HTMLInputElement) => node.files?.[0]?.name)).toBe(
    '未提交草稿.txt',
  )
  expect(await page.locator('.ui-task-composer input[name=checks]:checked').count()).toBe(original)
  await page.getByRole('button', { name: '刷新任务列表', exact: true }).click()
  expect(await file.evaluate((node: HTMLInputElement) => node.files?.length)).toBe(1)
})

test('模型拉取、测试、思考配置和保存沿用正式表单协议', async ({ page }) => {
  const requests: Record<string, unknown>[] = []
  await page.route('**/models/fetch', async (route) => {
    requests.push(route.request().postDataJSON())
    await route.fulfill({ json: { fetched_models: ['model-alpha', 'model-beta'] } })
  })
  await page.route('**/models/test', async (route) => {
    requests.push(route.request().postDataJSON())
    await route.fulfill({ json: { ok: true, message: '模型连通性测试通过。' } })
  })
  await page.goto('/models')
  await page.locator('[data-modal-open="provider-modal-new"]').click()
  const dialog = page.locator('#provider-modal-new')
  await expect(dialog.locator('[data-model-editor][data-component-ready=true]')).toBeVisible()
  await dialog.locator('[name=name]').fill('组件测试提供商')
  await dialog.locator('[name=api_base]').fill('https://example.test')
  await dialog.locator('[name=api_key]').fill('example-browser-key')
  await dialog.getByRole('button', { name: '拉取模型', exact: true }).click()
  const picker = page.getByRole('dialog', { name: '选择拉取模型', exact: true })
  await expect(picker).toBeVisible()
  await picker.getByLabel('model-alpha', { exact: true }).check()
  await picker.getByRole('button', { name: '加入当前清单' }).click()
  await expect(picker).not.toBeVisible()
  await dialog.locator('[data-model-reasoning-effort]').selectOption('max')
  await dialog.getByRole('button', { name: '测试', exact: true }).click()
  await expect(dialog.locator('.ui-model-result')).toHaveText('模型连通性测试通过。')
  expect(requests[0].api_base).toBe('https://example.test/v1/chat/completions')
  expect(requests[1].reasoning_effort).toBe('max')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.locator('.provider-table')).toContainText('组件测试提供商')
  await expect(page.locator('.provider-table')).toContainText('思考 max')
})

test('报告认可和不认可原因可以保存并在刷新后保持', async ({ page }) => {
  await page.goto('/tasks/1')
  const item = page.locator('[data-report-item]').first()
  await item.locator('input[data-report-acceptance-status][value=accepted]').check()
  await expect(
    item.locator('input[data-report-acceptance-status][value=accepted]'),
  ).toHaveAttribute('data-saved-value', 'accepted')
  await item.locator('input[data-report-acceptance-status][value=rejected]').check()
  await page.locator('[data-report-rejection-modal][open]').waitFor()
  await page.locator('[data-report-rejection-choice=evidence_insufficient]').click()
  await expect(item.locator('[data-report-rejection-reason]')).toHaveAttribute(
    'data-saved-value',
    'evidence_insufficient',
  )
  await page.reload()
  await expect(
    page.locator('[data-report-item]').first().locator('input[value=rejected]'),
  ).toBeChecked()
})

test('导出的 HTML 报告离线打开时保留样式和列宽调整', async ({ page }, testInfo) => {
  const response = await page.request.get('/tasks/1/export')
  expect(response.ok()).toBe(true)
  const path = testInfo.outputPath('report.html')
  await mkdir(dirname(path), { recursive: true })
  await writeFile(path, await response.body())
  await page.context().setOffline(true)
  await page.goto(pathToFileURL(path).href)
  await expect(page.getByRole('heading', { name: '任务状态', exact: true })).toBeVisible()
  await expect(page.locator('script[src], link[rel=stylesheet]')).toHaveCount(0)
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  expect(
    await page.locator('.page').evaluate((node) => node.getBoundingClientRect().left),
  ).toBeLessThan(24)
  const header = page.locator('.report-table th.report-field-description').first()
  const before = await header.evaluate((node) => node.getBoundingClientRect().width)
  await header.getByRole('separator').focus()
  await page.keyboard.press('ArrowRight')
  await expect
    .poll(() => header.evaluate((node) => node.getBoundingClientRect().width))
    .toBeGreaterThan(before + 5)
})

test('公共规则分页、提示词切换和权限保存可操作', async ({ page }) => {
  await page.goto('/__frontend__/admin')
  await page.goto('/admin/settings')
  const tabs = page.locator('[data-prompt-tab]')
  await tabs.nth(1).click()
  await expect(tabs.nth(1)).toHaveAttribute('aria-selected', 'true')
  await expect(page.locator('[data-prompt-panel]:not([hidden])')).toHaveCount(1)
  const rules = page.locator('[data-report-suppression-rules]')
  await expect(rules.locator('[data-report-suppression-row]')).toHaveCount(20)
  await rules.getByRole('link', { name: '下一页', exact: true }).click()
  await expect(page.locator('[data-report-suppression-row]')).toHaveCount(4)
  await page.goto('/admin/permissions')
  await expect(page.locator('[data-permission-form]').first()).toBeAttached()
  const choice = page.locator('input[name=permissions][value="rules.manage"]').first()
  const original = await choice.isChecked()
  await choice.setChecked(!original)
  await expect(page.locator('[data-permission-form][data-saving=true]')).toHaveCount(0)
  await page.reload()
  await expect(page.locator('input[name=permissions][value="rules.manage"]').first()).toBeChecked({
    checked: !original,
  })
  await page.locator('input[name=permissions][value="rules.manage"]').first().setChecked(original)
})

test('侧栏折叠、主题记忆和移动端导航支持键盘', async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await page.getByRole('button', { name: '收起侧栏' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-sidebar', 'collapsed')
  await page.getByRole('button', { name: '切换主题' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect(page.locator('html')).toHaveAttribute('data-sidebar', 'collapsed')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: '打开导航' }).click()
  const menu = page.getByRole('dialog', { name: '文档智能门禁', exact: true })
  await expect(menu).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(menu).not.toBeVisible()
  await expect(page.getByRole('button', { name: '打开导航' })).toBeFocused()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})
