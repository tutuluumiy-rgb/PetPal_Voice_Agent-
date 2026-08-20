import { resolve } from 'path'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import vue from '@vitejs/plugin-vue'

/**
 * electron-vite 构建配置
 * - main:    主进程入口  main/index.ts        → out/main/index.js (CJS)
 * - preload: 预加载脚本  preload/index.ts     → out/preload/index.js (CJS)
 * - renderer:双 HTML 多页入口(pet.html / panel.html)
 *            root 指向 renderer/,两个 BrowserWindow 各自加载对应页面
 */
export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
    build: {
      rollupOptions: {
        input: resolve(__dirname, 'main/index.ts')
      }
    }
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
    build: {
      rollupOptions: {
        input: resolve(__dirname, 'preload/index.ts')
      }
    }
  },
  renderer: {
    root: resolve(__dirname, 'renderer'),
    resolve: {
      alias: {
        '@': resolve(__dirname, 'renderer'),
        // TODO: 后续迭代实现 — 接入根目录 pet-avatar 库（形象驱动模块）
        // '@pet-avatar': resolve(__dirname, '../src/index.ts')
      }
    },
    plugins: [vue()],
    build: {
      rollupOptions: {
        input: {
          pet: resolve(__dirname, 'renderer/pet.html'),
          chat: resolve(__dirname, 'renderer/chat.html'),
          panel: resolve(__dirname, 'renderer/panel.html')
        }
      }
    }
  }
})
