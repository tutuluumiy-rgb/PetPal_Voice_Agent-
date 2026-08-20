/**
 * 宠物动画 — 类型定义
 * --------------------------------------------------------------------------
 * PetAnimState：宠物表达的状态集合。
 * FrameManifest：按状态描述 PNG 序列帧（帧 URL / fps / 是否循环 / 是否就绪）。
 */

/** 宠物动画状态（含 4 段过渡态，过渡为单向播放） */
export type PetAnimState =
  | 'idle'
  | 'speaking'
  | 'listening'
  | 'working'
  | 'thinking'
  | 'happy'
  | 'sad'
  | 'sleeping'
  | 'surprised'
  | 'trans_idle_speak'
  | 'trans_speak_idle'
  | 'trans_speak_work'
  | 'trans_work_speak'

/** 情感标签（预留：未来由语音情绪分析驱动，现阶段可不强制使用） */
export type EmotionTag =
  | 'neutral'
  | 'happy'
  | 'sad'
  | 'angry'
  | 'surprised'
  | 'excited'
  | 'tired'

/** 单个动画状态的帧配置 */
export interface FrameStateConfig {
  /** 帧 URL 列表（按播放顺序） */
  frames: string[]
  /** 播放帧率 */
  fps: number
  /** 是否循环播放（过渡动画 loop=false） */
  loop: boolean
  /** 该套素材是否已就绪（懒加载默认为 false，加载成功置 true） */
  ready: boolean
}

/** 动画素材清单：state -> 帧配置 */
export type FrameManifest = Partial<Record<PetAnimState, FrameStateConfig>>
