#!/usr/bin/env python
"""Bootstrap the corpus and an admin user.

Fastest path from empty database to working system:

    python -m scripts.bootstrap --admin admin@example.uz --seed-csv-dir ../../Hybrid-Rag

Loading the pre-structured CSVs is strongly preferred over scraping for a first
run: it is instant, deterministic, and gives you a real index to evaluate
retrieval against before you point anything at lex.uz.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.models import ActType, Language, User, UserRole  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.services.ingestion import csv_seed_loader, get_connector, ingestion_pipeline  # noqa: E402

log = get_logger("bootstrap")

# Known files shipped with the Hybrid-Rag corpus, mapped to act metadata.
KNOWN_CSVS: dict[str, dict] = {
    "Jinoyat_kodeksi_lotin_bolim_bob_modda.csv": {
        "short_name": "Jinoyat kodeksi",
        "title": "O‘zbekiston Respublikasining Jinoyat kodeksi",
        "act_type": ActType.CODE,
        "language": Language.UZ_LATN,
        "source_url": "https://lex.uz/docs/111457",
        "date_of_adoption": date(1994, 9, 22),
    },
    "konstitutsiya_moddalar.csv": {
        "short_name": "Konstitutsiya",
        "title": "O‘zbekiston Respublikasining Konstitutsiyasi",
        "act_type": ActType.CONSTITUTION,
        "language": Language.UZ_LATN,
        "source_url": "https://lex.uz/docs/6445145",
        "date_of_adoption": date(2023, 4, 30),
    },
}


async def create_admin(email: str, password: str | None) -> None:
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(User).where(User.email == email.lower()))
        ).scalar_one_or_none()
        if existing:
            existing.role = UserRole.ADMIN
            await session.commit()
            print(f"  promoted existing user {email} to admin")
            return

        if not password:
            password = getpass.getpass(f"password for {email}: ")
            if len(password) < 8:
                raise SystemExit("password must be at least 8 characters")

        session.add(
            User(
                email=email.lower(),
                hashed_password=hash_password(password),
                full_name="Administrator",
                role=UserRole.ADMIN,
            )
        )
        await session.commit()
        print(f"  created admin {email}")


async def seed_csvs(directory: Path) -> None:
    if not directory.exists():
        print(f"  ! directory not found: {directory}")
        return

    found = 0
    for filename, meta in KNOWN_CSVS.items():
        path = directory / filename
        if not path.exists():
            continue
        found += 1
        print(f"  loading {filename} → {meta['short_name']} ...")
        async with SessionLocal() as session:
            stats = await csv_seed_loader.load(
                session,
                path,
                short_name=meta["short_name"],
                title=meta["title"],
                act_type=meta["act_type"],
                language=meta["language"],
                source_url=meta["source_url"],
                date_of_adoption=meta.get("date_of_adoption"),
            )
        print(
            f"    articles={stats.nodes_written} chunks={stats.chunks_written} "
            f"crossrefs={stats.crossrefs_written} errors={len(stats.errors)}"
        )
        for err in stats.errors[:5]:
            print(f"    ! {err}")

    # Anything else in the directory: try layout auto-detection.
    for path in sorted(directory.glob("*.csv")):
        if path.name in KNOWN_CSVS:
            continue
        print(f"  attempting auto-detect on {path.name} ...")
        async with SessionLocal() as session:
            stats = await csv_seed_loader.load(
                session, path, short_name=path.stem.replace("_", " ").title()
            )
        if stats.errors and not stats.chunks_written:
            print(f"    skipped: {stats.errors[0]}")
        else:
            found += 1
            print(f"    chunks={stats.chunks_written}")

    if not found:
        print(f"  no loadable CSVs found in {directory}")


async def seed_lexuz(
    limit: int | None, languages: list[Language], force: bool = False
) -> None:
    print("  fetching seed acts from lex.uz (this is rate-limited and will take a while)")
    if force:
        print("  --force: re-parsing even when the source text is unchanged")
    async with SessionLocal() as session:
        connector = get_connector("lexuz")
        stats = await ingestion_pipeline.run_connector(
            session, connector, languages=languages, seeds=True, limit=limit, force=force
        )
    print(f"    {stats.to_dict()}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap QonunAI")
    parser.add_argument("--admin", help="admin email to create")
    parser.add_argument("--admin-password", help="admin password (prompted if omitted)")
    parser.add_argument(
        "--seed-csv-dir",
        help="directory containing pre-structured legal CSVs (fastest path)",
    )
    parser.add_argument(
        "--seed-lexuz",
        action="store_true",
        help="also crawl the seeded acts from lex.uz",
    )
    parser.add_argument("--limit", type=int, default=None, help="max acts to fetch from lex.uz")
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "re-parse and re-embed even when the source text is unchanged. "
            "Needed after a parser/hierarchy fix, since ingestion is keyed on "
            "the content hash and would otherwise skip every existing act."
        ),
    )
    parser.add_argument(
        "--languages",
        default="uz-Latn,ru",
        help="comma-separated languages to ingest from lex.uz",
    )
    args = parser.parse_args()

    configure_logging()

    try:
        if args.admin:
            print("→ admin user")
            await create_admin(args.admin, args.admin_password)

        if args.seed_csv_dir:
            print("→ CSV corpus")
            await seed_csvs(Path(args.seed_csv_dir).expanduser().resolve())

        if args.seed_lexuz:
            print("→ lex.uz")
            await seed_lexuz(
                args.limit,
                [Language(v.strip()) for v in args.languages.split(",") if v.strip()],
                force=args.force,
            )

        if not any((args.admin, args.seed_csv_dir, args.seed_lexuz)):
            parser.print_help()
            return

        print("\ndone. Verify with:  curl localhost:8000/health")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
