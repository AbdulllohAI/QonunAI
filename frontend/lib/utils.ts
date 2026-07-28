import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import type { Language, RiskLevel } from "./types";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export const RISK_STYLES: Record<RiskLevel, string> = {
  low: "bg-emerald-50 text-emerald-800 border-emerald-300 dark:bg-emerald-950 dark:text-emerald-200 dark:border-emerald-800",
  medium:
    "bg-amber-50 text-amber-900 border-amber-300 dark:bg-amber-950 dark:text-amber-200 dark:border-amber-800",
  high: "bg-red-50 text-red-800 border-red-300 dark:bg-red-950 dark:text-red-200 dark:border-red-800",
};

/** Colour by legal force — the visual cue for the hierarchy of norms. */
export const ACT_TYPE_STYLES: Record<string, string> = {
  constitution: "bg-violet-100 text-violet-900 dark:bg-violet-950 dark:text-violet-200",
  constitutional_law: "bg-violet-50 text-violet-800 dark:bg-violet-900 dark:text-violet-200",
  code: "bg-blue-100 text-blue-900 dark:bg-blue-950 dark:text-blue-200",
  law: "bg-sky-100 text-sky-900 dark:bg-sky-950 dark:text-sky-200",
  presidential_decree: "bg-teal-100 text-teal-900 dark:bg-teal-950 dark:text-teal-200",
  presidential_resolution: "bg-teal-50 text-teal-800 dark:bg-teal-900 dark:text-teal-200",
  cabinet_resolution: "bg-cyan-100 text-cyan-900 dark:bg-cyan-950 dark:text-cyan-200",
  ministerial_act: "bg-slate-100 text-slate-800 dark:bg-slate-800 dark:text-slate-200",
  local_act: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
  court_decision: "bg-orange-100 text-orange-900 dark:bg-orange-950 dark:text-orange-200",
  commentary: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
};

export const ACT_TYPE_LABELS: Record<string, string> = {
  constitution: "Constitution",
  constitutional_law: "Constitutional law",
  code: "Code",
  law: "Law",
  presidential_decree: "Decree",
  presidential_resolution: "Presidential resolution",
  cabinet_resolution: "Cabinet resolution",
  ministerial_act: "Ministerial act",
  local_act: "Local act",
  court_decision: "Court decision",
  commentary: "Commentary",
};

/** Non-binding sources must be visually distinguishable from statute. */
export function isBinding(actType: string): boolean {
  return actType !== "commentary" && actType !== "court_decision";
}

export function detectBrowserLanguage(): Language {
  if (typeof navigator === "undefined") return "uz-Latn";
  const raw = navigator.language.toLowerCase();
  if (raw.startsWith("ru")) return "ru";
  if (raw.startsWith("en")) return "en";
  return "uz-Latn";
}

export function formatMs(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function uid(): string {
  return Math.random().toString(36).slice(2, 11);
}

const SUPERSCRIPT_DIGITS: Record<string, string> = {
  "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
  "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
};

function toSuperscript(n: number): string {
  return String(n)
    .split("")
    .map((d) => SUPERSCRIPT_DIGITS[d] ?? d)
    .join("");
}

/**
 * The backend's `[S1]`/`[S2]` tags are the citation-validation mechanism
 * itself — the model is instructed to cite only those tags, and the
 * validator strips or flags anything that doesn't resolve to a real
 * retrieved source. They're load-bearing for the backend and need to stay
 * in the text it validates; they're just not something a reader should have
 * to look at. Renumber them here, purely for display, to small superscripts
 * matching each source's position in the Sources panel below (which is
 * already in first-appearance order) — so "[S7]" in raw model output
 * becomes "¹" if it's the first citation actually used, "²" for the second
 * distinct one, and so on, with the same numbering in both places.
 */
export function renumberCitations(text: string, citations: { tag: string }[]): string {
  const order = new Map<string, number>();
  citations.forEach((c, i) => {
    if (!order.has(c.tag)) order.set(c.tag, i + 1);
  });
  return text.replace(/(?:\[S\d+\])+/g, (run) => {
    const nums = Array.from(run.matchAll(/\[S(\d+)\]/g))
      .map((m) => order.get(`S${m[1]}`))
      .filter((n): n is number => n !== undefined);
    return nums.map(toSuperscript).join(",");
  });
}
