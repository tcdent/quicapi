"""Test schema definition and seed data generation.

Defines the ``articles`` table used across all benchmark strategies and provides
helpers for seeding rows with controllable TOAST payload sizes.
"""

from __future__ import annotations

import hashlib
import random
import string
import textwrap
from enum import Enum
from typing import Any

import sqlalchemy as sa

metadata = sa.MetaData()

articles = sa.Table(
    "articles",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("slug", sa.String(128), unique=True, nullable=False),
    sa.Column("author_id", sa.BigInteger, nullable=False),
    sa.Column(
        "title", sa.String(256), nullable=False
    ),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    ),
    sa.Column("summary", sa.String(512)),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("metadata", sa.JSON),
)

# Explicit indexes matching the spec.
IDX_SLUG = sa.Index("idx_articles_slug", articles.c.slug)
IDX_AUTHOR = sa.Index("idx_articles_author", articles.c.author_id)
IDX_CREATED = sa.Index("idx_articles_created", articles.c.created_at.desc())


# -- Tier column groupings ---------------------------------------------------

TIER_0_COLS = [articles.c.id, articles.c.slug, articles.c.author_id]
TIER_1_COLS = [articles.c.title, articles.c.created_at, articles.c.summary]
TIER_2_COLS = [articles.c.body, articles.c.metadata]


class ToastSize(str, Enum):
    """Controls the ``body`` column size during seeding."""

    SMALL = "small"  # < 2 KB  — stays inline
    MEDIUM = "medium"  # ~8 KB  — single TOAST chunk
    LARGE = "large"  # ~100 KB — multi-chunk TOAST
    MIXED = "mixed"  # Random per-row: power-law distribution from ~500B to ~200KB


# Approximate byte targets per toast category.
_TOAST_BYTES = {
    ToastSize.SMALL: 1_500,
    ToastSize.MEDIUM: 8_000,
    ToastSize.LARGE: 100_000,
}

# Distribution buckets for MIXED mode.  Each row randomly picks a size from
# a power-law-ish distribution that skews toward small but includes a fat
# tail of very large rows — mimicking real-world CMS / blog tables.
_MIXED_DISTRIBUTION = [
    # (weight, min_bytes, max_bytes)
    (40, 200, 1_500),       # ~40% tiny  (inline, no TOAST)
    (25, 1_500, 4_000),     # ~25% small (near TOAST threshold)
    (15, 4_000, 8_000),     # ~15% medium (single TOAST chunk)
    (10, 8_000, 30_000),    # ~10% large
    (7, 30_000, 100_000),   #  ~7% very large (multi-chunk)
    (3, 100_000, 250_000),  #  ~3% huge  (stress TOAST decompression)
]
_MIXED_WEIGHTS = [w for w, _, _ in _MIXED_DISTRIBUTION]
_MIXED_RANGES = [(lo, hi) for _, lo, hi in _MIXED_DISTRIBUTION]

_RNG = random.Random(42)


def _random_text(nbytes: int) -> str:
    """Generate pseudo-random prose-like text of approximately *nbytes*."""
    words = []
    remaining = nbytes
    while remaining > 0:
        word_len = _RNG.randint(3, 12)
        word = "".join(_RNG.choices(string.ascii_lowercase, k=word_len))
        words.append(word)
        remaining -= word_len + 1  # +1 for the space
    return " ".join(words)[:nbytes]


def _make_metadata(row_id: int, toast_size: ToastSize) -> dict[str, Any]:
    """Generate a JSONB metadata blob that scales with *toast_size*."""
    base: dict[str, Any] = {
        "version": 1,
        "row_hash": hashlib.md5(str(row_id).encode()).hexdigest(),
        "tags": [f"tag-{i}" for i in range(5)],
    }
    if toast_size == ToastSize.LARGE:
        # Push the JSONB into TOAST territory.
        base["extra"] = {f"key_{i}": _random_text(200) for i in range(50)}
    elif toast_size == ToastSize.MEDIUM:
        base["extra"] = {f"key_{i}": _random_text(100) for i in range(10)}
    return base


def _pick_mixed_body_size() -> int:
    """Sample a body size from the mixed distribution."""
    [bucket] = _RNG.choices(_MIXED_RANGES, weights=_MIXED_WEIGHTS, k=1)
    return _RNG.randint(bucket[0], bucket[1])


def _effective_toast_for_size(nbytes: int) -> ToastSize:
    """Map a concrete byte size to the closest uniform ToastSize for metadata
    generation purposes."""
    if nbytes < 2_000:
        return ToastSize.SMALL
    if nbytes < 30_000:
        return ToastSize.MEDIUM
    return ToastSize.LARGE


def generate_rows(
    count: int,
    toast_size: ToastSize,
    *,
    start_id: int = 1,
) -> list[dict[str, Any]]:
    """Return *count* row dicts ready for bulk insert.

    When *toast_size* is ``MIXED``, each row gets a randomly chosen body size
    drawn from a power-law-ish distribution (see ``_MIXED_DISTRIBUTION``).
    """
    rows: list[dict[str, Any]] = []
    for i in range(start_id, start_id + count):
        if toast_size == ToastSize.MIXED:
            body_bytes = _pick_mixed_body_size()
            meta_toast = _effective_toast_for_size(body_bytes)
        else:
            body_bytes = _TOAST_BYTES[toast_size]
            meta_toast = toast_size

        rows.append(
            {
                "id": i,
                "slug": f"article-{i}",
                "author_id": _RNG.randint(1, 500),
                "title": f"Article #{i}: {_random_text(60)}",
                "summary": _random_text(200),
                "body": _random_text(body_bytes),
                "metadata": _make_metadata(i, meta_toast),
            }
        )
    return rows


# -- DDL helpers -------------------------------------------------------------

SETUP_SQL = textwrap.dedent("""\
    DROP TABLE IF EXISTS articles CASCADE;

    CREATE TABLE articles (
        id          BIGINT PRIMARY KEY,
        slug        VARCHAR(128) UNIQUE NOT NULL,
        author_id   BIGINT NOT NULL,
        title       VARCHAR(256) NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        summary     VARCHAR(512),
        body        TEXT NOT NULL,
        metadata    JSONB
    );

    CREATE INDEX idx_articles_slug    ON articles (slug);
    CREATE INDEX idx_articles_author  ON articles (author_id);
    CREATE INDEX idx_articles_created ON articles (created_at DESC);
""")
