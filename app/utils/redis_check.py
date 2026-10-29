import redis
from app.core.config import get_settings

def is_redis_online() -> bool:
    settings = get_settings()
    try:
        # Connect to redis and ping
        r = redis.Redis.from_url(settings.redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
        r.ping()
        return True
    except Exception:
        return False
