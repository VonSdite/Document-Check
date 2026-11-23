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
  const filter = page.getByRole('combobox', { name: '领域筛选' })
  try {
    await expect(filter).toHaveValue('unassigned')
    await filter.selectOption('')
  } finally {
    release()
  }
  await expect(page.locator('[data-domain-filter]')).toHaveAttribute('data-component-ready', 'true')
  await expect(filter).toHaveValue('')
})

test('领域支持多选、新建、复用、筛选和清空，并在刷新后保留', async ({ page }) => {
  page.on('pageerror', (error) => {
    throw error
  })
  await page.goto('/__frontend__/admin')
  await page.goto('/admin/permissions')
  const first = page.locator('[data-permission-user="ip:192.0.2.10"]')
  const second = page.locator('[data-permission-user="ip:192.0.2.11"]')
  const dialog = page.getByRole('dialog', { name: '领域', exact: true })
  const edit = () => first.getByRole('button', { name: '示例用户甲：编辑领域' }).click()
  const tags = first.locator('.user-domain-editor > .user-domain-tags')
  const filter = page.getByRole('combobox', { name: '领域筛选' })
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

  await second.getByRole('button', { name: '示例用户乙：编辑领域' }).click()
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
  await expect(first.getByRole('button', { name: '示例用户甲：编辑领域' })).toBeFocused()
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

test('勾选多名用户批量添加领域，保留个人标签并同步单人编辑', async ({ page }) => {
  page.on('pageerror', (error) => {
    throw error
  })
  await page.goto('/__frontend__/admin')
  await page.goto('/admin/permissions')
  const token = await page.locator('[data-user-domains-page]').getAttribute('data-csrf-token')
  for (const [subject, domains] of [
    ['ip:192.0.2.10', '电源'],
    ['ip:192.0.2.11', '工控'],
  ]) {
    const response = await page.request.post('/admin/permissions', {
      form: { action: 'domains', subject, domains, csrf_token: token! },
      headers: { Accept: 'application/json' },
    })
    expect(response.ok()).toBe(true)
  }
  await page.reload()
  const first = page.locator('[data-permission-user="ip:192.0.2.10"]')
  const second = page.locator('[data-permission-user="ip:192.0.2.11"]')
  const other = page.locator('[data-permission-user="ip:127.0.0.1"]')
  const all = page.getByRole('checkbox', { name: '选择本页全部用户', exact: true })
  const bulk = page.getByRole('button', { name: '批量添加领域', exact: true })
  await expect(bulk).toBeDisabled()
  await expect(page.locator('body')).not.toContainText('产品领域')
  await first.getByRole('checkbox', { name: '示例用户甲：选择用户', exact: true }).check()
  await expect(all).toBeChecked({ indeterminate: true })
  await all.check()
  await expect(page.locator('.domain-bulk-bar')).toContainText('已选 3 人')
  await page.getByRole('button', { name: '清除选择', exact: true }).click()
  await expect(all).not.toBeChecked()
  await first.getByRole('checkbox', { name: '示例用户甲：选择用户', exact: true }).check()
  await second.getByRole('checkbox', { name: '示例用户乙：选择用户', exact: true }).check()
  await bulk.click()
  const dialog = page.getByRole('dialog', { name: '批量添加领域', exact: true })
  await expect(dialog).toContainText('已选 2 位用户')
  await dialog.getByLabel('新领域', { exact: true }).fill('批量领域')
  await dialog.getByRole('checkbox', { name: '电源', exact: true }).check()
  await dialog.getByRole('button', { name: '添加到所选用户', exact: true }).click()
  await expect(dialog).not.toBeVisible()
  for (const row of [first, second]) {
    await expect(row.locator('.user-domain-tags')).toContainText('批量领域')
    await expect(row.locator('.user-domain-tags')).toContainText('电源')
    await expect(row.locator('input[name=permissions]:checked')).toHaveCount(0)
  }
  await expect(second.locator('.user-domain-tags')).toContainText('工控')
  await expect(other.locator('.user-domain-tags')).toHaveText('未标记')
  await expect(bulk).toBeDisabled()
  await first.getByRole('button', { name: '示例用户甲：编辑领域', exact: true }).click()
  const single = page.getByRole('dialog', { name: '领域', exact: true })
  await expect(single.getByRole('checkbox', { name: '批量领域', exact: true })).toBeChecked()
  await single.getByRole('button', { name: '取消', exact: true }).click()
  await page.reload()
  await expect(second.locator('.user-domain-tags')).toContainText('批量领域')
  await page.getByRole('combobox', { name: '领域筛选' }).selectOption({ label: '批量领域' })
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(page.locator('[data-permission-user]')).toHaveCount(2)
  await expect(bulk).toBeDisabled()
  await all.check()
  await expect(page.locator('.domain-bulk-bar')).toContainText('已选 2 人')
  await page.setViewportSize({ width: 390, height: 844 })
  await bulk.click()
  await expect(dialog).toBeVisible()
  expect(await dialog.evaluate((node) => node.getBoundingClientRect().width <= innerWidth)).toBe(
    true,
  )
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await expect(page.locator('.domain-bulk-bar')).toContainText('已选 2 人')
})
