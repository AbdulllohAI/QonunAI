"use client";

import { FileText, MessageSquare, Scale, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { ChatPanel } from "./ChatPanel";
import { DocumentUpload } from "./DocumentUpload";
import { LanguageSwitcher } from "./LanguageSwitcher";
import { LawSearch } from "./LawSearch";
import { api } from "@/lib/api";
import { t } from "@/lib/i18n";
import type { Language } from "@/lib/types";
import { cn, detectBrowserLanguage } from "@/lib/utils";

type Tab = "chat" | "search" | "documents";

const TABS: { id: Tab; icon: typeof MessageSquare; key: "chat" | "search" | "documents" }[] = [
  { id: "chat", icon: MessageSquare, key: "chat" },
  { id: "search", icon: Search, key: "search" },
  { id: "documents", icon: FileText, key: "documents" },
];

export function AppShell({ initialTab = "chat" }: { initialTab?: Tab }) {
  const [lang, setLang] = useState<Language>("uz-Latn");
  const [tab, setTab] = useState<Tab>(initialTab);
  const [corpus, setCorpus] = useState<{ acts: number; chunks: number } | null>(null);

  useEffect(() => {
    const stored = window.localStorage.getItem("uzlex_lang") as Language | null;
    setLang(stored ?? detectBrowserLanguage());
  }, []);

  useEffect(() => {
    window.localStorage.setItem("uzlex_lang", lang);
  }, [lang]);

  useEffect(() => {
    // Surfacing corpus size is a trust signal: an empty index should be visible,
    // not something the user discovers through bad answers.
    api
      .health()
      .then((h) => setCorpus({ acts: h.corpus?.acts ?? 0, chunks: h.corpus?.chunks ?? 0 }))
      .catch(() => setCorpus(null));
  }, []);

  return (
    <div className="flex h-screen flex-col">
      <header className="flex flex-wrap items-center gap-3 border-b border-[rgb(var(--border))] px-4 py-2.5">
        <div className="flex items-center gap-2">
          <Scale size={20} className="text-blue-700 dark:text-blue-400" aria-hidden />
          <div>
            <h1 className="text-sm font-semibold leading-tight">{t(lang, "appName")}</h1>
            <p className="text-[11px] leading-tight opacity-55">{t(lang, "tagline")}</p>
          </div>
        </div>

        <nav className="ml-4 flex gap-1" aria-label="Main">
          {TABS.map(({ id, icon: Icon, key }) => (
            <button
              key={id}
              type="button"
              onClick={() => setTab(id)}
              aria-current={tab === id ? "page" : undefined}
              className={cn(
                "flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs transition-colors",
                tab === id
                  ? "bg-blue-700 text-white"
                  : "hover:bg-black/5 dark:hover:bg-white/5",
              )}
            >
              <Icon size={13} aria-hidden />
              {t(lang, key)}
            </button>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-3">
          {corpus && (
            <span
              className={cn(
                "text-[11px]",
                corpus.chunks === 0 ? "text-amber-600" : "opacity-45",
              )}
              title="Indexed corpus size"
            >
              {corpus.chunks === 0
                ? "corpus empty — run the bootstrap script"
                : `${corpus.acts} acts · ${corpus.chunks.toLocaleString()} chunks`}
            </span>
          )}
          <LanguageSwitcher value={lang} onChange={setLang} />
        </div>
      </header>

      <main className="min-h-0 flex-1 overflow-y-auto">
        {tab === "chat" && <ChatPanel lang={lang} />}
        {tab === "search" && <LawSearch lang={lang} />}
        {tab === "documents" && <DocumentUpload lang={lang} />}
      </main>
    </div>
  );
}
