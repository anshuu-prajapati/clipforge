"""MongoDB: users, sessions, jobs (with their clips, post copy and publish status), schedule.

MONGODB_URI   connection string (mongodb+srv://… from Atlas, or mongodb://localhost:27017)
MONGODB_DB    database name (default: the one in the URI, else "clipforge")
"""
from __future__ import annotations

import functools
import os


class DatabaseError(RuntimeError):
    pass


@functools.cache
def db():
    from pymongo import ASCENDING, DESCENDING, MongoClient
    from pymongo.errors import PyMongoError

    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI") or os.environ.get("MONGODB_URL")
    if not uri:
        raise DatabaseError("Add MONGODB_URI=… to .env (your MongoDB connection string)")
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=8000, appname="clipforge")
        name = os.environ.get("MONGODB_DB")       # explicit name wins over one in the URI (e.g. a live/test split)
        d = client[name] if name else client.get_default_database(default="clipforge")
        d.users.create_index("email", unique=True)
        d.sessions.create_index("token", unique=True)
        d.sessions.create_index("expires", expireAfterSeconds=0)      # MongoDB deletes expired sessions
        d.jobs.create_index([("user_id", ASCENDING), ("created", DESCENDING)])
        d.jobs.create_index("state")
        d.schedule.create_index("at")
        d.oauth_states.create_index("expires", expireAfterSeconds=0)  # unfinished Connect clicks vanish
        d.watches.create_index([("user_id", ASCENDING), ("channel_id", ASCENDING)], unique=True)
        d.jobs.create_index("auto.watch")
        d.posts.create_index([("user_id", ASCENDING), ("created", DESCENDING)])
        d.schedule.create_index("post", sparse=True)
        d.sessions.create_index([("user_id", ASCENDING), ("sid", ASCENDING)])
        d.login_attempts.create_index("at", expireAfterSeconds=3600)   # throttle window data cleans itself
        d.login_attempts.create_index("key")
        d.password_resets.create_index("token", unique=True)
        d.password_resets.create_index("expires", expireAfterSeconds=0)  # expired reset links vanish
    except PyMongoError as e:
        raise DatabaseError(f"Can't reach MongoDB: {e}") from None
    return d


def public(doc: dict | None, drop: tuple[str, ...] = ()) -> dict | None:
    """Mongo document → API JSON (``_id`` → ``id``, internal fields removed)."""
    if doc is None:
        return None
    out = {k: v for k, v in doc.items() if k != "_id" and k not in drop}
    out["id"] = doc["_id"]
    return out
