import { expect, test } from '@playwright/test'

test('全部报告条目经过双人审核，修改初审后重新终审', async ({ browser }, testInfo) => {
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
      await expect(item).toContainText('请由另一位审核人员终审此条目')
    }
    await reviewPage.goto('/__frontend__/review-identity/final-reviewer')
    const reviewItems = reviewPage.locator('[data-audit-item]')
    await expect(reviewPage.getByRole('button', { name: '保存初审' })).toHaveCount(0)
    for (let index = 0; index < 2; index++) {
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
    await reviewItems.first().locator('select[name=decision]').selectOption('returned')
    await reviewItems
      .first()
      .locator('textarea[name=note]')
      .fill('请核对原文中两个参数的适用条件。')
    await reviewItems.first().getByRole('button', { name: '保存终审' }).click()
    await expect(reviewItems.first()).toContainText('请核对原文中两个参数的适用条件。')
    await reviewItems.first().locator('.review-history > summary').click()
    await expect(reviewItems.first().locator('.review-history')).toContainText('review-author')
    await expect(reviewItems.first().locator('.review-history')).toContainText('final-reviewer')
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
