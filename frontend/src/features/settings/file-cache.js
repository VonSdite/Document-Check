import { showToast } from '@/components/ui/toast'
import { showConfirmPopover } from '@/components/ui/confirmation'
import { openModal } from '@/components/ui/modal'
import { formatStorageSize } from '@/features/shared/format'
import { request as fetch } from '@/api/client'

const taskCacheRoot = document.querySelector('[data-task-cache]')
const taskCacheRefreshIntervalMs = 60_000
let taskCacheItems = []
let taskCacheSortField = 'finished_at'
let taskCacheSortDirection = 'asc'
let taskCacheLoading = false
let taskCacheRetryTimer = null

function compareTaskCacheItems(left, right, field) {
  const leftValue = field === 'size_bytes' ? Number(left[field] || 0) : String(left[field] || '')
  const rightValue = field === 'size_bytes' ? Number(right[field] || 0) : String(right[field] || '')
  if (leftValue < rightValue) {
    return -1
  }
  if (leftValue > rightValue) {
    return 1
  }
  return 0
}

function sortedTaskCacheItems() {
  return [...taskCacheItems].sort((left, right) => {
    let result = compareTaskCacheItems(left, right, taskCacheSortField)
    if (taskCacheSortDirection === 'desc') {
      result *= -1
    }
    if (result !== 0) {
      return result
    }
    const secondaryField = taskCacheSortField === 'finished_at' ? 'size_bytes' : 'finished_at'
    result = compareTaskCacheItems(left, right, secondaryField)
    return result || Number(left.id || 0) - Number(right.id || 0)
  })
}

function selectedTaskCacheIds() {
  return Array.from(document.querySelectorAll('[data-task-cache-item]:checked')).map((checkbox) =>
    Number(checkbox.dataset.taskCacheItem),
  )
}

function updateTaskCacheSortHeadings() {
  document.querySelectorAll('[data-task-cache-sort-heading]').forEach((heading) => {
    const active = heading.dataset.taskCacheSortHeading === taskCacheSortField
    heading.setAttribute(
      'aria-sort',
      active ? (taskCacheSortDirection === 'asc' ? 'ascending' : 'descending') : 'none',
    )
    const icon = heading.querySelector('button span')
    if (icon) {
      icon.textContent = active ? (taskCacheSortDirection === 'asc' ? '↑' : '↓') : ''
    }
  })
}

function updateTaskCacheSelection() {
  const checkboxes = Array.from(document.querySelectorAll('[data-task-cache-item]'))
  const selected = checkboxes.filter((checkbox) => checkbox.checked)
  const toggle = document.querySelector('[data-task-cache-toggle]')
  const cleanupButton = document.querySelector('[data-task-cache-cleanup]')
  const selection = document.querySelector('[data-task-cache-selection]')
  const selectedSize = selected.reduce(
    (total, checkbox) => total + Number(checkbox.dataset.sizeBytes || 0),
    0,
  )

  if (toggle instanceof HTMLInputElement) {
    toggle.disabled = checkboxes.length === 0
    toggle.checked = checkboxes.length > 0 && selected.length === checkboxes.length
    toggle.indeterminate = selected.length > 0 && selected.length < checkboxes.length
  }
  if (cleanupButton instanceof HTMLButtonElement) {
    cleanupButton.disabled = selected.length === 0
  }
  if (selection) {
    selection.textContent = selected.length
      ? `已选择 ${selected.length} 个任务，共 ${formatStorageSize(selectedSize)}`
      : '未选择任务'
  }
}

function renderTaskCacheItems(selectedIds = new Set()) {
  const rows = document.querySelector('[data-task-cache-rows]')
  const tableWrap = document.querySelector('[data-task-cache-table-wrap]')
  const empty = document.querySelector('[data-task-cache-empty]')
  const summary = document.querySelector('[data-task-cache-dialog-summary]')
  if (!rows || !tableWrap || !empty || !summary) {
    return
  }
  const modal = document.getElementById('task-cache-modal')
  // 设置页加载时只更新摘要，不提前创建数千个表格行；用户打开弹窗时
  // 再渲染明细，避免浏览器主线程被大 DOM 占满。
  if (modal instanceof HTMLDialogElement && !modal.open) {
    return
  }

  rows.replaceChildren()
  const fragment = document.createDocumentFragment()
  sortedTaskCacheItems().forEach((item) => {
    const row = document.createElement('tr')

    const selectCell = document.createElement('td')
    const checkbox = document.createElement('input')
    checkbox.type = 'checkbox'
    checkbox.dataset.taskCacheItem = String(item.id)
    checkbox.dataset.sizeBytes = String(item.size_bytes || 0)
    checkbox.checked = selectedIds.has(Number(item.id))
    checkbox.setAttribute('aria-label', `选择清理任务：${item.title || `任务 #${item.id}`}`)
    selectCell.appendChild(checkbox)

    const titleCell = document.createElement('td')
    const link = document.createElement('a')
    link.className = 'strong-link task-cache-item-title'
    link.href = item.report_url
    link.target = '_blank'
    link.rel = 'noopener'
    link.title = '查看报告'
    link.textContent = item.title || `任务 #${item.id}`
    const meta = document.createElement('div')
    meta.className = 'task-cache-item-meta'
    meta.textContent = `${item.task_type_label || '任务'} · 任务 #${item.id}`
    titleCell.append(link, meta)

    const timeCell = document.createElement('td')
    timeCell.textContent = item.finished_at || '-'

    const sizeCell = document.createElement('td')
    sizeCell.className = 'task-cache-size'
    sizeCell.textContent = formatStorageSize(item.size_bytes)

    const countCell = document.createElement('td')
    countCell.className = 'task-cache-count'
    countCell.textContent = String(item.file_count || 0)

    row.append(selectCell, titleCell, timeCell, sizeCell, countCell)
    fragment.appendChild(row)
  })
  rows.appendChild(fragment)

  const cleanableSize = taskCacheItems.reduce(
    (total, item) => total + Number(item.size_bytes || 0),
    0,
  )
  const hasItems = taskCacheItems.length > 0
  tableWrap.hidden = !hasItems
  empty.hidden = hasItems
  summary.textContent = hasItems
    ? `共 ${taskCacheItems.length} 个任务、${formatStorageSize(cleanableSize)} 可清理。默认按任务结束时间、大小升序显示。`
    : '当前没有仍占用任务文件的已结束任务。'
  updateTaskCacheSortHeadings()
  updateTaskCacheSelection()
}

function updateTaskCacheSummary(data) {
  if (!taskCacheRoot) {
    return
  }
  const total = taskCacheRoot.querySelector('[data-task-cache-total]')
  const uploads = taskCacheRoot.querySelector('[data-task-cache-uploads]')
  const generated = taskCacheRoot.querySelector('[data-task-cache-generated]')
  const cleanable = taskCacheRoot.querySelector('[data-task-cache-cleanable]')
  const openButton = taskCacheRoot.querySelector('[data-task-cache-open]')
  const actionStatus = taskCacheRoot.querySelector('[data-task-cache-action-status]')
  const cleanableCount = Number(data.cleanable_count || 0)
  if (total) {
    total.textContent = formatStorageSize(data.total_size_bytes)
  }
  if (uploads) {
    uploads.textContent = formatStorageSize(data.upload_size_bytes)
  }
  if (generated) {
    generated.textContent = formatStorageSize(data.generated_size_bytes)
  }
  if (cleanable) {
    cleanable.textContent = `可清理 ${formatStorageSize(data.cleanable_size_bytes)}`
  }
  if (openButton instanceof HTMLButtonElement) {
    openButton.hidden = cleanableCount === 0
    openButton.textContent =
      cleanableCount === 1 ? '查看 1 个可清理任务' : `查看 ${cleanableCount} 个可清理任务`
  }
  if (actionStatus) {
    actionStatus.hidden = cleanableCount > 0
    actionStatus.textContent = '暂无可清理文件'
  }
}

async function loadTaskFileCache({ notifyError = false } = {}) {
  if (!taskCacheRoot || taskCacheLoading) {
    return
  }
  const url = new URL(taskCacheRoot.dataset.taskCacheUrl, window.location.href)
  url.searchParams.set('background', '1')
  if (!url) {
    return
  }
  const selectedIds = new Set(selectedTaskCacheIds())
  taskCacheLoading = true
  const dialogSummary = document.querySelector('[data-task-cache-dialog-summary]')
  if (dialogSummary) {
    dialogSummary.textContent = '正在统计可清理内容...'
  }
  try {
    const response = await fetch(url, {
      headers: { Accept: 'application/json', 'X-Requested-With': 'fetch' },
      credentials: 'same-origin',
      cache: 'no-store',
    })
    const data = await response.json()
    if (!response.ok) {
      throw new Error(data.error || '缓存统计失败。')
    }
    if (data.ready === false) {
      const dialogSummary = document.querySelector('[data-task-cache-dialog-summary]')
      if (dialogSummary) {
        dialogSummary.textContent = '正在后台统计任务文件，稍后自动刷新...'
      }
      if (taskCacheRetryTimer === null) {
        taskCacheRetryTimer = window.setTimeout(() => {
          taskCacheRetryTimer = null
          loadTaskFileCache({ notifyError })
        }, 1000)
      }
      return
    }
    taskCacheItems = Array.isArray(data.items) ? data.items : []
    updateTaskCacheSummary(data)
    renderTaskCacheItems(selectedIds)
  } catch (error) {
    const message = error instanceof Error ? error.message : '缓存统计失败。'
    if (dialogSummary) {
      dialogSummary.textContent = message
    }
    const total = taskCacheRoot.querySelector('[data-task-cache-total]')
    if (total) {
      total.textContent = '统计失败'
    }
    if (notifyError) {
      showToast(message, 'error')
    }
  } finally {
    taskCacheLoading = false
  }
}

async function cleanupSelectedTaskFileCache() {
  if (!taskCacheRoot) {
    return
  }
  const taskIds = selectedTaskCacheIds()
  const cleanupButton = document.querySelector('[data-task-cache-cleanup]')
  if (!taskIds.length || !(cleanupButton instanceof HTMLButtonElement)) {
    return
  }
  const url = taskCacheRoot.dataset.taskCacheCleanupUrl
  const originalText = cleanupButton.textContent
  cleanupButton.disabled = true
  cleanupButton.textContent = '清理中...'
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'X-Requested-With': 'fetch',
      },
      credentials: 'same-origin',
      body: JSON.stringify({ task_ids: taskIds }),
    })
    const data = await response.json()
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || '清理失败，请稍后重试。')
    }
    const cleanedCount = Array.isArray(data.cleaned_ids) ? data.cleaned_ids.length : 0
    const failed = Array.isArray(data.failed) ? data.failed : []
    if (cleanedCount) {
      showToast(`已清理 ${cleanedCount} 个任务，释放 ${formatStorageSize(data.freed_size_bytes)}。`)
    }
    if (failed.length) {
      showToast(`${failed.length} 个任务清理失败：${failed[0].error}`, 'error')
    }
    if (!cleanedCount && !failed.length) {
      showToast('所选任务已清理或不再可清理。', 'error')
    }
    await loadTaskFileCache()
  } catch (error) {
    showToast(error instanceof Error ? error.message : '清理失败，请稍后重试。', 'error')
  } finally {
    cleanupButton.textContent = originalText
    updateTaskCacheSelection()
  }
}

document.addEventListener('click', (event) => {
  const openButton = event.target.closest('[data-task-cache-open]')
  if (openButton) {
    event.preventDefault()
    openModal('task-cache-modal')
    loadTaskFileCache({ notifyError: true })
    return
  }

  const sortButton = event.target.closest('[data-task-cache-sort]')
  if (sortButton) {
    event.preventDefault()
    const selectedIds = new Set(selectedTaskCacheIds())
    const field = sortButton.dataset.taskCacheSort
    if (field === taskCacheSortField) {
      taskCacheSortDirection = taskCacheSortDirection === 'asc' ? 'desc' : 'asc'
    } else {
      taskCacheSortField = field
      taskCacheSortDirection = 'asc'
    }
    renderTaskCacheItems(selectedIds)
    return
  }

  const cleanupButton = event.target.closest('[data-task-cache-cleanup]')
  if (cleanupButton instanceof HTMLButtonElement) {
    event.preventDefault()
    const taskIds = selectedTaskCacheIds()
    const selectedSize = Array.from(
      document.querySelectorAll('[data-task-cache-item]:checked'),
    ).reduce((total, checkbox) => total + Number(checkbox.dataset.sizeBytes || 0), 0)
    showConfirmPopover(
      cleanupButton,
      `确认清理选中的 ${taskIds.length} 个任务文件，共 ${formatStorageSize(selectedSize)}？任务历史和报告继续保留。`,
      cleanupSelectedTaskFileCache,
    )
  }
})

document.addEventListener('change', (event) => {
  const target = event.target
  if (!(target instanceof HTMLInputElement)) {
    return
  }
  if (target.matches('[data-task-cache-toggle]')) {
    document.querySelectorAll('[data-task-cache-item]').forEach((checkbox) => {
      checkbox.checked = target.checked
    })
    updateTaskCacheSelection()
    return
  }
  if (target.matches('[data-task-cache-item]')) {
    updateTaskCacheSelection()
  }
})

if (taskCacheRoot) {
  loadTaskFileCache()
  window.setInterval(() => {
    if (document.visibilityState === 'visible') {
      loadTaskFileCache()
    }
  }, taskCacheRefreshIntervalMs)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      loadTaskFileCache()
    }
  })
}
