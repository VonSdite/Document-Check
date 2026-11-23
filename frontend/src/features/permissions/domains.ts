import { reactive, ref } from 'vue'
import type { ProductDomain } from '@/api/user-domains'

export const availableDomains = ref<ProductDomain[]>([])
export const assignedDomains = reactive(new Map<string, ProductDomain[]>())
export const selectedSubjects = ref<string[]>([])
