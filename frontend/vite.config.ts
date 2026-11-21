import { fileURLToPath, URL } from 'node:url'
import { readFile, writeFile } from 'node:fs/promises'
import { build as buildOffline } from 'esbuild'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const reportEntry = fileURLToPath(new URL('./src/report-export.ts', import.meta.url))
const reportStyles = fileURLToPath(new URL('./src/styles/report-export.css', import.meta.url))
const output = new URL('../app/web/static/frontend/', import.meta.url)

export default defineConfig({
  plugins: [
    vue(),
    {
      name: 'offline-report',
      apply: 'build',
      buildStart() {
        this.addWatchFile(reportEntry)
        this.addWatchFile(reportStyles)
      },
      async writeBundle() {
        // 离线报告内嵌资源，使用独立 IIFE 与完整样式，直接打开文件即可使用。
        await buildOffline({
          entryPoints: [reportEntry],
          outfile: fileURLToPath(new URL('report-export.js', output)),
          bundle: true,
          format: 'iife',
          minify: true,
          target: 'es2022',
          charset: 'utf8',
        })
        const path = new URL('manifest.json', output)
        const manifest = JSON.parse(await readFile(path, 'utf8'))
        manifest['src/report-export.ts'] = {
          file: 'report-export.js',
          css: ['report-export.css'],
          isEntry: true,
        }
        await writeFile(path, JSON.stringify(manifest, null, 2) + '\n', 'utf8')
      },
    },
  ],
  base: './',
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  build: {
    outDir: '../app/web/static/frontend',
    emptyOutDir: false,
    manifest: 'manifest.json',
    rollupOptions: { input: fileURLToPath(new URL('./src/main.ts', import.meta.url)) },
  },
})
