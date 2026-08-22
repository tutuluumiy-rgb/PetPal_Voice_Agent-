<script setup lang="ts">
/**
 * LogoImg — 统一的品牌 logo（assets/logo.png）
 * 圆形裁切 + 背景适配；尺寸默认 24px。
 */
import logoUrl from '../../assets/logo.png'

withDefaults(defineProps<{ size?: number }>(), { size: 24 })
</script>

<template>
  <img
    :src="logoUrl"
    alt="PetPal"
    class="shrink-0 rounded-full object-cover ring-1 ring-black/10"
    :style="{ width: `${size}px`, height: `${size}px` }"
  />
</template>