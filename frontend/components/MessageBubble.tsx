"use client";

import { AlertCircle, Bot, User } from "lucide-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { CitationList } from "./CitationCard";
import { HierarchyPanel } from "./HierarchyPanel";
import { RiskBadge } from "./RiskBadge";
import { t } from "@/lib/i18n";
import type { ChatMessage, Language } from "@/lib/types";
import { cn, formatMs, renumberCitations } from "@/lib/utils";

export function MessageBubble({ message, lang }: { message: ChatMessage; lang: Language }) {
  const isUser = message.role === "user";

  if (isUser) {
    return (
      <div className="flex justify-end gap-3">
        <div className="max-w-[85%] rounded-2xl rounded-tr-sm bg-blue-700 px-4 py-2.5 text-sm text-white">
          {message.content}
        </div>
        <div className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-blue-700/10">
          <User size={14} className="text-blue-700 dark:text-blue-400" aria-hidden />
        </div>
      </div>
    );
  }

  return (
    <div className="flex gap-3">
      <div className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-emerald-600/10">
        <Bot size={14} className="text-emerald-700 dark:text-emerald-400" aria-hidden />
      </div>

      <div className="min-w-0 flex-1 space-y-3">
        {message.error ? (
          <div className="card flex items-start gap-2 border-red-300 bg-red-50 p-3 text-sm text-red-800 dark:border-red-800 dark:bg-red-950 dark:text-red-200">
            <AlertCircle size={15} className="mt-0.5 shrink-0" aria-hidden />
            <span>{message.error}</span>
          </div>
        ) : (
          <>
            <div className="card legal-prose answer-md p-4 text-sm">
              <Markdown remarkPlugins={[remarkGfm]}>
                {renumberCitations(message.content, message.citations ?? [])}
              </Markdown>
              {message.streaming && (
                <span className="ml-0.5 inline-flex gap-0.5" aria-label="generating">
                  <span className="typing-dot">●</span>
                  <span className="typing-dot" style={{ animationDelay: "0.2s" }}>●</span>
                  <span className="typing-dot" style={{ animationDelay: "0.4s" }}>●</span>
                </span>
              )}
            </div>

            {message.warning && (
              <div className="card border-amber-300 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
                <strong>{t(lang, "warning")}: </strong>
                {message.warning}
              </div>
            )}

            {!message.streaming && (
              <div className="flex flex-wrap items-center gap-3">
                {message.risk && <RiskBadge risk={message.risk} lang={lang} />}
                {message.timings && (
                  <span className="text-[11px] opacity-50">
                    {t(lang, "took")} {formatMs(message.timings.total_ms)} (retrieval{" "}
                    {formatMs(message.timings.retrieval_ms)})
                  </span>
                )}
              </div>
            )}

            {message.hierarchy && <HierarchyPanel hierarchy={message.hierarchy} lang={lang} />}

            {message.citations && message.citations.length > 0 && (
              <CitationList citations={message.citations} title={t(lang, "sources")} />
            )}

            {message.disclaimer && !message.streaming && (
              <p className="border-t border-[rgb(var(--border))] pt-2 text-[11px] italic opacity-50">
                {message.disclaimer}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
