import TaskComposer from '@/features/tasks/TaskComposer.vue'
import { readTaskComposer } from '@/features/tasks/contract'
import { mountComponent } from '@/app/mount'
import '@/features/tasks/list'
document.querySelectorAll<HTMLElement>('[data-component="task-composer"]').forEach((root) => {
  mountComponent(root, TaskComposer, { ...readTaskComposer(root) })
})
