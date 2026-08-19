/**
 * 悬浮宠物窗口渲染入口
 */
import { createApp } from 'vue'
import PetWindow from './components/pet-window/PetWindow.vue'
// 设计系统 token 必须先于 index.css 加载（tailwind 颜色类引用其中的 CSS 变量）
import './styles/design-tokens.css'
import './styles/index.css'

createApp(PetWindow).mount('#app')
