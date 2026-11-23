import { expect, test } from '@playwright/test'

test('管理员可终审普通用户和自己初审的条目', async ({ browser }) => {
  const author = await browser.newContext()
  const admin = await browser.newContext()
  const page = await author.newPage()
  const adminPage = await admin.newPage()
  try {
    const response = await author.request.post('/__frontend__/review-fixture?mode=ip')
    expect(response.ok()).toBe(true)
    const { task_id: taskId } = await response.json()
    await page.goto(`/tasks/${taskId}/reviews`)
    const items = page.locator('[data-audit-item]')
    for (let index = 0; index < 2; index++) {
      const item = items.nth(index)
      await item
        .locator('select[name=acceptance_status]')
        .selectOption(index ? 'rejected' : 'accepted')
      if (index)
        await item.locator('select[name=rejection_reason]').selectOption('evidence_insufficient')
      await item.getByRole('button', { name: '保存初审' }).click()
      await expect(item).toContainText('待终审')
    }
    await adminPage.goto('/__frontend__/admin')
    await adminPage.goto(`/admin/tasks/${taskId}/reviews`)
    await expect(adminPage.locator('.review-notice')).toContainText('本地管理员')
    const adminItems = adminPage.locator('[data-audit-item]')
    const note = '已核对原文中的 <参数> & "条件"。\n确认初审意见。'
    for (let index = 0; index < 2; index++) {
      const item = adminItems.nth(index)
      await item.locator('select[name=decision]').selectOption('confirmed')
      await item.locator('textarea[name=note]').fill(note)
      await item.getByRole('button', { name: '保存终审' }).click()
      await expect(item.locator('select[name=decision]')).toHaveValue('confirmed')
      await expect(item.locator('textarea[name=note]')).toHaveValue(note)
    }
    await adminPage.reload()
    await expect(adminItems.first().locator('select[name=decision]')).toHaveValue('confirmed')
    await expect(adminItems.first().locator('textarea[name=note]')).toHaveValue(note)
    await adminPage.getByRole('link', { name: '检查报告', exact: true }).click()
    await adminPage.getByRole('link', { name: '审核与记录', exact: true }).click()
    await expect(adminItems.last().locator('select[name=decision]')).toHaveValue('confirmed')
    await expect(adminItems.last().locator('textarea[name=note]')).toHaveValue(note)
    await expect(adminPage.locator('[data-report-audit] > .panel').first()).toContainText(
      '终审确认 2/2',
    )
    await adminItems.first().getByRole('button', { name: '保存初审' }).click()
    await expect(adminItems.first()).toContainText('待终审')
    await expect(adminItems.first().locator('select[name=decision]')).toHaveValue('')
    await expect(adminItems.first().locator('textarea[name=note]')).toHaveValue('')
    await adminItems.first().locator('select[name=decision]').selectOption('confirmed')
    await adminItems.first().getByRole('button', { name: '保存终审' }).click()
    await expect(adminPage.locator('[data-report-audit] > .panel').first()).toContainText(
      '终审确认 2/2',
    )
    await adminItems.first().locator('.review-history > summary').click()
    await expect(adminItems.first().locator('.review-history')).toContainText('local_admin:primary')
    await expect(adminItems.first().locator('.review-history')).toContainText('ip:127.0.0.1')
    await expect(adminItems.first().locator('.review-history')).toContainText(note)
  } finally {
    await author.request.delete('/__frontend__/review-fixture')
    await author.close()
    await admin.close()
  }
})

test('报告按权限逐条终审，修改初审后重新审核', async ({ browser }, testInfo) => {
  const author = await browser.newContext()
  const reviewer = await browser.newContext()
  const page = await author.newPage()
  const reviewPage = await reviewer.newPage()
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  reviewPage.on('pageerror', (error) => errors.push(error.message))
  try {
    const fixture = await author.request.post('/__frontend__/review-fixture')
    expect(fixture.ok()).toBe(true)
    const { task_id: taskId } = await fixture.json()
    await page.goto('/__frontend__/review-identity/review-author')
    const items = page.locator('[data-audit-item]')
    await expect(items).toHaveCount(2)
    for (let index = 0; index < 2; index++) {
      const item = items.nth(index)
      await item
        .locator('select[name=acceptance_status]')
        .selectOption(index ? 'rejected' : 'accepted')
      if (index) await item.locator('select[name=rejection_reason]').selectOption('false_positive')
      await item.getByRole('button', { name: '保存初审' }).click()
      await expect(item.getByRole('button', { name: '保存终审' })).toBeVisible()
      if (index === 0) {
        await item.locator('select[name=decision]').selectOption('confirmed')
        await item.getByRole('button', { name: '保存终审' }).click()
        await expect(item).toContainText('确认初审')
      }
    }
    await reviewPage.goto('/__frontend__/review-identity/final-reviewer')
    const reviewItems = reviewPage.locator('[data-audit-item]')
    await expect(reviewPage.getByRole('button', { name: '保存初审' })).toHaveCount(0)
    for (let index = 1; index < 2; index++) {
      const item = reviewItems.nth(index)
      await item.locator('select[name=decision]').selectOption('confirmed')
      await item.getByRole('button', { name: '保存终审' }).click()
      await expect(item).toContainText('确认初审')
    }
    await expect(reviewPage.locator('[data-report-audit] > .panel').first()).toContainText(
      '终审确认 2/2',
    )
    await reviewPage.screenshot({
      path: testInfo.outputPath('final-review-desktop.png'),
      fullPage: true,
    })
    await page.reload()
    await items.first().locator('select[name=acceptance_status]').selectOption('rejected')
    await items.first().locator('select[name=rejection_reason]').selectOption('false_positive')
    await items.first().getByRole('button', { name: '保存初审' }).click()
    await reviewPage.reload()
    await expect(reviewPage.locator('[data-report-audit] > .panel').first()).toContainText(
      '终审确认 1/2',
    )
    await expect(reviewItems.first().locator('select[name=decision]')).toHaveValue('')
    await expect(reviewItems.first().locator('textarea[name=note]')).toHaveValue('')
    await reviewItems.first().locator('select[name=decision]').selectOption('returned')
    await reviewItems
      .first()
      .locator('textarea[name=note]')
      .fill('请核对原文中两个参数的适用条件。')
    await reviewItems.first().getByRole('button', { name: '保存终审' }).click()
    await expect(reviewItems.first().locator('select[name=decision]')).toHaveValue('returned')
    await expect(reviewItems.first().locator('textarea[name=note]')).toHaveValue(
      '请核对原文中两个参数的适用条件。',
    )
    await reviewPage.reload()
    await expect(reviewItems.first().locator('select[name=decision]')).toHaveValue('returned')
    await expect(reviewItems.first().locator('textarea[name=note]')).toHaveValue(
      '请核对原文中两个参数的适用条件。',
    )
    await reviewItems.first().locator('select[name=decision]').selectOption('deferred')
    await reviewItems.first().locator('textarea[name=note]').fill('需要补充原始依据。')
    await reviewItems.first().getByRole('button', { name: '保存终审' }).click()
    await expect(reviewItems.first().locator('select[name=decision]')).toHaveValue('deferred')
    await expect(reviewItems.first().locator('textarea[name=note]')).toHaveValue(
      '需要补充原始依据。',
    )
    await reviewPage.getByRole('link', { name: '终审列表', exact: true }).click()
    await reviewPage.locator(`a[href="/tasks/${taskId}/reviews"]`).click()
    await expect(reviewItems.first().locator('select[name=decision]')).toHaveValue('deferred')
    await expect(reviewItems.first().locator('textarea[name=note]')).toHaveValue(
      '需要补充原始依据。',
    )
    await reviewItems.first().locator('.review-history > summary').click()
    await expect(reviewItems.first().locator('.review-history')).toContainText('review-author')
    await expect(reviewItems.first().locator('.review-history')).toContainText('final-reviewer')
    await expect(reviewItems.first().locator('.review-history')).toContainText(
      '请核对原文中两个参数的适用条件。',
    )
    await reviewPage.setViewportSize({ width: 390, height: 844 })
    await reviewPage.screenshot({
      path: testInfo.outputPath('final-review-mobile.png'),
      fullPage: true,
    })
    expect(
      await reviewPage.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(390)
    expect(errors).toEqual([])
  } finally {
    await author.request.delete('/__frontend__/review-fixture')
    await author.close()
    await reviewer.close()
  }
})
