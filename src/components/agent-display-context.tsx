"use client";

import { createContext, useContext, type ReactNode } from "react";
import { DEFAULT_AGENT_DISPLAY_NAME } from "@/lib/agent-display";

const AgentDisplayNameContext = createContext(DEFAULT_AGENT_DISPLAY_NAME);

export function AgentDisplayNameProvider({
  value,
  children,
}: {
  value: string;
  children: ReactNode;
}) {
  return (
    <AgentDisplayNameContext.Provider value={value}>
      {children}
    </AgentDisplayNameContext.Provider>
  );
}

export function useAgentDisplayName(): string {
  return useContext(AgentDisplayNameContext);
}
