"use client";

import { AlertTriangle, FileText, Upload } from "lucide-react";
import { useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { CitationList } from "./CitationCard";
import { RiskBadge } from "./RiskBadge";
import { api } from "@/lib/api";
import { t } from "@/lib/i18n";
import type { DocumentAnalysisResult, Language } from "@/lib/types";

export function DocumentUpload({ lang }: { lang: Language }) {
  const [file, setFile] = useState<File | null>(null);
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<DocumentAnalysisResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const analyze = async () => {
    if (!file || busy) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await api.analyzeDocument(file, { question: question || undefined, language: lang }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-4">
      <label
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          const dropped = e.dataTransfer.files?.[0];
          if (dropped) setFile(dropped);
        }}
        className={`card flex cursor-pointer flex-col items-center gap-2 border-2 border-dashed p-8 text-center transition-colors ${
          dragging ? "border-blue-500 bg-blue-50 dark:bg-blue-950" : ""
        }`}
      >
        <Upload size={24} className="opacity-40" aria-hidden />
        <span className="text-sm opacity-70">{t(lang, "uploadPrompt")}</span>
        {file && (
          <span className="mt-1 inline-flex items-center gap-1.5 text-sm font-medium">
            <FileText size={14} aria-hidden />
            {file.name} ({(file.size / 1024).toFixed(0)} KB)
          </span>
        )}
        <input
          type="file"
          accept=".pdf,.docx,.doc,.txt,.html"
          className="hidden"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
      </label>

      <input
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        placeholder="Optional: a specific question about this document"
        className="input"
      />

      <button onClick={analyze} disabled={!file || busy} className="btn-primary w-full">
        {busy ? t(lang, "analyzing") : t(lang, "analyze")}
      </button>

      {error && (
        <div className="card border-red-300 bg-red-50 p-3 text-sm text-red-800 dark:border-red-800 dark:bg-red-950 dark:text-red-200">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <RiskBadge risk={result.risk} lang={lang} />
            <span className="text-xs opacity-55">
              {result.document.clause_count} clauses · {result.document.text_length} chars ·{" "}
              {result.document.detected_language}
              {result.document.truncated && " · truncated for analysis"}
            </span>
          </div>

          {result.document.heuristic_flags.length > 0 && (
            <div className="card border-amber-300 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-950">
              <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-amber-900 dark:text-amber-200">
                <AlertTriangle size={13} aria-hidden />
                Automated clause screening
              </h4>
              <ul className="space-y-2">
                {result.document.heuristic_flags.map((flag) => (
                  <li key={flag.code} className="text-xs text-amber-900 dark:text-amber-200">
                    <strong>{flag.code.replace(/_/g, " ")}</strong>: {flag.message}
                    <p className="mt-0.5 font-mono text-[10px] opacity-70">“{flag.excerpt}”</p>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="card legal-prose answer-md p-4 text-sm">
            <Markdown remarkPlugins={[remarkGfm]}>{result.answer}</Markdown>
          </div>

          {result.warning && (
            <div className="card border-amber-300 bg-amber-50 p-3 text-xs dark:border-amber-800 dark:bg-amber-950">
              <strong>{t(lang, "warning")}: </strong>
              {result.warning}
            </div>
          )}

          <CitationList citations={result.citations} title={t(lang, "sources")} />

          <p className="border-t border-[rgb(var(--border))] pt-2 text-[11px] italic opacity-50">
            {result.disclaimer}
          </p>
        </div>
      )}
    </div>
  );
}
