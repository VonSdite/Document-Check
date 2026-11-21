export function fileIdentity(file: File) {
  return JSON.stringify([file.name, file.size, file.lastModified, file.type])
}
export function mergeFiles(existing: File[], selected: File[], limit = 0) {
  const unique = [
    ...new Map([...existing, ...selected].map((file) => [fileIdentity(file), file])).values(),
  ]
  return {
    files: limit > 0 ? unique.slice(0, limit) : unique,
    omitted: limit > 0 ? Math.max(0, unique.length - limit) : 0,
  }
}
export function fileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
