"use client";

import { motion } from "framer-motion";
import { useSteppedVisualization } from "@/hooks/useSteppedVisualization";
import { StepControls } from "@/components/visualizations/shared/step-controls";
import { useLocale } from "@/lib/i18n";

const STEPS_ZH = [
  { title: "会话启动", description: "加载 MEMORY.md 和 USER.md 快照，冻结进 system prompt。" },
  { title: "用户提问", description: "用户说'记住我喜欢 Python'。模型决定调用 memory 工具。" },
  { title: "memory add", description: "memory(op='add', file='USER.md', content='偏好 Python')。写入磁盘。" },
  { title: "磁盘已更新", description: "USER.md 已写入新内容。但当前 session 的 prompt 快照不变。" },
  { title: "会话继续", description: "模型回复确认。本轮 prompt 里的记忆仍是旧快照。" },
  { title: "下次会话", description: "新会话启动，重新加载 MEMORY.md / USER.md，新内容生效。" },
];

const STEPS_EN = [
  { title: "Session Start", description: "Load MEMORY.md & USER.md snapshots, freeze into system prompt." },
  { title: "User Request", description: "User says 'remember I prefer Python'. Model calls memory tool." },
  { title: "memory add", description: "memory(op='add', file='USER.md', content='prefers Python'). Written to disk." },
  { title: "Disk Updated", description: "USER.md updated on disk. But current session prompt snapshot unchanged." },
  { title: "Session Continues", description: "Model confirms. In-prompt memory still shows old snapshot." },
  { title: "Next Session", description: "New session loads fresh MEMORY.md / USER.md. New content takes effect." },
];

const FILES = [
  { name: "MEMORY.md", color: "#3b82f6" },
  { name: "USER.md", color: "#8b5cf6" },
];

export default function S07MemorySystem() {
  const locale = useLocale();
  const steps = locale === "en" ? STEPS_EN : STEPS_ZH;
  const viz = useSteppedVisualization({ totalSteps: steps.length });

  const diskUpdated = viz.currentStep >= 3;
  const promptRefreshed = viz.currentStep >= 5;

  return (
    <div className="space-y-4 rounded-lg border border-zinc-200 p-4 dark:border-zinc-700">
      <h3 className="text-sm font-semibold">
        {locale === "zh" ? "记忆系统：快照 vs 磁盘" : "Memory System: Snapshot vs Disk"}
      </h3>
      <div className="grid grid-cols-2 gap-4">
        {/* System Prompt snapshot */}
        <div className="space-y-2 rounded-lg border border-zinc-200 p-3 dark:border-zinc-700">
          <p className="text-[10px] font-bold uppercase tracking-wider text-zinc-400">
            {locale === "zh" ? "Prompt 快照" : "Prompt Snapshot"}
          </p>
          {FILES.map((f) => (
            <motion.div
              key={f.name}
              animate={{
                opacity: viz.currentStep >= 0 ? 1 : 0.3,
                borderColor: promptRefreshed ? "#10b981" : f.color,
              }}
              className="rounded-md border-l-[3px] px-3 py-2"
            >
              <span className="text-xs font-mono" style={{ color: f.color }}>{f.name}</span>
              <p className="text-[10px] text-zinc-500">
                {promptRefreshed
                  ? (locale === "zh" ? "已刷新" : "refreshed")
                  : (locale === "zh" ? "冻结中" : "frozen")}
              </p>
            </motion.div>
          ))}
        </div>
        {/* Disk state */}
        <div className="space-y-2 rounded-lg border border-zinc-200 p-3 dark:border-zinc-700">
          <p className="text-[10px] font-bold uppercase tracking-wider text-zinc-400">
            {locale === "zh" ? "磁盘文件" : "Disk Files"}
          </p>
          {FILES.map((f) => (
            <motion.div
              key={f.name}
              animate={{
                opacity: 1,
                borderColor: diskUpdated && f.name === "USER.md" ? "#10b981" : f.color,
              }}
              className="rounded-md border-l-[3px] px-3 py-2"
            >
              <span className="text-xs font-mono" style={{ color: f.color }}>{f.name}</span>
              <p className="text-[10px] text-zinc-500">
                {diskUpdated && f.name === "USER.md"
                  ? (locale === "zh" ? "✓ 已写入新内容" : "✓ new content written")
                  : (locale === "zh" ? "原始内容" : "original")}
              </p>
            </motion.div>
          ))}
        </div>
      </div>
      {viz.currentStep >= 2 && viz.currentStep <= 4 && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="rounded-lg bg-amber-50 p-2 text-center dark:bg-amber-900/20"
        >
          <p className="text-[10px] text-amber-700 dark:text-amber-400">
            {locale === "zh"
              ? "磁盘已写入，但 prompt 快照要到下次会话才刷新"
              : "Disk written, but prompt snapshot won't refresh until next session"}
          </p>
        </motion.div>
      )}
      <StepControls
        currentStep={viz.currentStep} totalSteps={viz.totalSteps}
        isPlaying={viz.isPlaying} onReset={viz.reset} onPrev={viz.prev}
        onNext={viz.next} onTogglePlay={viz.toggleAutoPlay} annotations={steps}
      />
    </div>
  );
}
