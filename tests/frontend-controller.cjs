const { readFileSync } = require('node:fs');
const { join } = require('node:path');

// 虚拟 DOM 测试在模块边界注入网络和通知依赖，执行正式控制器中的交互逻辑。
exports.controllerSource = (path) => readFileSync(join(__dirname, '../frontend/src', path), 'utf8')
  .replace(/^import .*\n/gm, '')
  .replace(/^export \{[^}]*\};?\s*$/gm, '');
