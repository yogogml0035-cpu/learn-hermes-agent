"use client";

import { motion } from "framer-motion";
import { useSteppedVisualization } from "@/hooks/useSteppedVisualization";
import { StepControls } from "@/components/visualizations/shared/step-controls";
import { useLocale } from "@/lib/i18n";

const STEPS_ZH = [
  { title: "模型请求执行", description: "模型调用 terminal('rm -rf /tmp/data')。进入权限检查。" },
  { title: "正则匹配", description: "~15 条危险模式逐一匹配。命中 'rm\\s+.*-r' 规则。" },
  { title: "检查缓存", description: "先查 session 缓存和 allowlist.json，都没有这条命令。" },
  { title: "提示用户", description: "弹出四选一：once / session / always / deny。" },
  { title: "用户选 session", description: "用户选了 session。该模式写入内存缓存，本轮免问。" },
  { title: "执行命令", description: "权限通过，执行 rm -rf /tmp/data，返回结果。" },
  { title: "再次触发", description: "模型又调 rm -rf /tmp/logs。同一 session，缓存命中，直接放行。" },
];

const STEPS_EN = [
  { title: "Model Requests Exec", description: "Model calls terminal('rm -rf /tmp/data'). Permission check begins." },
  { title: "Regex Match", description: "~15 danger patterns checked. Matches 'rm\\s+.*-r' rule." },
  { title: "Check Caches", description: "Check session cache & allowlist.json. No match found." },
  { title: "Prompt User", description: "Show four choices: once / session / always / deny." },
  { title: "User Picks Session", description: "User chose 'session'. Pattern cached in memory for this session." },
  { title: "Command Executed", description: "Permission granted. Execute rm -rf /tmp/data, return result." },
  { title: "Triggered Again", description: "Model calls rm -rf /tmp/logs. Same session, cache hit, auto-allowed." },
];

const LAYERS = [
  { label: "Regex Match", color: "#ef4444" },
  { label: "Cache Lookup", color: "#f59e0b" },
  { label: "User Prompt", color: "#3b82f6" },
];

export default function S09PermissionSystem() {
  const locale = useLocale();
  const steps = locale === "en" ? STEPS_EN : STEPS_ZH;
  const viz = useSteppedVisualization({ totalSteps: steps.length });

  const activeLayer = viz.currentStep === 1 ? 0 : viz.currentStep === 2 ? 1 : viz.currentStep >= 3 && viz.currentStep <= 4 ? 2 : -1;
  const cacheHit = viz.currentStep >= 6;

  return (
    <div className="space-y-4 rounded-lg border border-zinc-200 p-4 dark:border-zinc-700">
      <h3 className="text-sm font-semibold">
        {locale === "zh" ? "权限系统：三层检查" : "Permission System: Three-Layer Check"}
      </h3>
      {/* Three layers */}
      <div className="flex items-center justify-center gap-2">
        {LAYERS.map((layer, i) => (
          <div key={layer.label} className="flex items-center gap-2">
            <motion.div
              animate={{
                scale: activeLayer === i ? 1.08 : 1,
                opacity: activeLayer === i ? 1 : 0.4,
              }}
              className="rounded-lg border px-4 py-3 text-center"
              style={{ borderColor: activeLayer === i ? layer.color : undefined }}
            >
              <p className="text-xs font-medium" style={{ color: layer.color }}>{layer.label}</p>
              <p className="text-[10px] text-zinc-500">Layer {i + 1}</p>
            </motion.div>
            {i < LAYERS.length - 1 && (
              <span className="text-zinc-400">&rarr;</span>
            )}
          </div>
        ))}
      </div>
      {/* User choice display */}
      {viz.currentStep >= 3 && viz.currentStep <= 4 && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex justify-center gap-2"
        >
          {["once", "session", "always", "deny"].map((choice) => (
            <span
              key={choice}
              className={`rounded-full border px-3 py-1 text-[10px] font-mono ${
                viz.currentStep === 4 && choice === "session"
                  ? "border-blue-500 bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400"
                  : "border-zinc-200 text-zinc-400 dark:border-zinc-700"
              }`}
            >
              {choice}
            </span>
          ))}
        </motion.div>
      )}
      {/* Cache hit indicator */}
      {cacheHit && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="rounded-lg bg-emerald-50 p-2 text-center dark:bg-emerald-900/20"
        >
          <p className="text-[10px] text-emerald-700 dark:text-emerald-400">
            {locale === "zh" ? "Session 缓存命中 → 直接放行" : "Session cache hit → auto-allowed"}
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
