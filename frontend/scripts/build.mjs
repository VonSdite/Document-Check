import { build } from 'vite'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { readFileSync, readdirSync, realpathSync, unlinkSync } from 'node:fs'

await build()
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const output = join(root, 'app', 'web', 'static', 'frontend')
const assets = join(output, 'assets')
// 仅清理当前构建目录内的过期哈希资源，目录必须解析到本项目中的固定位置。
if (realpathSync(output) !== output || realpathSync(assets) !== assets) {
  throw new Error('Frontend output must be a regular directory inside this project.')
}
const manifest = JSON.parse(readFileSync(join(output, 'manifest.json'), 'utf8'))
const keep = new Set(
  Object.values(manifest).flatMap((chunk) => [
    chunk.file,
    ...(chunk.css || []),
    ...(chunk.assets || []),
  ]),
)
for (const entry of readdirSync(assets, { withFileTypes: true })) {
  if (
    entry.isFile() &&
    /^[\w-]+-[\w-]+\.(js|css)$/.test(entry.name) &&
    !keep.has(`assets/${entry.name}`)
  ) {
    unlinkSync(join(assets, entry.name))
  }
}
