"""landing cms + revalidation outbox

Revision ID: b937ac8c5a82
Revises: a56efdfd1109
Create Date: 2026-08-04 12:10:54.080114

ATENÇÃO — esta migration foi escrita à mão a partir de docs/landing_cms/schema.sql.

O ``flask db migrate`` autogenerate NÃO emite: CHECK constraints, índices parciais,
índices GIN, funções, triggers nem server defaults. A versão autogerada criava as
tabelas mas deixava o trigger QUEBRADO: ele insere na outbox fornecendo apenas 5
colunas, e ``event_id``, ``attempts``, ``next_attempt_at`` e ``criado_em`` precisam
de DEFAULT no banco — os defaults do modelo são Python-side e não valem para um
INSERT feito de dentro do Postgres.

Se os modelos mudarem, atualize docs/landing_cms/schema.sql e esta migration juntos.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b937ac8c5a82'
down_revision = 'a56efdfd1109'
branch_labels = None
depends_on = None


def upgrade():
    # ------------------------------------------------------------------
    # 1. landing_pages
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE sistur_landing_pages (
            id                BIGSERIAL   PRIMARY KEY,
            slug              TEXT        NOT NULL,
            locale            TEXT        NOT NULL DEFAULT 'pt-BR',
            status            TEXT        NOT NULL DEFAULT 'draft',

            seo_title         TEXT,
            meta_description  TEXT,
            og_image_url      TEXT,
            canonical_url     TEXT,
            noindex           BOOLEAN     NOT NULL DEFAULT FALSE,

            content_jsonb     JSONB       NOT NULL DEFAULT '{"blocks": []}'::jsonb,
            content_version   INTEGER     NOT NULL DEFAULT 1,

            published_at      TIMESTAMPTZ,
            criado_em         TIMESTAMPTZ NOT NULL DEFAULT now(),
            atualizado_em     TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at        TIMESTAMPTZ,

            CONSTRAINT ck_landing_status
                CHECK (status IN ('draft', 'published', 'archived')),

            CONSTRAINT ck_landing_slug_shape
                CHECK (slug = '' OR slug ~ '^[a-z0-9]+(?:[-a-z0-9]+)*(?:/[a-z0-9]+(?:[-a-z0-9]+)*)*$'),

            CONSTRAINT ck_landing_content_shape
                CHECK (jsonb_typeof(content_jsonb) = 'object'
                       AND jsonb_typeof(content_jsonb -> 'blocks') = 'array'),

            -- \\y é fronteira de palavra no Postgres ARE. \\b significa BACKSPACE e
            -- faria esta guarda nunca casar — código morto de aparência correta.
            CONSTRAINT ck_landing_no_markup
                CHECK (content_jsonb::text !~* '<\\s*/?\\s*(script|iframe|object|embed|style|link|svg)\\y')
        );
    """)

    op.execute("""
        CREATE UNIQUE INDEX uq_landing_slug_locale_live
            ON sistur_landing_pages (slug, locale)
            WHERE deleted_at IS NULL;
    """)
    op.execute("""
        CREATE INDEX ix_landing_published
            ON sistur_landing_pages (slug, locale)
            WHERE deleted_at IS NULL AND status = 'published';
    """)
    op.execute("""
        CREATE INDEX ix_sistur_landing_pages_status
            ON sistur_landing_pages (status);
    """)
    op.execute("""
        CREATE INDEX ix_landing_content_gin
            ON sistur_landing_pages USING GIN (content_jsonb jsonb_path_ops);
    """)

    # ------------------------------------------------------------------
    # 2. revalidation outbox
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE sistur_revalidation_outbox (
            id               BIGSERIAL   PRIMARY KEY,
            event_id         UUID        NOT NULL DEFAULT gen_random_uuid(),

            tags             TEXT[]      NOT NULL,
            paths            TEXT[]      NOT NULL DEFAULT '{}',

            entity_table     TEXT,
            entity_id        BIGINT,
            reason           TEXT,

            attempts         INTEGER     NOT NULL DEFAULT 0,
            next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            delivered_at     TIMESTAMPTZ,
            last_error       TEXT,
            criado_em        TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT ck_outbox_tags_nonempty CHECK (cardinality(tags) > 0)
        );
    """)

    op.execute("""
        CREATE INDEX ix_outbox_pending
            ON sistur_revalidation_outbox (next_attempt_at, id)
            WHERE delivered_at IS NULL;
    """)
    op.execute("""
        CREATE INDEX ix_outbox_stuck
            ON sistur_revalidation_outbox (criado_em)
            WHERE delivered_at IS NULL;
    """)

    # ------------------------------------------------------------------
    # 3. Trigger — enfileira na outbox e acorda o worker
    # ------------------------------------------------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION sistur_landing_pages_revalidate()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_row     sistur_landing_pages%ROWTYPE;
            v_tags    TEXT[];
            v_paths   TEXT[];
            v_event   UUID;
            v_reason  TEXT;
        BEGIN
            v_row := COALESCE(NEW, OLD);

            IF TG_OP = 'UPDATE' THEN
                IF  NEW.content_jsonb     IS NOT DISTINCT FROM OLD.content_jsonb
                AND NEW.seo_title         IS NOT DISTINCT FROM OLD.seo_title
                AND NEW.meta_description  IS NOT DISTINCT FROM OLD.meta_description
                AND NEW.og_image_url      IS NOT DISTINCT FROM OLD.og_image_url
                AND NEW.canonical_url     IS NOT DISTINCT FROM OLD.canonical_url
                AND NEW.noindex           IS NOT DISTINCT FROM OLD.noindex
                AND NEW.status            IS NOT DISTINCT FROM OLD.status
                AND NEW.slug              IS NOT DISTINCT FROM OLD.slug
                AND NEW.deleted_at        IS NOT DISTINCT FROM OLD.deleted_at
                THEN
                    RETURN NULL;
                END IF;
                v_reason := 'update';
            ELSIF TG_OP = 'INSERT' THEN
                v_reason := 'insert';
            ELSE
                v_reason := 'delete';
            END IF;

            v_tags := ARRAY['page:' || v_row.slug, 'pages:index'];
            v_paths := ARRAY['/' || v_row.slug];

            IF TG_OP = 'UPDATE' AND NEW.slug IS DISTINCT FROM OLD.slug THEN
                v_tags  := v_tags  || ('page:' || OLD.slug);
                v_paths := v_paths || ('/' || OLD.slug);
            END IF;

            INSERT INTO sistur_revalidation_outbox (tags, paths, entity_table, entity_id, reason)
            VALUES (v_tags, v_paths, TG_TABLE_NAME, v_row.id, v_reason)
            RETURNING event_id INTO v_event;

            PERFORM pg_notify(
                'sistur_revalidate',
                json_build_object('event_id', v_event, 'table', TG_TABLE_NAME)::text
            );

            RETURN NULL;
        END;
        $$;
    """)

    op.execute("""
        CREATE TRIGGER trg_landing_pages_revalidate
            AFTER INSERT OR UPDATE OR DELETE ON sistur_landing_pages
            FOR EACH ROW
            EXECUTE FUNCTION sistur_landing_pages_revalidate();
    """)


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trg_landing_pages_revalidate ON sistur_landing_pages;")
    op.execute("DROP FUNCTION IF EXISTS sistur_landing_pages_revalidate();")
    op.execute("DROP TABLE IF EXISTS sistur_revalidation_outbox;")
    op.execute("DROP TABLE IF EXISTS sistur_landing_pages;")
