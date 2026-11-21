export const pages = [
  { id: 'tasks', selector: '[data-task-list-panel]', load: () => import('@/pages/tasks') },
  { id: 'models', selector: '.provider-modal-form', load: () => import('@/pages/models') },
  { id: 'reports', selector: '[data-task-detail]', load: () => import('@/pages/reports') },
  { id: 'settings', selector: '.settings-section', load: () => import('@/pages/settings') },
  {
    id: 'permissions',
    selector: '[data-permission-form]',
    load: () => import('@/pages/permissions'),
  },
] as const
