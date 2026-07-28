"use client";

import { AlertTriangle, CheckCircle2, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { t } from "@/lib/i18n";
import type { Language, Risk } from "@/lib/types";
import { RISK_STYLES, cn } from "@/lib/utils";

const ICONS = {
  low: CheckCircle2,
  medium: AlertTriangle,
  high: ShieldAlert,
} as const;

export function RiskBadge({
  risk,
  lang,
  expandable = true,
}: {
  risk: Risk;
  lang: Language;
  expandable?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const Icon = ICONS[risk.level];
  const label = t(
    lang,
    risk.level === "low" ? "riskLow" : risk.level === "medium" ? "riskMedium" : "riskHigh",
  );

  return (
    <div className="inline-block">
      <button
        type="button"
        onClick={() => expandable && setOpen((v) => !v)}
        className={cn(
          "badge border px-2.5 py-1",
          RISK_STYLES[risk.level],
          expandable && "cursor-pointer hover:opacity-80",
        )}
        aria-expanded={open}
      >
        <Icon size={13} aria-hidden />
        <span>
          {t(lang, "risk")}: {label}
        </span>
        {expandable && risk.factors.length > 0 && (
          <span className="opacity-60">({risk.factors.length})</span>
        )}
      </button>

      {open && risk.factors.length > 0 && (
        <ul className="mt-2 space-y-1 rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-3 text-xs opacity-90">
          {risk.factors.map((factor, i) => (
            <li key={i} className="flex gap-2">
              <span className="opacity-50">•</span>
              <span>{factor}</span>
            </li>
          ))}
          {risk.model_stated && risk.model_stated !== risk.level && (
            <li className="mt-1 border-t border-[rgb(var(--border))] pt-1.5 opacity-70">
              Model stated <strong>{risk.model_stated}</strong>; the system escalated to{" "}
              <strong>{risk.level}</strong> based on the factors above.
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
