import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests/e2e',
  outputDir: '../instance/frontend-test-results',
  timeout: 30_000,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:8768',
    browserName: 'chromium',
    channel: 'chrome',
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'uv run python frontend/tests/server.py',
    cwd: '..',
    url: 'http://127.0.0.1:8768',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
})
