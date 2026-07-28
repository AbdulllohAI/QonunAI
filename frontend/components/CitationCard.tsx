"use client";

import { ExternalLink, Info, Link2 } from "lucide-react";
import type { Citation } from "@/lib/types";
import { ACT_TYPE_LABELS, ACT_TYPE_STYLES, cn, isBinding } from "@/lib/utils";

export function CitationCard({ citation, index }: { citation: Citation; index: number }) {
  const binding = isBinding(citation.act_type);

  return (
    <div
      className={cn(
        "card p-3 text-sm transition-shadow hover:shadow-sm",
        citation.supporting && "border-dashed opacity-90",
      )}
    >
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
        <span
          className="flex h-5 w-5 items-center justify-center rounded-full bg-blue-700 text-[10px] font-semibold text-white"
          title={citation.tag}
        >
          {index}
        </span>
        <span className={cn("badge", ACT_TYPE_STYLES[citation.act_type] ?? "bg-zinc-100")}>
          {ACT_TYPE_LABELS[citation.act_type] ?? citation.act_type}
        </span>
        {!binding && (
          <span
            className="badge bg-orange-100 text-orange-900 dark:bg-orange-950 dark:text-orange-200"
            title="Not a binding source of law in Uzbekistan"
          >
            <Info size={11} aria-hidden /> non-binding
          </span>
        )}
        {citation.supporting && (
          <span className="badge bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">
            <Link2 size={11} aria-hidden /> cross-referenced
          </span>
        )}
      </div>

      <p className="font-medium leading-snug">{citation.citation}</p>

      {citation.hierarchy_path && (
        <p className="mt-1 font-mono text-[11px] opacity-55">{citation.hierarchy_path}</p>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] opacity-65">
        <span>{citation.language}</span>
        {citation.date_of_adoption && <span>adopted {citation.date_of_adoption}</span>}
        {citation.last_updated && <span>updated {citation.last_updated}</span>}
        <span>score {citation.score.toFixed(2)}</span>
      </div>

      {citation.source_url && (
        <a
          href={citation.source_url}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-2 inline-flex items-center gap-1 text-[11px] text-blue-700 hover:underline dark:text-blue-400"
        >
          <ExternalLink size={11} aria-hidden /> Verify on the official source
        </a>
      )}
    </div>
  );
}

export function CitationList({
  citations,
  title,
}: {
  citations: Citation[];
  title: string;
}) {
  if (!citations.length) return null;
  // Index by position in the full list, before splitting into primary/
  // supporting — must match renumberCitations' ordering exactly, since that
  // function numbers the inline superscripts from this same array/order.
  const indexByTag = new Map(citations.map((c, i) => [c.tag, i + 1]));
  const primary = citations.filter((c) => !c.supporting);
  const supporting = citations.filter((c) => c.supporting);

  return (
    <div className="space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wide opacity-60">
        {title} ({citations.length})
      </h4>
      <div className="space-y-2">
        {primary.map((c) => (
          <CitationCard key={c.chunk_id + c.tag} citation={c} index={indexByTag.get(c.tag)!} />
        ))}
      </div>
      {supporting.length > 0 && (
        <>
          <h4 className="pt-2 text-xs font-semibold uppercase tracking-wide opacity-60">
            Cross-referenced ({supporting.length})
          </h4>
          <div className="space-y-2">
            {supporting.map((c) => (
              <CitationCard key={c.chunk_id + c.tag} citation={c} index={indexByTag.get(c.tag)!} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
