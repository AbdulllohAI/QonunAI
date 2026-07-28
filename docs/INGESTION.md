# Ingestion reference

How raw legal documents become retrievable, citable chunks.

```
connector → parser → hierarchy builder → versioner ─┐
                            │                       ├→ Postgres
                            ├→ cross-ref extractor ─┤
                            └→ chunker → embedder ──┘
```

---

## 1. The chunk metadata contract

Every `Chunk` row **must** carry these fields. They are what makes an answer
citable and auditable, and the reasoning engine assumes all of them are present.

| Field | Source | Why it is required |
|---|---|---|
| `law_name` | `LegalAct.short_name` | Rendered in every citation ("…of the Civil Code") |
| `article_number` | nearest ancestor `MODDA` node | The citable unit; also drives exact-article lookup |
| `jurisdiction` | always `"Uzbekistan"` | Guards against cross-jurisdiction contamination if the corpus is ever extended |
| `language` | connector / CSV loader | Selects the Postgres FTS dictionary and filters retrieval |
| `date_of_adoption` | act metadata | Input to *lex posterior* conflict resolution |
| `last_updated` | act metadata | Shown to the user; also *lex posterior* input |

Two more are carried for reasoning rather than citation:

| Field | Purpose |
|---|---|
| `act_type` | Legal force — drives hierarchy ordering and context grouping |
| `hierarchy_path` | Provenance, e.g. `UMUMIY QISM/BIRINCHI BO‘LIM/I/1` |

`article_number` is **denormalised down the tree**: a `BAND` (clause) inherits
its parent article's number. Without this, clause-level chunks would cite only
their act and the user could not locate them.

---

## 2. Structural hierarchy

`hierarchy_builder.py` runs a state machine over the flat block stream emitted by
the parsers. Each block is classified by regex into a level; the open-node stack
is popped to that level, then the new node pushed. Unclassifiable text attaches
to the current node rather than being dropped.

| Level | Uzbek | Russian | English | Example marker |
|---|---|---|---|---|
| 1 | Qism | Часть | Part | `UMUMIY QISM` |
| 2 | Bo‘lim | Раздел | Section | `BIRINCHI BO‘LIM`, `РАЗДЕЛ II` |
| 3 | Bob | Глава | Chapter | `III bob`, `ГЛАВА IV` |
| 4 | **Modda** | **Статья** | **Article** | `54-modda`, `Статья 105` |
| 5 | Band | Пункт | Clause | `1)` |
| 6 | Qismcha | Подпункт | Sub-clause | `a)` |

Real acts skip levels — a Law has articles but often no Parts. The builder
handles this by popping to the *level index*, not by assuming a fixed depth.

---

## 3. Chunking strategy

Generic fixed-window chunking is actively harmful here: it splits an article
mid-sentence, so a chunk can state a rule without its exception, and the
citation metadata becomes ambiguous.

| Case | Behaviour |
|---|---|
| Article fits the budget | **One article = one chunk** |
| Article too long | Split on **clause** boundaries, then paragraph, then sentence (with overlap) |
| Single oversized clause | Hard character split with overlap — last resort |
| Bare structural heading | Carried forward as context onto following articles, not indexed alone |

Every chunk repeats its structural context (chapter heading + article heading)
because chunks are retrieved in isolation — a body fragment with no article
label is unusable to the model and uncitable to the user.

The header is counted against the token budget (`overhead` in `_split_body`), so
a long chapter title cannot silently push chunks past `CHUNK_MAX_TOKENS`.

---

## 4. Idempotency

An act is keyed by `(source, external_id)`; `content_hash` decides whether
anything downstream runs.

- **Unchanged act** → one HTTP request, no parsing, no embedding, no writes.
- **Changed act** → the act's chunks and nodes *for that language* are deleted
  and rebuilt. Wholesale replacement is safer than diffing a restructured
  document node by node.
- **Article text changed** → the open `LegalActVersion` is closed (`valid_to`
  set) and a new one opened. This is what powers the timeline and diff views.
- **Any change** → a `LegalAlert` row is written (`new` / `amended` / `repealed`).

Re-running ingestion over an unchanged corpus is therefore cheap and produces no
duplicates.

---

## 5. Cross-references

Extracted at ingest from every article body:

| Form | Example |
|---|---|
| Self-reference | `ushbu Kodeksning 333-moddasi`, `статьей 45 настоящего Кодекса` |
| External | `Fuqarolik kodeksining 54-moddasi`, `Article 54 of the Civil Code` |

External targets whose act has not yet been ingested are stored **unresolved**
(`target_act_id = NULL`, `target_raw` kept). The daily
`resolve_pending_crossrefs_task` sweeps them up once the target act exists —
otherwise a reference to a law crawled later would be lost permanently.

At query time, `expand_cross_references` pulls the referenced articles into
context, labelled as supporting rather than primary.

---

## 6. Adding a new connector

Subclass `BaseConnector` and implement two methods:

```python
class MyConnector(BaseConnector):
    name = "my_source"
    source = SourceSystem.MANUAL

    def __init__(self, **kwargs):
        self.base_url = "https://example.uz"
        super().__init__(**kwargs)

    async def discover(self, **kwargs) -> AsyncIterator[str]:
        yield "doc-123"

    async def fetch_act(self, identifier: str, language: Language) -> RawAct | None:
        resp = await self.fetch(f"{self.base_url}/docs/{identifier}")
        return RawAct(
            external_id=identifier,
            source=self.source,
            source_url=str(resp.url),
            content=resp.content,
            mime_type="text/html",
            language=language,
            act_type=ActType.LAW,
        )
```

Register it in `connectors/__init__.py`. Rate limiting, robots.txt, retry with
exponential backoff and jitter, and 429 `Retry-After` handling all come from the
base class — do not reimplement or bypass them.

**Type non-statutory sources correctly.** Anything that is not a normative act
must be `ActType.COMMENTARY` or `COURT_DECISION`, so the reasoning engine labels
it non-binding rather than citing it as law.

---

## 7. Loading pre-structured CSV

The fastest path to a working index. Two layouts are auto-detected:

| Layout | Columns |
|---|---|
| `jinoyat` | `Qism, Bo'lim, Bob raqami, Bob nomi, Modda raqami, Modda nomi, Modda matni` |
| `konstitutsiya` | `modda_raqami, bolim, bob_raqami, bob_nomi, matn` |

Files are read with `utf-8-sig` — these exports carry a BOM, and without it the
first column name becomes `"﻿Qism"` and layout detection fails silently.

Rows sharing an article number are **grouped before chunking**. The Constitution
export emits one row per clause; without grouping, every clause would become its
own "article" and citations would be wrong.

For any other layout, pass an explicit `ColumnMap`:

```python
from app.services.ingestion.seed_csv import ColumnMap, csv_seed_loader

await csv_seed_loader.load(
    session, "mehnat.csv",
    short_name="Mehnat kodeksi",
    column_map=ColumnMap(
        article_number="modda", body="matn",
        article_title="sarlavha", chapter_number="bob",
    ),
)
```

---

## 8. Parser notes

| Format | Notes |
|---|---|
| HTML | Content root found by selector list; nested containers skipped so paragraphs are not emitted once per ancestor |
| PDF | Heading detection uses modal font size + regex. **Pages with no extractable text trigger OCR** (`uzb+rus+eng`) — without the Tesseract language packs, scanned acts ingest as empty documents |
| DOCX | Uses Word outline levels when present; falls back to all-bold-short-paragraph, then regex. Tables are kept (tariffs, penalty scales) |

PDF line wrapping is repaired by `_merge_wrapped_lines`: a line not ending in
sentence punctuation, followed by one starting lowercase, is a continuation.

---

## 9. Search vector population

The `tsvector` is written **in SQL after chunk insert**, not in Python:

```sql
setweight(to_tsvector(:config, law_name),  'A') ||
setweight(to_tsvector(:config, heading),   'B') ||
setweight(to_tsvector(:config, text),      'C')
```

`:config` is the language's dictionary (`russian`, `english`, or `simple` for
both Uzbek scripts). Doing this in SQL guarantees the indexing dictionary
matches the one `to_tsquery` uses at query time — a mismatch returns zero
keyword hits with no error.

Uzbek has no Postgres stemmer, so the query builder compensates with prefix
matching (`term:*`); both languages are heavily inflected and an exact-token
query would miss `shartnomani` when the user typed `shartnoma`.
