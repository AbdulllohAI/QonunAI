import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "HuquqAI — Uzbekistan legal research",
  description:
    "AI legal research for the Republic of Uzbekistan. Citation-grounded answers in Uzbek, Russian and English.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="uz" suppressHydrationWarning>
      <body className="min-h-screen">{children}</body>
    </html>
  );
}
