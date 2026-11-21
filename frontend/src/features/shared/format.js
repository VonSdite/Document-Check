function formatFileSize(bytes) {
  if (!Number.isFinite(bytes)) {
    return ''
  }
  if (bytes < 1024 * 1024) {
    return `${Math.max(0.1, bytes / 1024).toFixed(1)} KB`
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function formatStorageSize(bytes) {
  let value = Number(bytes)
  if (!Number.isFinite(value) || value <= 0) {
    return '0 B'
  }
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  const digits = unitIndex === 0 ? 0 : unitIndex >= 3 ? 2 : 1
  return `${value.toFixed(digits)} ${units[unitIndex]}`
}

export { formatFileSize, formatStorageSize }
