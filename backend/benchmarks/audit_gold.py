"""Check every benchmark gold label against the live corpus.

A benchmark can be wrong in ways that look exactly like the system being wrong.
Four items in `uzlegal-v1` named a gold article whose title was shared by
another article in the same act — Criminal Code 73 and 89 are both
"Условно-досрочное освобождение от отбывания наказания", and the Tax Code
carries "Солиқ тўловчилар" fourteen times, once per tax type. Retrieval was
being marked wrong for returning an equally correct article, and no amount of
tuning would have fixed that.

Run this after editing the benchmark:

    python benchmarks/audit_gold.py --base https://uzlex-ai.fly.dev

Exits non-zero if any gold label is missing from the corpus or ambiguous
within its own act, so it can gate a change to the benchmark.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.request import urlopen

DEFAULT_BENCHMARK = Path(__file__).with_name("uzlegal_v1.json")


def _strip_label(heading: str | None) -> str:
    """Compare titles, not their article labels: every heading starts "N-modda."."""
    without_label = re.sub(r"^\s*\S*-?modda\.?\s*", "", heading or "", flags=re.I)
    return re.sub(r"\W+", "", without_label.lower())


def fetch_corpus(base: str) -> dict:
    with urlopen(f"{base.rstrip('/')}/api/v1/corpus/articles", timeout=120) as response:
        return json.load(response)


def audit(items: list[dict], headings: dict, law_names: dict) -> list[str]:
    """One message per problem; empty means the gold labels are sound."""
    problems: list[str] = []
    for item in items:
        gold = item.get("gold_article")
        if not gold:
            continue  # out-of-scope and adversarial items carry no gold
        act = item.get("act")
        if not act:
            problems.append(f"{item['id']}: has a gold article but no act to disambiguate it")
            continue

        fragments = act if isinstance(act, list) else [act]
        acts = [
            act_id
            for act_id, name in law_names.items()
            if any(f.lower() in (name or "").lower() for f in fragments)
        ]
        accepted = {str(g) for g in (gold if isinstance(gold, list) else [gold])}

        found = [(a, n) for a in acts for n in accepted if (a, n) in headings]
        if not found:
            problems.append(f"{item['id']}: gold {sorted(accepted)} not in {fragments}")
            continue

        titles = {_strip_label(headings[key]) for key in found}
        twins = [
            f"{number}"
            for (act_id, number), heading in headings.items()
            if act_id in acts
            and number not in accepted
            and _strip_label(heading)
            and _strip_label(heading) in titles
        ]
        if twins:
            problems.append(
                f"{item['id']}: gold {sorted(accepted)} shares its title with "
                f"{sorted(twins)[:4]} in the same act — either accept them all "
                f"or make the question specific"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="https://uzlex-ai.fly.dev")
    parser.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    args = parser.parse_args()

    items = json.loads(args.benchmark.read_text(encoding="utf-8"))["items"]
    corpus = fetch_corpus(args.base)
    headings = {(row["act_id"], str(row["article_number"])): row["heading"] for row in corpus["rows"]}
    law_names = {row["act_id"]: row["law_name"] for row in corpus["rows"]}

    problems = audit(items, headings, law_names)
    scored = sum(1 for i in items if i.get("gold_article"))
    for problem in problems:
        print(f"  {problem}")
    print(f"\n{scored} scored items, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
