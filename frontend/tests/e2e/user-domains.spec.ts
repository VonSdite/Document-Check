import { expect, test } from '@playwright/test'

test('领域组件延迟加载时保留用户已选择的筛选条件', async ({ page }) => {
  await page.goto('/__frontend__/admin')
  let release!: () => void
  const ready = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/assets/permissions-*.js', async (route) => {
    await ready
    await route.continue()
  })
  await page.goto('/admin/permissions?domain=unassigned', { waitUntil: 'domcontentloaded' })
  const filter = page.getByRole('combobox', { name: '产品领域筛选' })
  try {
    await expect(filter).toHaveValue('unassigned')
    await filter.selectOption('')
  } finally {
    release()
  }
  await expect(page.locator('[data-domain-filter]')).toHaveAttribute('data-component-ready', 'true')
  await expect(filter).toHaveValue('')
})

test('产品领域支持多选、新建、复用、筛选和清空，并在刷新后保留', async ({ page }) => {
  page.on('pageerror', (error) => {
    throw error
  })
  await page.goto('/__frontend__/admin')
  await page.goto('/admin/permissions')
  const first = page.locator('[data-permission-user="ip:192.0.2.10"]')
  const second = page.locator('[data-permission-user="ip:192.0.2.11"]')
  const dialog = page.getByRole('dialog', { name: '产品领域', exact: true })
  const edit = () => first.getByRole('button', { name: '示例用户甲：编辑产品领域' }).click()
  const tags = first.locator('.user-domain-editor > .user-domain-tags')
  const filter = page.getByRole('combobox', { name: '产品领域筛选' })
  await edit()
  await dialog.getByLabel('新领域', { exact: true }).fill('电源')
  await dialog.getByRole('button', { name: '添加', exact: true }).click()
  await dialog.getByLabel('新领域', { exact: true }).fill('工控')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog).not.toBeVisible()
  await expect(tags).toContainText('电源')
  await expect(tags).toContainText('工控')
  await expect(first.locator('input[name=permissions]:checked')).toHaveCount(0)
  await expect(filter.getByRole('option', { name: '电源', exact: true })).toHaveCount(1)

  await second.getByRole('button', { name: '示例用户乙：编辑产品领域' }).click()
  await dialog.getByRole('checkbox', { name: '电源', exact: true }).check()
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog).not.toBeVisible()
  await page.reload()
  await expect(tags).toContainText('工控')
  await expect(second.locator('.user-domain-tags')).toContainText('电源')

  await filter.selectOption({ label: '电源' })
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(page.locator('[data-permission-user]')).toHaveCount(2)
  await filter.selectOption({ label: '工控' })
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(page.locator('[data-permission-user]')).toHaveCount(1)
  await expect(first).toBeVisible()
  await page.locator('.permission-filter-bar input[name=keyword]').fill('没有此用户')
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(page.locator('[data-permission-user]')).toHaveCount(0)
  await expect(filter).toBeVisible()
  await page.locator('.permission-filter-bar input[name=keyword]').fill('')
  await page.getByRole('button', { name: '查询', exact: true }).click()

  await page.setViewportSize({ width: 390, height: 844 })
  await edit()
  await expect(dialog).toBeVisible()
  expect(await dialog.evaluate((node) => node.getBoundingClientRect().width <= innerWidth)).toBe(
    true,
  )
  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
  await expect(first.getByRole('button', { name: '示例用户甲：编辑产品领域' })).toBeFocused()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await edit()
  await dialog.getByRole('button', { name: '移除领域 电源', exact: true }).click()
  await dialog.getByRole('button', { name: '移除领域 工控', exact: true }).click()
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog).not.toBeVisible()
  await expect(tags).toHaveText('未标记')
  await filter.selectOption('unassigned')
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(first).toBeVisible()
  await expect(second).toHaveCount(0)
  await page.reload()
  await expect(tags).toHaveText('未标记')
})
