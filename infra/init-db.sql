-- Extensions must exist before Alembic runs.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Uzbek has no Postgres stemmer, so 'simple' (tokenise, lowercase, no stemming)
-- is used for both scripts. `unaccent` folds diacritics so a query typed without
-- them still matches. Prefix matching in the query builder compensates for the
-- lack of stemming on this agglutinative language.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'uzbek') THEN
        CREATE TEXT SEARCH CONFIGURATION uzbek (COPY = simple);
        ALTER TEXT SEARCH CONFIGURATION uzbek
            ALTER MAPPING FOR hword, hword_part, word
            WITH unaccent, simple;
    END IF;
END
$$;
