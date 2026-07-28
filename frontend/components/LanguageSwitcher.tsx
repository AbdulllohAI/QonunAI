"use client";

import { Languages } from "lucide-react";
import { LANGUAGE_LABELS } from "@/lib/i18n";
import type { Language } from "@/lib/types";
import { cn } from "@/lib/utils";

const ORDER: Language[] = ["uz-Latn", "uz-Cyrl", "ru", "en"];

export function LanguageSwitcher({
  value,
  onChange,
}: {
  value: Language;
  onChange: (lang: Language) => void;
}) {
  return (
    <div className="flex items-center gap-1">
      <Languages size={14} className="opacity-50" aria-hidden />
      <div
        className="flex overflow-hidden rounded-lg border border-[rgb(var(--border))]"
        role="group"
        aria-label="Answer language"
      >
        {ORDER.map((lang) => (
          <button
            key={lang}
            type="button"
            onClick={() => onChange(lang)}
            aria-pressed={value === lang}
            className={cn(
              "px-2.5 py-1 text-xs transition-colors",
              value === lang
                ? "bg-blue-700 text-white"
                : "hover:bg-black/5 dark:hover:bg-white/5",
            )}
          >
            {LANGUAGE_LABELS[lang]}
          </button>
        ))}
      </div>
    </div>
  );
}
