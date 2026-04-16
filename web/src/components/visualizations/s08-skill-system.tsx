"use client";

import { motion } from "framer-motion";
import { useSteppedVisualization } from "@/hooks/useSteppedVisualization";
import { StepControls } from "@/components/visualizations/shared/step-controls";
import { useLocale } from "@/lib/i18n";

const STEPS_ZH = [
  { title: "构建 Prompt", description: "系统扫描 skills/ 目录，把每个 SKILL.md 的 name + description 注入 prompt。" },
  { title: "用户请求", description: "用户说 '帮我写个 MCP 服务器'。模型在 prompt 里看到 mcp-builder 技能。" },
  { title: "skill_view", description: "模型调用 skill_view('mcp-builder')，按需加载完整 SKILL.md body。" },
  { title: "全文注入", description: "完整技能内容作为 tool result 返回，模型获得详细指导。" },
  { title: "执行技能", description: "模型按照技能指导，调用 terminal/file 等工具完成任务。" },
];

const STEPS_EN = [
  { title: "Build Prompt", description: "Scan skills/ dir, inject name + description of each SKILL.md into prompt." },
  { title: "User Request", description: "User: 'build an MCP server'. Model sees mcp-builder skill in prompt." },
  { title: "skill_view", description: "Model calls skill_view('mcp-builder') to load full SKILL.md body on demand." },
  { title: "Full Body Injected", description: "Complete skill content returned as tool result. Model gets detailed guidance." },
  { title: "Execute Skill", description: "Model follows skill guidance, calling terminal/file tools to complete task." },
];

const SKILLS = [
  { name: "agent-builder", desc: "Build custom agents" },
  { name: "mcp-builder", desc: "Create MCP servers" },
  { name: "code-review", desc: "Review pull requests" },
];

export default function S08SkillSystem() {
  const locale = useLocale();
  const steps = locale === "en" ? STEPS_EN : STEPS_ZH;
  const viz = useSteppedVisualization({ totalSteps: steps.length });

  const activeSkill = viz.currentStep >= 1 ? 1 : -1; // mcp-builder
  const bodyLoaded = viz.currentStep >= 3;

  return (
    <div className="space-y-4 rounded-lg border border-zinc-200 p-4 dark:border-zinc-700">
      <h3 className="text-sm font-semibold">
        {locale === "zh" ? "技能系统：两步加载" : "Skill System: Two-Step Loading"}
      </h3>
      <div className="flex gap-4">
        {/* Skill registry */}
        <div className="flex-1 space-y-1.5">
          <p className="text-[10px] font-bold uppercase tracking-wider text-zinc-400">
            {locale === "zh" ? "Prompt 中的技能列表" : "Skills in Prompt"}
          </p>
          {SKILLS.map((s, i) => (
            <motion.div
              key={s.name}
              animate={{
                scale: activeSkill === i ? 1.03 : 1,
                borderColor: activeSkill === i ? "#10b981" : "#e4e4e7",
              }}
              className="rounded-md border px-3 py-2"
            >
              <span className="text-xs font-mono font-medium">{s.name}</span>
              <p className="text-[10px] text-zinc-500">{s.desc}</p>
            </motion.div>
          ))}
        </div>
        {/* Detail panel */}
        <div className="w-48 shrink-0">
          <p className="text-[10px] font-bold uppercase tracking-wider text-zinc-400">
            {locale === "zh" ? "技能详情" : "Skill Detail"}
          </p>
          <motion.div
            animate={{ opacity: bodyLoaded ? 1 : 0.3 }}
            className="mt-1.5 rounded-lg border border-dashed border-zinc-300 p-3 dark:border-zinc-600"
          >
            {bodyLoaded ? (
              <>
                <p className="text-xs font-mono font-medium text-emerald-600">mcp-builder</p>
                <p className="mt-1 text-[10px] text-zinc-500">
                  {locale === "zh" ? "完整 SKILL.md body 已加载" : "Full SKILL.md body loaded"}
                </p>
                <div className="mt-2 space-y-0.5">
                  {["## Steps", "## Templates", "## Examples"].map((h) => (
                    <p key={h} className="text-[10px] font-mono text-zinc-400">{h}</p>
                  ))}
                </div>
              </>
            ) : (
              <p className="text-[10px] text-zinc-400 italic">
                {locale === "zh" ? "等待 skill_view 调用…" : "Awaiting skill_view call…"}
              </p>
            )}
          </motion.div>
        </div>
      </div>
      <StepControls
        currentStep={viz.currentStep} totalSteps={viz.totalSteps}
        isPlaying={viz.isPlaying} onReset={viz.reset} onPrev={viz.prev}
        onNext={viz.next} onTogglePlay={viz.toggleAutoPlay} annotations={steps}
      />
    </div>
  );
}
