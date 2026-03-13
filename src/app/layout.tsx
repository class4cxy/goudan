import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Home Agent · Aria",
  description: "智能家居管家",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" className="dark">
      <body className="antialiased">{children}</body>
    </html>
  );
}
