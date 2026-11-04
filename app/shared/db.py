from psycopg_pool import AsyncConnectionPool


def make_pool(database_url: str) -> AsyncConnectionPool:
    # open=False + wait=False on open: app boots even when the DB is down.
    return AsyncConnectionPool(
        database_url,
        min_size=1,
        max_size=5,
        timeout=5,
        open=False,
        reconnect_timeout=30,
    )


async def check_db(pool: AsyncConnectionPool) -> bool:
    try:
        async with pool.connection(timeout=2) as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception:
        return False
