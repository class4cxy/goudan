import type { Metadata } from "next";
import "./globals.css";
import { AgentDisplayNameProvider } from "@/components/agent-display-context";
import { DebugProvider } from "@/components/debug-context";
import { agentDisplayName } from "@/lib/agent-display";

export async function generateMetadata(): Promise<Metadata> {
  const name = agentDisplayName();
  return {
    title: `Home Agent · ${name}`,
    description: `智能家居管家 · ${name}`,
  };
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const displayName = agentDisplayName();
  return (
    <html lang="zh-CN" className="dark" suppressHydrationWarning>
      <body className="antialiased">
        <DebugProvider>
          <AgentDisplayNameProvider value={displayName}>{children}</AgentDisplayNameProvider>
        </DebugProvider>
      </body>
    </html>
  );
}
