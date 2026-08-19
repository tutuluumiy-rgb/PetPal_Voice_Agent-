import type { Config } from 'tailwindcss'

/**
 * PetPal Voice Agent — Linear 风格暗黑设计系统 Tailwind 映射
 * 唯一事实源：renderer/styles/design-tokens.css 的 :root CSS 变量
 * 本文件仅做 token → 原子类 的桥接，禁止在此处新造魔法值
 */
export default {
  content: ['./renderer/**/*.{html,vue,ts}'],
  theme: {
    extend: {
      colors: {
        // 分层背景（暗黑层级，0 最深 → 3 最浅）
        surface: {
          0: 'rgb(var(--ds-bg-0) / <alpha-value>)',
          1: 'rgb(var(--ds-bg-1) / <alpha-value>)',
          2: 'rgb(var(--ds-bg-2) / <alpha-value>)',
          3: 'rgb(var(--ds-bg-3) / <alpha-value>)'
        },
        // 文字层级
        fg: {
          primary: 'rgb(var(--ds-fg-primary) / <alpha-value>)',
          secondary: 'rgb(var(--ds-fg-secondary) / <alpha-value>)',
          muted: 'rgb(var(--ds-fg-muted) / <alpha-value>)',
          inverse: 'rgb(var(--ds-fg-inverse) / <alpha-value>)'
        },
        // 品牌强调色（Linear 靛蓝）
        accent: {
          DEFAULT: 'rgb(var(--ds-accent) / <alpha-value>)',
          hover: 'rgb(var(--ds-accent-hover) / <alpha-value>)',
          soft: 'rgb(var(--ds-accent) / 0.14)'
        },
        // 语义状态色
        status: {
          success: 'rgb(var(--ds-success) / <alpha-value>)',
          danger: 'rgb(var(--ds-danger) / <alpha-value>)',
          warning: 'rgb(var(--ds-warning) / <alpha-value>)'
        },
        // hairline 描边
        line: {
          subtle: 'var(--ds-line-subtle)',
          strong: 'var(--ds-line-strong)'
        }
      },
      borderRadius: {
        'ds-sm': 'var(--ds-radius-sm)',
        'ds-md': 'var(--ds-radius-md)',
        'ds-lg': 'var(--ds-radius-lg)',
        'ds-2xl': 'var(--ds-radius-2xl)'
      },
      boxShadow: {
        'ds-sm': 'var(--ds-shadow-sm)',
        'ds-md': 'var(--ds-shadow-md)',
        'ds-lg': 'var(--ds-shadow-lg)',
        'ds-hover': 'var(--ds-shadow-hover)',
        'ds-active': 'var(--ds-shadow-active)'
      },
      fontFamily: {
        sans: ['var(--ds-font-sans)'],
        mono: ['var(--ds-font-mono)']
      },
      letterSpacing: {
        'ds-tight': 'var(--ds-tracking-tight)',
        'ds-normal': 'var(--ds-tracking-normal)'
      },
      transitionTimingFunction: {
        'expo-out': 'var(--ds-ease-expo-out)'
      },
      transitionDuration: {
        'ds-sm': 'var(--ds-duration-sm)',
        'ds-md': 'var(--ds-duration-md)',
        'ds-lg': 'var(--ds-duration-lg)'
      },
      animation: {
        'scale-in': 'scale-in var(--ds-duration-md) var(--ds-ease-expo-out) both',
        'fade-in': 'fade-in var(--ds-duration-sm) ease-out both',
        'slide-up': 'slide-up var(--ds-duration-md) var(--ds-ease-expo-out) both'
      },
      keyframes: {
        'scale-in': {
          '0%': { opacity: '0', transform: 'scale(0.95)' },
          '100%': { opacity: '1', transform: 'scale(1)' }
        },
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' }
        },
        'slide-up': {
          '0%': { opacity: '0', transform: 'translateY(6px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' }
        }
      }
    }
  },
  plugins: []
} satisfies Config
