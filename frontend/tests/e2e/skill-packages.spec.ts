import { expect, test } from '@playwright/test'

test('上传 Skill、查看参考资料、更新版本并加入方案提交', async ({ page }) => {
  await page.goto('/__frontend__/admin')
  await page.goto('/admin/rules?tab=items')
  await page.locator('.pool-create-item > summary').click()
  const create = page.locator('[data-create-check-item-form]')
  await create.locator('[name=name]').fill('Skill 浏览器检查')
  await create.locator('[data-check-method]').selectOption('skill')
  await expect(create.locator('[name=prompt]')).toBeDisabled()
  const source = await page.request.get('/__frontend__/skill-package')
  await create.locator('[data-skill-file]').setInputFiles({
    name: '接口检查.skill',
    mimeType: 'application/zip',
    buffer: await source.body(),
  })
  await create.getByRole('button', { name: '创建检查项', exact: true }).click()
  const row = page.locator('[data-check-item-row]').filter({ hasText: 'Skill 浏览器检查' })
  await expect(row).toBeVisible()
  await expect(row.locator('.check-item-kind')).toHaveText('Skill')
  await expect(create.locator('[data-check-method]')).toHaveValue('prompt')
  const id = await row.getAttribute('data-check-item-id')
  await row.locator('[data-check-item-toggle]').click()
  const editor = page.locator(`#check-item-detail-${id}`)
  await editor.getByText('文件清单与检查内容', { exact: true }).click()
  await expect(editor.locator('[data-skill-preview]')).toHaveValue(/请求字段应与示例一致/)
  await expect(editor.locator('.skill-package-files')).toContainText('references/标准.md')
  const originalVersion = await editor.locator('.skill-package-summary').textContent()
  const download = page.waitForEvent('download')
  await editor.getByRole('link', { name: '下载当前 Skill 包' }).click()
  expect((await download).suggestedFilename()).toBe('接口检查.skill')
  const replacement = await page.request.get('/__frontend__/skill-package?version=2')
  await editor.locator('[data-skill-file]').setInputFiles({
    name: '接口检查-v2.zip',
    mimeType: 'application/zip',
    buffer: await replacement.body(),
  })
  await editor.getByRole('button', { name: '保存', exact: true }).click()
  await expect(editor.locator('.skill-package-summary')).toContainText('接口检查-v2.zip')
  await expect(editor.locator('[name=name]')).toHaveJSProperty('required', false)
  await row.locator('[data-check-item-toggle]').click()
  await expect(editor).toBeVisible()
  expect(await editor.locator('.skill-package-summary').textContent()).not.toBe(originalVersion)
  await editor.getByText('文件清单与检查内容', { exact: true }).click()
  await expect(editor.locator('[data-skill-preview]')).toHaveValue(/更新后的接口标准/)
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  for (const control of [
    editor.locator('[data-skill-file]'),
    editor.locator('[data-skill-preview]'),
    editor.getByRole('button', { name: '保存', exact: true }),
  ]) {
    const bounds = await control.boundingBox()
    expect(bounds).not.toBeNull()
    expect(bounds!.x).toBeGreaterThanOrEqual(0)
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390)
  }
  await page.screenshot({ path: '../instance/skill-package-mobile.png', fullPage: true })

  await page.goto('/admin/rules?profile_id=software')
  const configured = page.locator(`[name=profile_checks][value="${id}"]`)
  await configured.check()
  await page.getByRole('button', { name: '保存方案', exact: true }).click()
  await page.goto('/?profile_id=software')
  const composer = page.locator('.ui-task-composer')
  const check = composer.locator(`input[type=checkbox][name=checks][value="${id}"]`)
  await expect(check).toBeChecked()
  await expect(check).toBeDisabled()
  await composer.locator('input[type=file]').setInputFiles({
    name: 'Skill 页面验证.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('接口参数 userId 为必填。'),
  })
  await composer.getByRole('button', { name: '提交检查', exact: true }).click()
  await expect(
    page.locator('tr[data-task-id]').filter({ hasText: 'Skill 页面验证.txt' }),
  ).toBeVisible()

  await page.goto('/admin/rules?profile_id=software')
  await configured.uncheck()
  await page.getByRole('button', { name: '保存方案', exact: true }).click()
})
