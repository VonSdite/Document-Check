<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 20 })
const paths: Record<string, string> = {
  document: 'M14 2H6a2 2 0 0 0-2 2v16h16V8z M14 2v6h6 M8 13h8 M8 17h6',
  compare: 'M3 4h7v16H3z M14 4h7v16h-7z M6 9h1 M17 9h1 M6 13h1 M17 13h1',
  language: 'M3 5h12 M9 2v3 M6 5c0 6 5 9 8 10 M12 5c0 5-4 9-9 11 M14 21l4-10 4 10 M15 18h6',
  image: 'M3 3h18v18H3z M3 17l6-6 4 4 3-3 5 5 M14 7h.01',
  video: 'M3 5h12v14H3z M15 10l6-4v12l-6-4z',
  models: 'M12 3l9 5-9 5-9-5z M3 12l9 5 9-5 M3 16l9 5 9-5',
  overview: 'M4 20V10 M10 20V4 M16 20v-7 M22 20H2',
  settings: 'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6',
  users:
    'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M16 3a4 4 0 0 1 0 8 M22 21v-2a4 4 0 0 0-3-4 M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  shield: 'M12 2l8 4v6c0 5-8 10-8 10S4 17 4 12V6z M8 12l3 3 5-6',
  menu: 'M4 6h16 M4 12h16 M4 18h16',
  collapse: 'M4 3h16v18H4z M9 3v18 M16 9l-3 3 3 3',
  expand: 'M4 3h16v18H4z M9 3v18 M13 9l3 3-3 3',
  sun: 'M12 2v2 M12 20v2 M2 12h2 M20 12h2 M5 5l1 1 M18 18l1 1 M5 19l1-1 M18 6l1-1 M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  moon: 'M20 14A9 9 0 0 1 10 3a9 9 0 1 0 10 11',
  close: 'M6 6l12 12 M6 18L18 6',
  upload: 'M12 16V3 M7 8l5-5 5 5 M3 15v6h18v-6',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12 M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
}
</script>
<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
  >
    <path :d="paths[name] || paths.document" />
  </svg>
</template>
