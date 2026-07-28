"use client";

import { Send, Square } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { MessageBubble } from "./MessageBubble";
import { streamChat } from "@/lib/api";
import { EXAMPLE_QUESTIONS, t } from "@/lib/i18n";
import type { ChatMessage, Language, Mode } from "@/lib/types";
import { cn, uid } from "@/lib/utils";

const MODES: { value: Mode; key: "modeQa" | "modeSearch" | "modeArticle" }[] = [
  { value: "qa", key: "modeQa" },
  { value: "law_search", key: "modeSearch" },
  { value: "by_article", key: "modeArticle" },
];

export function ChatPanel({ lang }: { lang: Language }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [mode, setMode] = useState<Mode>("qa");
  const [busy, setBusy] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const send = useCallback(
    async (text: string) => {
      const question = text.trim();
      if (!question || busy) return;

      const userMsg: ChatMessage = { id: uid(), role: "user", content: question };
      const assistantId = uid();
      setMessages((prev) => [
        ...prev,
        userMsg,
        { id: assistantId, role: "assistant", content: "", streaming: true },
      ]);
      setInput("");
      setBusy(true);

      const controller = new AbortController();
      abortRef.current = controller;

      const patch = (updater: (m: ChatMessage) => ChatMessage) =>
        setMessages((prev) => prev.map((m) => (m.id === assistantId ? updater(m) : m)));

      try {
        await streamChat(
          { message: question, conversation_id: conversationId, mode, language: lang },
          {
            onMeta: (meta) => setConversationId(meta.conversation_id),
            onDelta: (delta) =>
              patch((m) => ({ ...m, content: m.content + delta })),
            onDone: (result) =>
              // Replace the streamed text with the validated answer: the
              // server strips citations that referenced non-retrieved sources.
              patch((m) => ({
                ...m,
                content: result.answer,
                citations: result.citations,
                risk: result.risk,
                hierarchy: result.hierarchy,
                validation: result.validation,
                warning: result.warning,
                disclaimer: result.disclaimer,
                timings: result.timings,
                streaming: false,
              })),
            onError: (error) =>
              patch((m) => ({ ...m, streaming: false, error })),
          },
          controller.signal,
        );
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          patch((m) => ({ ...m, streaming: false, error: (err as Error).message }));
        }
      } finally {
        patch((m) => ({ ...m, streaming: false }));
        setBusy(false);
        abortRef.current = null;
      }
    },
    [busy, conversationId, lang, mode],
  );

  const stop = () => {
    abortRef.current?.abort();
    setBusy(false);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send(input);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-[rgb(var(--border))] px-4 py-2">
        {MODES.map((m) => (
          <button
            key={m.value}
            type="button"
            onClick={() => setMode(m.value)}
            aria-pressed={mode === m.value}
            className={cn(
              "rounded-full px-3 py-1 text-xs transition-colors",
              mode === m.value
                ? "bg-blue-700 text-white"
                : "border border-[rgb(var(--border))] hover:bg-black/5 dark:hover:bg-white/5",
            )}
          >
            {t(lang, m.key)}
          </button>
        ))}
        {messages.length > 0 && (
          <button
            type="button"
            onClick={() => {
              setMessages([]);
              setConversationId(null);
            }}
            className="ml-auto text-xs opacity-60 hover:opacity-100"
          >
            {t(lang, "newChat")}
          </button>
        )}
      </div>

      <div className="flex-1 space-y-5 overflow-y-auto p-4">
        {messages.length === 0 ? (
          <div className="mx-auto max-w-lg pt-10 text-center">
            <p className="text-sm opacity-60">{t(lang, "empty")}</p>
            <p className="mt-6 mb-2 text-xs font-semibold uppercase tracking-wide opacity-40">
              {t(lang, "examples")}
            </p>
            <div className="space-y-2">
              {EXAMPLE_QUESTIONS[lang].map((q) => (
                <button
                  key={q}
                  type="button"
                  onClick={() => void send(q)}
                  className="card w-full p-2.5 text-left text-sm transition-colors hover:bg-black/5 dark:hover:bg-white/5"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((m) => <MessageBubble key={m.id} message={m} lang={lang} />)
        )}
        <div ref={endRef} />
      </div>

      <div className="border-t border-[rgb(var(--border))] p-3">
        <div className="flex items-end gap-2">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={t(lang, "askPlaceholder")}
            rows={2}
            disabled={busy}
            className="input resize-none"
            aria-label={t(lang, "askPlaceholder")}
          />
          {busy ? (
            <button type="button" onClick={stop} className="btn-ghost h-[42px]">
              <Square size={14} aria-hidden /> {t(lang, "stop")}
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void send(input)}
              disabled={!input.trim()}
              className="btn-primary h-[42px]"
            >
              <Send size={14} aria-hidden /> {t(lang, "send")}
            </button>
          )}
        </div>
        <p className="mt-1.5 text-[11px] opacity-45">{t(lang, "disclaimer")}</p>
      </div>
    </div>
  );
}
