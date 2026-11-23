import { expect, test, type Locator, type Page } from '@playwright/test'

async function initialReview(page: Page, item: Locator, rejected = false) {
  await item
    .locator(`input[data-report-acceptance-status][value=${rejected ? 'rejected' : 'accepted'}]`)
    .check()
  if (rejected) await page.locator('[data-report-rejection-choice=evidence_insufficient]').click()
  await expect(item.locator('[data-initial-save-status]')).toHaveText('已保存')
}

async function finalReview(item: Locator, decision: string, note: string) {
  await item.locator('select[name=decision]').selectOption(decision)
  await item.locator('textarea[name=note]').fill(note)
  await item.getByRole('button', { name: '保存终审' }).click()
  await expect(item.locator('[data-final-save-status]')).toHaveText('已保存')
  await expect(item.locator('select[name=decision]')).toHaveValue(decision)
  await expect(item.locator('textarea[name=note]')).toHaveValue(note)
}

test('管理员在报告页内终审，无需横向滚动且重新进入保留意见', async ({ browser }, testInfo) => {
  const author = await browser.newContext()
  const admin = await browser.newContext()
  const page = await author.newPage()
  const adminPage = await admin.newPage()
  try {
    const response = await author.request.post('/__frontend__/review-fixture?mode=ip')
    expect(response.ok()).toBe(true)
    const { task_id: taskId } = await response.json()
    await page.goto(`/tasks/${taskId}`)
    const items = page.locator('[data-report-item]')
    await initialReview(page, items.first())
    await initialReview(page, items.last(), true)
    await expect(page.getByRole('button', { name: '保存终审' })).toHaveCount(0)
    await adminPage.goto('/__frontend__/admin')
    await adminPage.goto(`/admin/tasks/${taskId}/reviews`)
    await expect(adminPage).toHaveURL(new RegExp(`/admin/tasks/${taskId}#report-reviews$`))
    await expect(adminPage.locator('.review-notice')).toContainText('本地管理员')
    const adminItems = adminPage.locator('[data-report-item]')
    const note = '已核对原文中的 <参数> & "条件"。\n确认初审意见。'
    for (let index = 0; index < 2; index++)
      await finalReview(adminItems.nth(index), 'confirmed', note)
    await expect(adminPage.locator('[data-report-review-progress]')).toContainText('终审确认 2/2')
    await adminPage.reload()
    await expect(adminItems.first().locator('select[name=decision]')).toHaveValue('confirmed')
    await expect(adminItems.first().locator('textarea[name=note]')).toHaveValue(note)
    await adminPage.getByRole('link', { name: '终审列表', exact: true }).click()
    await adminPage.locator(`a[href="/admin/tasks/${taskId}#report-reviews"]`).click()
    await expect(adminItems.last().locator('select[name=decision]')).toHaveValue('confirmed')
    await expect(adminItems.last().locator('textarea[name=note]')).toHaveValue(note)
    await adminItems.first().getByRole('button', { name: '保存初审' }).click()
    await expect(adminItems.first().locator('[data-initial-save-status]')).toHaveText('已保存')
    await expect(adminItems.first()).toContainText('需重新终审')
    await expect(adminItems.first().locator('select[name=decision]')).toHaveValue('')
    await expect(adminItems.first().locator('textarea[name=note]')).toHaveValue('')
    await finalReview(adminItems.first(), 'confirmed', '本次初审已核对。')
    await adminItems.first().locator('[data-review-history-toggle]').click()
    const history = adminPage.locator('.report-history-row').first()
    await expect(history).toBeVisible()
    await expect(history).toContainText('local_admin:primary')
    await expect(history).toContainText('ip:127.0.0.1')
    await expect(history).toContainText(note)
    for (const width of [1440, 1280, 1024]) {
      await adminPage.setViewportSize({ width, height: 1000 })
      expect(
        await adminPage.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(width)
      const finalBounds = await adminItems.first().locator('[data-final-review-cell]').boundingBox()
      expect(finalBounds!.x).toBeGreaterThanOrEqual(0)
      expect(finalBounds!.x + finalBounds!.width).toBeLessThanOrEqual(width)
      const entry = adminPage.locator('.report-review-entry').first()
      expect(await entry.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true)
    }
    await adminPage.setViewportSize({ width: 1440, height: 1000 })
    const historyBounds = await history.locator('.review-history').boundingBox()
    const entryBounds = await adminPage.locator('.report-review-entry').first().boundingBox()
    expect(historyBounds!.x).toBeGreaterThanOrEqual(entryBounds!.x)
    expect(historyBounds!.x + historyBounds!.width).toBeLessThanOrEqual(
      entryBounds!.x + entryBounds!.width,
    )
    await adminItems.first().locator('[data-review-history-toggle]').click()
    await adminPage
      .locator('.report-review-entry')
      .first()
      .screenshot({
        path: testInfo.outputPath('review-layout-desktop.png'),
      })
  } finally {
    await author.request.delete('/__frontend__/review-fixture')
    await author.close()
    await admin.close()
  }
})

test('报告页区分初审和终审权限，初审修改后重新终审', async ({ browser }, testInfo) => {
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
    const items = page.locator('[data-report-item]')
    await expect(items).toHaveCount(2)
    await initialReview(page, items.first())
    await finalReview(items.first(), 'confirmed', '确认本人初审。')
    await initialReview(page, items.last(), true)
    await reviewPage.goto('/__frontend__/review-identity/final-reviewer')
    const reviewItems = reviewPage.locator('[data-report-item]')
    await expect(reviewPage.getByRole('button', { name: '保存初审' })).toHaveCount(0)
    await expect(reviewPage.locator('[data-report-acceptance-status]')).toHaveCount(0)
    await finalReview(reviewItems.last(), 'confirmed', '确认不认可该条结论。')
    await expect(reviewPage.locator('[data-report-review-progress]')).toContainText('终审确认 2/2')
    await page.reload()
    await initialReview(page, items.first(), true)
    await reviewPage.reload()
    await expect(reviewPage.locator('[data-report-review-progress]')).toContainText('终审确认 1/2')
    await expect(reviewItems.first().locator('select[name=decision]')).toHaveValue('')
    await expect(reviewItems.first().locator('textarea[name=note]')).toHaveValue('')
    const returnedNote = '请核对原文中两个参数的适用条件。'
    await finalReview(reviewItems.first(), 'returned', returnedNote)
    await reviewPage.reload()
    await expect(reviewItems.first().locator('select[name=decision]')).toHaveValue('returned')
    await expect(reviewItems.first().locator('textarea[name=note]')).toHaveValue(returnedNote)
    await finalReview(reviewItems.first(), 'deferred', '需要补充原始依据。')
    await reviewPage.getByRole('link', { name: '终审列表', exact: true }).click()
    await reviewPage.locator(`a[href="/tasks/${taskId}#report-reviews"]`).click()
    await expect(reviewItems.first().locator('select[name=decision]')).toHaveValue('deferred')
    await expect(reviewItems.first().locator('textarea[name=note]')).toHaveValue(
      '需要补充原始依据。',
    )
    await reviewItems.first().locator('[data-review-history-toggle]').click()
    const history = reviewPage.locator('.report-history-row').first()
    await expect(history).toContainText('review-author')
    await expect(history).toContainText('final-reviewer')
    await expect(history).toContainText(returnedNote)
    await reviewPage.setViewportSize({ width: 390, height: 844 })
    const finalBounds = await reviewItems.first().locator('[data-final-review-cell]').boundingBox()
    expect(finalBounds!.x).toBeGreaterThanOrEqual(0)
    expect(finalBounds!.x + finalBounds!.width).toBeLessThanOrEqual(390)
    await reviewItems.first().locator('select[name=decision]').scrollIntoViewIfNeeded()
    const noteWidth = await reviewItems
      .first()
      .locator('textarea[name=note]')
      .evaluate((node) => node.getBoundingClientRect().width)
    expect(noteWidth).toBeGreaterThan(180)
    await reviewItems.first().locator('[data-review-history-toggle]').click()
    await reviewPage
      .locator('.report-review-entry')
      .first()
      .screenshot({
        path: testInfo.outputPath('review-layout-mobile.png'),
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

test('终审保存失败和版本冲突时保留输入，不改写审核记录', async ({ browser }) => {
  const context = await browser.newContext()
  const page = await context.newPage()
  const otherPage = await context.newPage()
  try {
    const fixture = await context.request.post('/__frontend__/review-fixture')
    const { task_id: taskId } = await fixture.json()
    await page.goto('/__frontend__/review-identity/review-author')
    const item = page.locator('[data-report-item]').first()
    await initialReview(page, item)
    await item.locator('select[name=decision]').selectOption('returned')
    await item.locator('textarea[name=note]').fill('仍需核对的审核说明。')
    await page.route(
      `**/tasks/${taskId}/reviews`,
      (route) =>
        route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ ok: false, error: '暂时无法保存，请重试。' }),
        }),
      { times: 1 },
    )
    await item.getByRole('button', { name: '保存终审' }).click()
    await expect(item.locator('[data-final-save-status]')).toHaveText('暂时无法保存，请重试。')
    await expect(item.locator('select[name=decision]')).toHaveValue('returned')
    await expect(item.locator('textarea[name=note]')).toHaveValue('仍需核对的审核说明。')
    await otherPage.goto(`/tasks/${taskId}`)
    await otherPage
      .locator('[data-report-item]')
      .first()
      .getByRole('button', { name: '保存初审' })
      .click()
    await expect(otherPage.locator('[data-initial-save-status]').first()).toHaveText('已保存')
    await item.getByRole('button', { name: '保存终审' }).click()
    await expect(item.locator('[data-final-save-status]')).toContainText('已更新')
    await expect(item.locator('textarea[name=note]')).toHaveValue('仍需核对的审核说明。')
    await otherPage.reload()
    await expect(otherPage.locator('select[name=decision]').first()).toHaveValue('')
    await page.reload()
    await finalReview(item, 'returned', '重新核对后提交的说明。')
  } finally {
    await context.request.delete('/__frontend__/review-fixture')
    await context.close()
  }
})
