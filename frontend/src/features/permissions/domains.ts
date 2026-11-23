import { ref } from 'vue'
import type { ProductDomain } from '@/api/user-domains'

export const availableDomains = ref<ProductDomain[]>([])
