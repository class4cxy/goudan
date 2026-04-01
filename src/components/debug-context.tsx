"use client";

import { createContext, useContext, useEffect, useState } from "react";

const DebugContext = createContext(false);

export function DebugProvider({ children }: { children: React.ReactNode }) {
  const [debug, setDebug] = useState(false);

  useEffect(() => {
    setDebug(new URLSearchParams(window.location.search).has("debug"));
  }, []);

  return <DebugContext.Provider value={debug}>{children}</DebugContext.Provider>;
}

export function useDebugMode() {
  return useContext(DebugContext);
}
