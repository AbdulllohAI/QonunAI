"use client";

import { Link2, Search } from "lucide-react";
import { useState } from "react";
import { api } from "@/lib/api";
import { t } from "@/lib/i18n";
import type { Language, SearchResponse } from "@/lib/types";
import { ACT_TYPE_LABELS, ACT_TYPE_STYLES, cn, formatMs } from "@/lib/utils";

const ACT_FILTERS = [
  { value: "", label: "All" },
  { value: "constitution", label: "Constitution" },
  { value: "code", label: "Codes" },
  { value: "law", label: "Laws" },
  { value: "presidential_decree", label: "Decrees" },
  { value: "cabinet_resolution", label: "Resolutions" },
];

export function LawSearch({ lang }: { lang: Language }) {
  const [query, setQuery] = useState("");
  const [actType, setActType] = useState("");
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    if (!query.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      setResult(
        await api.search({
          query: query.trim(),
          language: lang,
          top_k: 20,
          act_types: actType ? [actType] : null,
        }),
      );
    } catch (e) {
      setError((e as Error).message);
      setResult(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-4">
      <div className="flex flex-wrap gap-2">
        <div className="flex min-w-[260px] flex-1 gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && run()}
            placeholder={t(lang, "askPlaceholder")}
            className="input"
            aria-label={t(lang, "search")}
          />
          <button onClick={run} disabled={busy || !query.trim()} className="btn-primary">
            <Search size={14} aria-hidden />
            {busy ? t(lang, "searching") : t(lang, "search")}
          </button>
        </div>
        <select
          value={actType}
          onChange={(e) => setActType(e.target.value)}
          className="input w-auto"
          aria-label="Act type filter"
        >
          {ACT_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </select>
      </div>

      {error && (
        <div className="card border-red-300 bg-red-50 p-3 text-sm text-red-800 dark:border-red-800 dark:bg-red-950 dark:text-red-200">
          {error}
        </div>
      )}

      {result && (
        <>
          <p className="text-xs opacity-55">
            {result.hits.length} results · dense {result.dense_hits} · keyword{" "}
            {result.sparse_hits} · cross-ref {result.crossref_hits} ·{" "}
            {formatMs(result.took_ms)} · detected {result.detected_language}
          </p>

          {result.hits.length === 0 ? (
            <p className="py-8 text-center text-sm opacity-50">{t(lang, "noResults")}</p>
          ) : (
            <div className="space-y-3">
              {result.hits.map((hit) => (
                <article key={hit.chunk_id} className="card p-4">
                  <div className="mb-1.5 flex flex-wrap items-center gap-2">
                    <span
                      className={cn(
                        "badge",
                        ACT_TYPE_STYLES[hit.act_type] ?? "bg-zinc-100",
                      )}
                    >
                      {ACT_TYPE_LABELS[hit.act_type] ?? hit.act_type}
                    </span>
                    <h3 className="text-sm font-semibold">{hit.citation}</h3>
                    {hit.via_crossref_from && (
                      <span className="badge bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">
                        <Link2 size={10} aria-hidden /> via {hit.via_crossref_from}
                      </span>
                    )}
                  </div>

                  {hit.heading && <p className="mb-1 text-sm opacity-85">{hit.heading}</p>}

                  <p className="legal-prose whitespace-pre-wrap text-sm opacity-80">
                    {hit.text.length > 700 ? `${hit.text.slice(0, 700)}…` : hit.text}
                  </p>

                  <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] opacity-50">
                    <span>score {hit.score.toFixed(3)}</span>
                    <span>dense {hit.dense_score.toFixed(3)}</span>
                    <span>keyword {hit.sparse_score.toFixed(3)}</span>
                    {hit.rerank_score !== null && (
                      <span>rerank {hit.rerank_score.toFixed(3)}</span>
                    )}
                    <span>{hit.language}</span>
                    {hit.source_url && (
                      <a
                        href={hit.source_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-blue-700 hover:underline dark:text-blue-400"
                      >
                        source
                      </a>
                    )}
                  </div>
                </article>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
