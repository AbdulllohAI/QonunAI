"""Run the uzlegal retrieval benchmark against a deployed QonunAI instance.

Two phases, deliberately separated by cost:

* **Retrieval** hits ``/api/v1/search``, which runs the full hybrid pipeline
  (dense + sparse + rerank) with no LLM call. It is free and fast, so every
  in-scope item runs. This is where Recall@k and MRR come from.

* **Answers** hits ``/api/v1/chat`` and does cost tokens, so it runs on a
  sample. This is where refusal behaviour and citation integrity come from —
  neither is observable from retrieval alone.

Usage
-----
    python run_benchmark.py --base https://uzlex-ai.fly.dev
    python run_benchmark.py --base http://localhost:8000 --answers 0
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

TIMEOUT = httpx.Timeout(180.0, connect=20.0)


def _safe_print(text: str) -> None:
    """Print without dying on a console that can't encode the corpus.

    Answers contain Cyrillic and emoji; a cp1251 Windows console raises
    UnicodeEncodeError mid-run and loses the whole report. Never let the
    formatter kill the measurement.
    """
    enc = sys.stdout.encoding or "utf-8"
    print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def load_items(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["items"]


def _act_matches(hit_law: str, expected_act: str | list[str] | None) -> bool:
    """Loose containment — the benchmark stores a short act fragment, the corpus
    stores the full dated title.

    A list means any of several acts is acceptable, which is needed where the
    same provision exists in more than one act: Criminal Code art. 97 is
    "Умышленное убийство" in the Russian act and "Qasddan odam o'ldirish" in the
    Latin one, the same law in two scripts. Scoring only one of them would mark
    a correct answer wrong for having answered in the user's own language.

    This is deliberately not the default. Art. 96 is "Место допроса" in the
    Criminal Procedure Code and something unrelated in the Criminal Code, so
    most items must still pin exactly one act.
    """
    if not expected_act:
        return True
    fragments = [expected_act] if isinstance(expected_act, str) else expected_act
    law = (hit_law or "").lower()
    return any(f.lower() in law for f in fragments)


def rank_of_gold(hits: list[dict[str, Any]], item: dict[str, Any]) -> int | None:
    """1-based rank of the gold article, or None if absent.

    Both the article number and the act must match: article 106 of the Civil
    Code is not a correct hit for a question about article 106 of the Labour
    Code, and matching on number alone would score that as success.

    `gold_article` may be a list, for the cases where the corpus genuinely
    carries the same provision under two numbers — Criminal Code articles 73
    and 89 are both titled "Условно-досрочное освобождение от отбывания
    наказания". Marking either one wrong would penalise a correct answer.
    """
    gold = item["gold_article"]
    accepted = {str(g) for g in (gold if isinstance(gold, list) else [gold])}
    for i, h in enumerate(hits, start=1):
        if str(h.get("article_number")) in accepted and _act_matches(
            h.get("law_name", ""), item.get("act")
        ):
            return i
    return None


def run_retrieval(client: httpx.Client, base: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [i for i in items if i.get("gold_article") and not i.get("expect")]
    ranks: list[int | None] = []
    latencies: list[int] = []
    failures: list[dict[str, Any]] = []

    for item in scored:
        try:
            r = client.post(
                f"{base}/api/v1/search",
                json={"query": item["question"], "language": item["lang"], "top_k": 10},
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # noqa: BLE001 - report, don't abort the run
            print(f"  !! {item['id']} request failed: {exc}", file=sys.stderr)
            ranks.append(None)
            failures.append({**item, "rank": None, "error": str(exc)})
            continue

        hits = data.get("hits", [])
        rank = rank_of_gold(hits, item)
        ranks.append(rank)
        latencies.append(int(data.get("took_ms", 0)))

        mark = "ok " if rank == 1 else ("~  " if rank and rank <= 5 else "MISS")
        top = hits[0] if hits else {}
        gold = item["gold_article"]
        gold_label = "/".join(str(g) for g in gold) if isinstance(gold, list) else str(gold)
        print(
            f"  [{mark}] {item['id']:8s} gold={gold_label:>6s} "
            f"rank={str(rank or '-'):>3s}  top={str(top.get('article_number')):>4s}"
        )
        if rank is None or rank > 5:
            failures.append(
                {
                    **item,
                    "rank": rank,
                    "top_hits": [
                        {"art": h.get("article_number"), "law": (h.get("law_name") or "")[:38]}
                        for h in hits[:3]
                    ],
                }
            )

    n = len(ranks)
    hit_at = lambda k: sum(1 for r in ranks if r and r <= k) / n if n else 0.0  # noqa: E731
    mrr = sum(1.0 / r for r in ranks if r) / n if n else 0.0

    return {
        "n": n,
        "recall@1": hit_at(1),
        "recall@3": hit_at(3),
        "recall@5": hit_at(5),
        "recall@10": hit_at(10),
        "mrr": mrr,
        "median_latency_ms": statistics.median(latencies) if latencies else 0,
        "failures": failures,
    }


REFUSAL_MARKERS = [
    "topilmadi", "топилмади", "не найдено", "не найден", "not found",
    "qamrab olmaydi", "qamramaydi", "не охватывают", "do not cover", "does not cover",
    "aniqroq", "уточните", "rephrase", "не содержит", "yo'q", "йўқ",
]


def looks_like_refusal(answer: str, answered: bool, citations: list[Any]) -> bool:
    """A refusal is either flagged by the API or carries no citations at all.

    The marker list is a fallback: the system can answer `answered=True` while
    still saying the corpus doesn't cover the question.
    """
    if not answered:
        return True
    if not citations:
        return True
    low = (answer or "").lower()
    return any(m in low for m in REFUSAL_MARKERS)


def run_answers(
    client: httpx.Client, base: str, items: list[dict[str, Any]], limit: int
) -> dict[str, Any]:
    sample = [i for i in items if i.get("expect") == "refuse"][:limit]
    results = []
    correct_refusals = 0

    for item in sample:
        try:
            r = client.post(
                f"{base}/api/v1/chat",
                json={
                    "message": item["question"],
                    "mode": "qa",
                    "language": item["lang"],
                    "compact": True,
                },
            )
            r.raise_for_status()
            d = r.json()
        except Exception as exc:  # noqa: BLE001
            print(f"  !! {item['id']} chat failed: {exc}", file=sys.stderr)
            results.append({"id": item["id"], "error": str(exc)})
            continue

        answer = d.get("answer", "")
        cites = d.get("citations", [])
        refused = looks_like_refusal(answer, d.get("answered", True), cites)
        correct_refusals += int(refused)
        results.append(
            {
                "id": item["id"],
                "category": item["category"],
                "refused": refused,
                "citations": len(cites),
                "answer_head": answer[:110].replace("\n", " "),
            }
        )
        print(f"  [{'REFUSED' if refused else 'ANSWERED'}] {item['id']:8s} {item['category']}")
        _safe_print("      " + answer[:100].replace("\n", " "))

    n = len(results)
    return {
        "n": n,
        "correct_refusal_rate": correct_refusals / n if n else 0.0,
        "results": results,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://uzlex-ai.fly.dev")
    ap.add_argument("--items", default=str(Path(__file__).parent / "uzlegal_v1.json"))
    ap.add_argument("--answers", type=int, default=6, help="how many refusal items to send to /chat")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    items = load_items(Path(args.items))
    base = args.base.rstrip("/")
    started = time.time()

    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        print(f"\n=== RETRIEVAL ({base}) ===")
        retrieval = run_retrieval(client, base, items)

        answers = {"n": 0, "correct_refusal_rate": 0.0, "results": []}
        if args.answers:
            print(f"\n=== REFUSAL BEHAVIOUR (sample of {args.answers}) ===")
            answers = run_answers(client, base, items, args.answers)

    report = {
        "base": base,
        "elapsed_s": round(time.time() - started, 1),
        "retrieval": retrieval,
        "answers": answers,
    }

    print("\n=== SUMMARY ===")
    r = retrieval
    print(f"  items scored     : {r['n']}")
    print(f"  Recall@1         : {r['recall@1']:.3f}")
    print(f"  Recall@3         : {r['recall@3']:.3f}")
    print(f"  Recall@5         : {r['recall@5']:.3f}   (target 0.90)")
    print(f"  Recall@10        : {r['recall@10']:.3f}   (target 0.95)")
    print(f"  MRR              : {r['mrr']:.3f}   (target 0.75)")
    print(f"  median latency   : {r['median_latency_ms']} ms")
    if answers["n"]:
        print(f"  correct refusals : {answers['correct_refusal_rate']:.3f}  (target 0.95)")

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  report written to {args.out}")


if __name__ == "__main__":
    main()
