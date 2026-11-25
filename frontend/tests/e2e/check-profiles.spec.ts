import { expect, test } from '@playwright/test'

test('方案切换固定执行配置的检查项、保留文件并提交方案快照', async ({ page }) => {
  await page.goto('/__frontend__/user')
  const form = page.locator('.ui-task-composer')
  await expect(form).toBeVisible()
  const first = form.locator('input[type=checkbox][name=checks]').first()
  const defaultId = await first.inputValue()
  await expect(first).toBeChecked()
  await expect(first).toBeDisabled()
  const file = form.locator('input[type=file]')
  await file.setInputFiles({
    name: '软件方案验证.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('接口 POST /users 的请求字段为 userId，响应字段为 userId。'),
  })
  await form.locator('select[name=profile_id]').selectOption('software')
  await expect(form.locator(`input[name=checks][value="${defaultId}"]`)).toHaveCount(0)
  await expect(form.locator('input[name=checks]:checked')).not.toHaveCount(0)
  await form.locator('select[name=profile_id]').selectOption('default')
  await expect(
    form.locator(`input[type=checkbox][name=checks][value="${defaultId}"]`),
  ).toBeChecked()
  await form.locator('select[name=profile_id]').selectOption('software')
  expect(await file.evaluate((node: HTMLInputElement) => node.files?.[0]?.name)).toBe(
    '软件方案验证.txt',
  )
  expect(
    await form
      .locator('input[type=checkbox][name=checks]')
      .evaluateAll((nodes: HTMLInputElement[]) =>
        nodes.every((node) => node.checked && node.disabled),
      ),
  ).toBe(true)
  expect(
    await form.evaluate((node: HTMLFormElement) => new FormData(node).getAll('checks').length),
  ).toBe(await form.locator('input[type=checkbox][name=checks]').count())
  await form.getByRole('button', { name: '提交检查', exact: true }).click()
  const row = page.locator('tr[data-task-id]').filter({ hasText: '软件方案验证.txt' }).first()
  await expect(row).toContainText('软件文档方案')
  const href = await row.locator('.task-report-link').getAttribute('href')
  await page.goto(href!)
  await expect(page.locator('.report-meta-list')).toContainText('软件文档方案')
  await expect(page.locator('.report-meta-list')).toContainText('版本')
  await page.goto('/')
  await expect(page.locator('.ui-task-composer select[name=profile_id]')).toHaveValue('software')
})

test('方案从检查项池勾选组合，池中提示词集中维护', async ({ page }) => {
  await page.goto('/__frontend__/admin')
  await page.goto('/admin/settings')
  await expect(page.locator('[name=global_concurrency]')).toBeVisible()
  await expect(page.locator('[data-prompt-panel]')).toHaveCount(4)
  await expect(page.locator('select[name=profile_id]')).toHaveCount(0)
  await page.goto('/admin/rules?profile_id=software')
  await expect(page.locator('[name=global_concurrency]')).toHaveCount(0)
  const composition = page.locator('[data-profile-composition]')
  await expect(composition).toBeVisible()
  const choice = composition
    .locator('[data-profile-check-choice]')
    .filter({ has: page.getByText('文档规范性检查', { exact: true }) })
  const check = choice.locator('[name=profile_checks]')
  const itemId = await check.inputValue()
  const originalChecked = await check.isChecked()
  await composition.locator('[data-profile-check-search]').fill('文档规范性')
  await expect(choice).toBeVisible()
  await check.check()
  await composition.getByRole('button', { name: '保存方案', exact: true }).click()
  await expect(check).toBeChecked()
  await page.screenshot({ path: '../instance/check-pool-desktop.png', fullPage: true })
  await page.goto('/?profile_id=software')
  const submitted = page.locator(
    `.ui-task-composer input[type=checkbox][name=checks][value="${itemId}"]`,
  )
  await expect(submitted).toBeChecked()
  await expect(submitted).toBeDisabled()

  await page.goto('/admin/rules?tab=items&profile_id=software')
  await expect(page.locator('[data-prompt-tab]')).toHaveCount(0)
  const panel = page.locator('[data-prompt-panel=document_check]')
  await panel
    .locator(`[data-check-item-row][data-check-item-id="${itemId}"] [data-check-item-toggle]`)
    .click()
  const editor = page.locator(`#check-item-detail-${itemId}`)
  await expect(editor).toBeVisible()
  const prompt = editor.locator('[name=prompt]')
  const original = await prompt.inputValue()
  await prompt.fill(original + '\n软件方案浏览器验证标记。')
  await expect(editor.locator('[name=default_selected]')).toHaveCount(0)
  await editor.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page).toHaveURL(/tab=items/)
  await panel
    .locator(`[data-check-item-row][data-check-item-id="${itemId}"] [data-check-item-toggle]`)
    .click()
  await expect(prompt).toHaveValue(original + '\n软件方案浏览器验证标记。')
  await prompt.fill(original)
  await editor.getByRole('button', { name: '保存', exact: true }).click()
  await page.goto('/admin/rules?profile_id=software')
  await check.setChecked(originalChecked)
  await composition.getByRole('button', { name: '保存方案', exact: true }).click()
  await page.locator('[data-profile-switch]').selectOption('default')
  await expect(page).toHaveURL(/profile_id=default/)
  await page.locator('[data-profile-switch]').selectOption('software')
  await expect(page).toHaveURL(/profile_id=software/)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: '切换主题' }).click()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: '../instance/check-profiles-mobile.png', fullPage: true })
  await page.getByRole('link', { name: '检查项池', exact: true }).click()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: '../instance/check-pool-mobile.png', fullPage: true })
})

test('其他检查页面使用系统检查项且不展示方案选择', async ({ page }) => {
  await page.goto('/__frontend__/admin')
  for (const path of ['/consistency', '/language-consistency', '/images', '/videos']) {
    await page.goto(`${path}?profile_id=software`)
    const form = page.locator('.ui-task-composer')
    await expect(form).toBeVisible()
    await expect(form.locator('[name=profile_id]')).toHaveCount(0)
    await expect(form.locator('input[name=checks]')).not.toHaveCount(0)
  }
  await page.goto('/admin/settings')
  for (const type of [
    'consistency_check',
    'language_consistency_check',
    'image_check',
    'video_check',
  ]) {
    await page.locator(`[data-prompt-tab=${type}]`).click()
    await expect(page.locator(`[data-prompt-panel=${type}]`)).toBeVisible()
  }
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: '../instance/system-check-settings-mobile.png', fullPage: true })
})
