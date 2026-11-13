"""The PostgreSQL test harness itself: it must isolate and clean up after itself."""

from __future__ import annotations

from sqlalchemy import Engine, text


def test_tables_live_in_a_throwaway_schema(pg_engine: Engine) -> None:
    with pg_engine.connect() as conn:
        schema = conn.execute(text("SELECT current_schema()")).scalar_one()
        assert schema.startswith("test_")
        tables = conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
            {"s": schema},
        ).scalars().all()
    assert "games" in tables


def test_the_collection_schema_is_untouched(pg_engine: Engine) -> None:
    # Nothing the tests do may create tables in `public`.
    with pg_engine.connect() as conn:
        leaked = conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        ).scalars().all()
    assert "games" not in leaked
