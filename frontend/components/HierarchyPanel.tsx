"use client";

import { Scale } from "lucide-react";
import { t } from "@/lib/i18n";
import type { HierarchyInfo, Language } from "@/lib/types";

/**
 * Renders the deterministic conflict analysis. This comes from the server's
 * hierarchy resolver, not from the model — so it stays correct even if the
 * model's prose gets the precedence order wrong.
 */
export function HierarchyPanel({
  hierarchy,
  lang,
}: {
  hierarchy: HierarchyInfo;
  lang: Language;
}) {
  if (!hierarchy?.controlling && !hierarchy?.conflicts?.length) return null;

  return (
    <div className="card p-3 text-sm">
      <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide opacity-60">
        <Scale size={13} aria-hidden />
        {t(lang, "conflicts")}
      </div>

      {hierarchy.controlling && (
        <p className="mb-2">
          <span className="opacity-60">{t(lang, "controlling")}: </span>
          <strong>{hierarchy.controlling}</strong>
        </p>
      )}

      {hierarchy.conflicts?.length > 0 && (
        <ul className="space-y-2">
          {hierarchy.conflicts.map((conflict, i) => (
            <li
              key={i}
              className="rounded-lg border border-[rgb(var(--border))] bg-black/[0.02] p-2 dark:bg-white/[0.03]"
            >
              <p className="text-xs">
                <strong>{conflict.higher}</strong>
                <span className="opacity-60"> prevails over </span>
                <strong>{conflict.lower}</strong>
              </p>
              <p className="mt-1 font-mono text-[10px] italic opacity-55">{conflict.rule}</p>
              <p className="mt-1 text-xs opacity-80">{conflict.explanation}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
