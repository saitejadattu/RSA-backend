from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config.settings import get_settings


class MongoDB:
    client: AsyncIOMotorClient | None = None
    database: AsyncIOMotorDatabase | None = None


mongodb = MongoDB()


async def connect_to_mongo() -> None:
    settings = get_settings()
    # Pool + fail-fast timeouts so a slow/unreachable Atlas doesn't stall requests
    # indefinitely, and connections are reused across requests instead of re-opened.
    mongodb.client = AsyncIOMotorClient(
        settings.mongo_uri,
        maxPoolSize=50,
        minPoolSize=5,            # keep a few warm so the first query isn't a cold connect
        maxIdleTimeMS=60_000,
        serverSelectionTimeoutMS=8_000,
        connectTimeoutMS=8_000,
        socketTimeoutMS=30_000,
        retryWrites=True,
    )
    mongodb.database = mongodb.client[settings.mongo_db_name]
    # Establish the connection now (Motor connects lazily) so the first real
    # request after boot doesn't pay the TLS/handshake cost.
    await mongodb.client.admin.command("ping")


async def close_mongo_connection() -> None:
    if mongodb.client is not None:
        mongodb.client.close()


def get_database() -> AsyncIOMotorDatabase:
    
    if mongodb.database is None:
        raise RuntimeError("MongoDB is not connected")
    return mongodb.database
