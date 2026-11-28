import { expect, test } from '@playwright/test'

const taskTypes = [
  ['document_check', '单文档'],
  ['consistency_check', '多文档'],
  ['language_consistency_check', '跨语种'],
  ['image_check', '图片'],
  ['video_check', '视频'],
] as const

for (const [taskType, label] of taskTypes) {
  test(`${label}报告在用户端和管理端使用表格并保存初审`, async ({ page }, testInfo) => {
    const errors: string[] = []
    page.on('pageerror', (error) => errors.push(error.message))
    try {
      const fixture = await page.request.post(
        `/__frontend__/review-fixture?mode=ip&task_type=${taskType}`,
      )
      expect(fixture.ok()).toBe(true)
      const { task_id: taskId } = await fixture.json()
      await page.setViewportSize({ width: 1440, height: 1000 })
      await page.goto(`/tasks/${taskId}`)
      const table = page.locator('.report-table').first()
      const rows = table.locator('tr[data-report-item]')
      await expect(rows).toHaveCount(2)
      await expect(page.locator('.report-review-card, [data-final-review-form]')).toHaveCount(0)
      await expect(page.locator('[data-save-initial]')).toHaveCount(0)
      const expectedHeaders =
        taskType === 'image_check'
          ? ['条目', 'AI检查结论', 'AI判定', '是否认可 AI 结论']
          : [
              '条目',
              '严重程度',
              '证据可信度',
              '问题类型',
              '位置',
              '原文/证据',
              '问题描述',
              '影响',
              '修改建议',
              'AI判定',
              '是否认可 AI 结论',
            ]
      await expect(table.locator('thead th')).toHaveText(expectedHeaders)
      await rows.first().locator('input[value=accepted]').check()
      await expect(rows.first().locator('[data-initial-save-status]')).toHaveText('已保存')
      await rows.last().locator('input[value=rejected]').check()
      await page.locator('[data-report-rejection-choice=evidence_insufficient]').click()
      await expect(rows.last().locator('[data-initial-save-status]')).toHaveText('已保存')
      await page.reload()
      await expect(rows.first().locator('input[value=accepted]')).toBeChecked()
      await expect(rows.last().locator('input[value=rejected]')).toBeChecked()
      await expect(rows.last().locator('[data-report-rejection-reason]')).toHaveValue(
        'evidence_insufficient',
      )
      await page.getByRole('link', { name: '报告终审与记录', exact: true }).click()
      await expect(page.locator('[data-report-audit] [data-report-item]')).toHaveCount(1)
      await expect(page.locator('[data-final-review-form]')).toHaveCount(0)
      await expect(page.locator('[data-report-acceptance-status]')).toHaveCount(0)
      await page.getByRole('link', { name: '检查报告', exact: true }).click()
      await expect(rows).toHaveCount(2)

      await page.goto('/__frontend__/admin')
      await page.goto(`/admin/tasks/${taskId}`)
      await expect(table.locator('thead th')).toHaveText(expectedHeaders)
      await expect(rows.first().locator('input[value=accepted]')).toBeChecked()
      await expect(page.locator('.report-review-card, [data-final-review-form]')).toHaveCount(0)
      await page.getByRole('link', { name: '报告终审与记录', exact: true }).click()
      await expect(page.locator('[data-report-audit] [data-report-item]')).toHaveCount(1)
      await expect(page.locator('[data-final-review-form]')).toHaveCount(1)
      await expect(page.locator('[data-report-acceptance-status]')).toHaveCount(0)
      await page.getByRole('link', { name: '检查报告', exact: true }).click()
      await expect(rows).toHaveCount(2)
      await table.screenshot({ path: testInfo.outputPath(`${taskType}-desktop.png`) })
      await page.getByRole('button', { name: '切换主题' }).click()
      await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
      await page.setViewportSize({ width: 390, height: 844 })
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
        390,
      )
      await rows.last().locator('input[value=rejected]').scrollIntoViewIfNeeded()
      await expect(rows.last().locator('input[value=rejected]')).toBeVisible()
      await table.locator('..').screenshot({ path: testInfo.outputPath(`${taskType}-mobile.png`) })

      const exported = await page.request.get(`/admin/tasks/${taskId}/export`)
      expect(exported.ok()).toBe(true)
      const exportPage = await page.context().newPage()
      try {
        await exportPage.setContent(await exported.text())
        await expect(exportPage.locator('.report-table tbody tr')).toHaveCount(2)
        await expect(exportPage.locator('.report-review-card')).toHaveCount(0)
        await expect(exportPage.locator('.report-table th').first()).toHaveText('条目')
      } finally {
        await exportPage.close()
      }
      const excel = await page.request.get(`/admin/tasks/${taskId}/export.xlsx`)
      expect(excel.ok()).toBe(true)
      expect(excel.headers()['content-type']).toContain('spreadsheetml.sheet')
      expect(errors).toEqual([])
    } finally {
      await page.request.delete('/__frontend__/review-fixture')
    }
  })
}
