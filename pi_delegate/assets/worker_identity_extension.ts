import fs from "node:fs";

export default function workerIdentityExtension(pi: any) {
  const outputPath = process.env.PI_DELEGATE_WORKER_IDENTITY_PATH;
  if (!outputPath) return;

  const persist = (phase: string, ctx: any) => {
    try {
      const model = ctx?.model;
      const payload = {
        source: "pi_extension_ctx",
        phase,
        pid: process.pid,
        provider: model?.provider ?? null,
        model: model?.id ?? null,
        thinking: ctx?.thinkingLevel ?? pi.getThinkingLevel?.() ?? null,
        captured_at: new Date().toISOString(),
      };
      const temp = `${outputPath}.tmp`;
      fs.writeFileSync(temp, JSON.stringify(payload, null, 2), "utf8");
      fs.renameSync(temp, outputPath);
    } catch {
      // Runtime identity is diagnostic evidence only; never break the worker.
    }
  };

  pi.on("agent_start", (_event: any, ctx: any) => persist("agent_start", ctx));
  pi.on("agent_settled", (_event: any, ctx: any) => persist("agent_settled", ctx));
}
