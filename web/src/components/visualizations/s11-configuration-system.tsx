"use client";

import { motion } from "framer-motion";
import { useSteppedVisualization } from "@/hooks/useSteppedVisualization";
import { StepControls } from "@/components/visualizations/shared/step-controls";
import { useLocale } from "@/lib/i18n";

const STEPS_ZH = [
  { title: "启动流程", description: "Agent 启动，按顺序执行三步配置加载。" },
  { title: "load_env()", description: "读取 .env 文件，用 os.environ.setdefault 注入环境变量（不覆盖已有值）。" },
  { title: "load_config()", description: "读取 config.yaml，与 DEFAULT_CONFIG 做 deep merge。用户只需写 override。" },
  { title: "_expand_env_vars()", description: "扫描 YAML 值中的 ${VAR} 占位符，替换为实际环境变量值。" },
  { title: "配置就绪", description: "最终 config 字典生成。所有模块从中读取参数。" },
  { title: "切换 Profile", description: "改变 HERMES_HOME 就切换到完全不同的 profile（模型、记忆、技能、白名单）。" },
];

const STEPS_EN = [
  { title: "Boot Sequence", description: "Agent starts. Three-step config loading in order." },
  { title: "load_env()", description: "Read .env file, inject via os.environ.setdefault (won't overwrite existing)." },
  { title: "load_config()", description: "Read config.yaml, deep merge with DEFAULT_CONFIG. User only writes overrides." },
  { title: "_expand_env_vars()", description: "Scan YAML values for ${VAR} placeholders, replace with actual env values." },
  { title: "Config Ready", description: "Final config dict produced. All modules read from it." },
  { title: "Switch Profile", description: "Change HERMES_HOME to switch profile (model, memory, skills, allowlist)." },
];

const PIPELINE = [
  { label: ".env", sub: "secrets", color: "#f59e0b" },
  { label: "config.yaml", sub: "overrides", color: "#3b82f6" },
  { label: "DEFAULT_CONFIG", sub: "defaults", color: "#6b7280" },
  { label: "${VAR} expand", sub: "interpolate", color: "#8b5cf6" },
  { label: "Final Config", sub: "ready", color: "#10b981" },
];

export default function S11ConfigurationSystem() {
  const locale = useLocale();
  const steps = locale === "en" ? STEPS_EN : STEPS_ZH;
  const viz = useSteppedVisualization({ totalSteps: steps.length });

  const activeIndex = Math.min(viz.currentStep, PIPELINE.length - 1);

  return (
    <div className="space-y-4 rounded-lg border border-zinc-200 p-4 dark:border-zinc-700">
      <h3 className="text-sm font-semibold">
        {locale === "zh" ? "配置加载流水线" : "Config Loading Pipeline"}
      </h3>
      {/* Pipeline steps */}
      <div className="flex items-center justify-between gap-1">
        {PIPELINE.map((item, i) => (
          <div key={item.label} className="flex items-center gap-1">
            <motion.div
              animate={{
                scale: activeIndex === i ? 1.08 : 1,
                opacity: i <= activeIndex ? 1 : 0.3,
              }}
              className="rounded-lg border px-3 py-2 text-center"
              style={{
                borderColor: i <= activeIndex ? item.color : undefined,
                minWidth: 80,
              }}
            >
              <p className="text-[10px] font-mono font-medium" style={{ color: item.color }}>
                {item.label}
              </p>
              <p className="text-[10px] text-zinc-500">{item.sub}</p>
            </motion.div>
            {i < PIPELINE.length - 1 && (
              <motion.span
                animate={{ opacity: i < activeIndex ? 1 : 0.2 }}
                className="text-zinc-400 text-xs"
              >
                &rarr;
              </motion.span>
            )}
          </div>
        ))}
      </div>
      {/* Profile switch note */}
      {viz.currentStep >= 5 && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="rounded-lg bg-violet-50 p-2 dark:bg-violet-900/20"
        >
          <p className="text-[10px] text-center text-violet-700 dark:text-violet-400">
            HERMES_HOME=~/.hermes-work &rarr; {locale === "zh" ? "完全不同的配置、记忆、技能" : "completely different config, memory, skills"}
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
