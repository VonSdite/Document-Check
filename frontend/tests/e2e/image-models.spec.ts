import { expect, test } from '@playwright/test'

test('图片和视频仅允许已验证模型，没有模型时给出入口并阻止提交', async ({ page, request }) => {
  await request.post('/__frontend__/image-model-capability', { data: { verified: false } })
  try {
    await page.goto('/__frontend__/user')
    for (const viewport of [
      { width: 1440, height: 900 },
      { width: 390, height: 844 },
    ]) {
      await page.setViewportSize(viewport)
      await page.emulateMedia({ colorScheme: viewport.width < 500 ? 'dark' : 'light' })
      for (const route of ['/images', '/videos']) {
        await page.goto(route)
        const form = page.locator('.ui-task-composer')
        await expect(form).toBeVisible()
        await expect(form.getByRole('button', { name: '提交检查' })).toBeDisabled()
        await expect(form.locator('select[name=model_id]')).toHaveValue('')
        await expect(form.getByRole('link', { name: '前往模型管理' })).toHaveAttribute(
          'href',
          '/models',
        )
        await expect(form).toContainText('测试图片识别')
        expect(
          await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
        ).toBe(true)
      }
    }
  } finally {
    await request.post('/__frontend__/image-model-capability', { data: { verified: true } })
  }
  await page.goto('/videos')
  await expect(page.locator('.ui-task-composer button[type=submit]')).toBeEnabled()
  await expect(page.locator('.ui-task-composer select[name=model_id]')).not.toHaveValue('')
})

test('图片识别测试显示能力结果，改动连接后需要重新验证', async ({ page }) => {
  await page.route('**/models/test-image', async (route) => {
    await route.fulfill({ json: { ok: true, message: '图片识别测试通过，可用于图片和视频检查。' } })
  })
  await page.goto('/models')
  await page.locator('[data-modal-open="provider-modal-new"]').click()
  const dialog = page.locator('#provider-modal-new')
  await dialog.locator('[name=api_base]').pressSequentially('https://example.test/proxy')
  await expect(dialog.locator('[name=api_base]')).toHaveValue('https://example.test/proxy')
  await dialog.locator('[data-model-name]').fill('test-image-model')
  await dialog.getByRole('button', { name: '测试图片识别', exact: true }).click()
  await expect(dialog.locator('[data-image-capability]')).toContainText('可检查图片和视频')
  await dialog.locator('[name=api_key]').fill('new-key')
  await expect(dialog.locator('[data-image-capability]')).toContainText('尚未验证')
})
