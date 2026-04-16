"use client";

import { motion } from "framer-motion";
import { useSteppedVisualization } from "@/hooks/useSteppedVisualization";
import { StepControls } from "@/components/visualizations/shared/step-controls";
import { useLocale } from "@/lib/i18n";

const STEPS_ZH = [
  { title: "父 Agent 运行中", description: "父 Agent 收到复杂任务，决定拆分给子 Agent。" },
  { title: "调用 delegate_task", description: "delegate_task(prompt='分析这个日志文件')。创建子 Agent。" },
  { title: "子 Agent 启动", description: "子 Agent 拥有独立的 messages 列表，更小的迭代预算。" },
  { title: "子 Agent 工作", description: "子 Agent 调用 terminal/file 工具，执行分析。中间过程对父不可见。" },
  { title: "子 Agent 完成", description: "子 Agent 循环结束，产出最终文本结果。" },
  { title: "结果回传", description: "只有最终文本返回给父 Agent 作为 tool result。中间噪音被隔离。" },
  { title: "父 Agent 继续", description: "父 Agent 拿到精炼结果，继续处理其他任务。" },
];

const STEPS_EN = [
  { title: "Parent Running", description: "Parent agent gets a complex task, decides to delegate." },
  { title: "Call delegate_task", description: "delegate_task(prompt='analyze this log file'). Child agent created." },
  { title: "Child Starts", description: "Child has isolated messages list and smaller iteration budget." },
  { title: "Child Working", description: "Child calls terminal/file tools. Intermediate steps invisible to parent." },
  { title: "Child Completes", description: "Child loop ends, produces final text result." },
  { title: "Result Returned", description: "Only final text goes back to parent as tool result. Noise isolated." },
  { title: "Parent Continues", description: "Parent gets refined result, continues with remaining work." },
];

export default function S10SubagentDelegation() {
  const locale = useLocale();
  const steps = locale === "en" ? STEPS_EN : STEPS_ZH;
  const viz = useSteppedVisualization({ totalSteps: steps.length });

  const childActive = viz.currentStep >= 2 && viz.currentStep <= 4;
  const childDone = viz.currentStep >= 5;

  const parentMsgs = [
    { role: "user", color: "#3b82f6" },
    ...(viz.currentStep >= 1 ? [{ role: "assistant (delegate)", color: "#10b981" }] : []),
    ...(childDone ? [{ role: "tool (child result)", color: "#f59e0b" }] : []),
    ...(viz.currentStep >= 6 ? [{ role: "assistant", color: "#8b5cf6" }] : []),
  ];

  const childMsgs = childActive
    ? [
        { role: "user (prompt)", color: "#3b82f6" },
        { role: "assistant+tool_calls", color: "#10b981" },
        { role: "tool (output)", color: "#f59e0b" },
        ...(viz.currentStep >= 4 ? [{ role: "assistant (final)", color: "#8b5cf6" }] : []),
      ]
    : [];

  return (
    <div className="space-y-4 rounded-lg border border-zinc-200 p-4 dark:border-zinc-700">
      <h3 className="text-sm font-semibold">
        {locale === "zh" ? "子 Agent 委托：隔离与回传" : "Subagent Delegation: Isolation & Return"}
      </h3>
      <div className="grid grid-cols-2 gap-4">
        {/* Parent */}
        <div className="space-y-1.5 rounded-lg border border-zinc-200 p-3 dark:border-zinc-700">
          <p className="text-[10px] font-bold uppercase tracking-wider text-zinc-400">
            {locale === "zh" ? "父 Agent messages[]" : "Parent messages[]"}
          </p>
          {parentMsgs.map((m, i) => (
            <motion.div
              key={i}
              initial={{ opacity: 0, x: -10 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: i * 0.05 }}
              className="rounded-md px-2 py-1.5"
              style={{ backgroundColor: m.color + "15", borderLeft: `3px solid ${m.color}` }}
            >
              <span className="text-[10px] font-mono" style={{ color: m.color }}>{m.role}</span>
            </motion.div>
          ))}
        </div>
        {/* Child */}
        <div className="space-y-1.5 rounded-lg border border-dashed border-zinc-300 p-3 dark:border-zinc-600">
          <p className="text-[10px] font-bold uppercase tracking-wider text-zinc-400">
            {locale === "zh" ? "子 Agent messages[]" : "Child messages[]"}
          </p>
          {childMsgs.length > 0 ? (
            childMsgs.map((m, i) => (
              <motion.div
                key={i}
                initial={{ opacity: 0, x: 10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: i * 0.05 }}
                className="rounded-md px-2 py-1.5"
                style={{ backgroundColor: m.color + "15", borderLeft: `3px solid ${m.color}` }}
              >
                <span className="text-[10px] font-mono" style={{ color: m.color }}>{m.role}</span>
              </motion.div>
            ))
          ) : (
            <p className="text-[10px] text-zinc-400 italic">
              {childDone
                ? (locale === "zh" ? "已完成，结果已回传" : "Done, result returned")
                : (locale === "zh" ? "未启动" : "Not started")}
            </p>
          )}
        </div>
      </div>
      {childDone && viz.currentStep < 6 && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="rounded-lg bg-emerald-50 p-2 text-center dark:bg-emerald-900/20"
        >
          <p className="text-[10px] text-emerald-700 dark:text-emerald-400">
            {locale === "zh"
              ? "只有最终文本回传，中间 tool 调用全部隔离"
              : "Only final text returned. All intermediate tool calls isolated."}
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
