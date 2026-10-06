import { IconArrowUp, IconClock, IconFile, IconLayers } from './Icons'

const STARTERS = [
  { label: '计算核验', icon: IconClock, text: '请计算 (128 + 64) × 3，并核验计算结果。' },
  { label: '整理文本', icon: IconFile, text: '请将这段文字整理成三点：本周先确认目标，再拆分任务，最后检查结果；每天记录进度，遇到阻塞及时调整。' },
  { label: '制定计划', icon: IconLayers, text: '帮我制定一个三天学习 HTTP 基础的计划，写清每天的目标与练习。' },
]

export function EmptyState() {
  return (
    <div className="empty">
      <span className="empty__eyebrow"><span className="empty__spark" /> YOUR EVERYDAY AGENT</span>
      <h1 className="empty__title">有什么需要一起解决？</h1>
      <p className="empty__subtitle">从一个问题开始，整理思路，让下一步更清晰。</p>
    </div>
  )
}

export function StarterPrompts({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="empty__prompts" data-testid="starter-prompts" aria-label="试试这些问题">
      {STARTERS.map(({ label, icon: Icon, text }) => (
        <button key={label} type="button" className="prompt-chip" onClick={() => onPick(text)}>
          <Icon size={15} /><span>{label}</span><IconArrowUp size={12} className="prompt-chip__arrow" />
        </button>
      ))}
    </div>
  )
}
