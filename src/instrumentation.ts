export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    // ── 调度器 ──────────────────────────────────────────────────────
    const { loadScheduledTasks, setTaskExecutor } = await import("@/core/behavior/scheduler");
    const { executeScheduledTask } = await import("@/core/cognition/tools");
    setTaskExecutor(executeScheduledTask);
    loadScheduledTasks();
    console.log("[Instrumentation] 调度器已启动");

    // ── 交流能力模块（麦克风感知 + 对话状态机 + 主/被动发起）────────
    const { startConversationModule } = await import("@/core/behavior/conversation");
    startConversationModule();

    // ── 运动模块（导航意图 + 电机指令转发）──────────────────────────
    const { startMotorModule } = await import("@/core/behavior/motor");
    startMotorModule();
  }
}
