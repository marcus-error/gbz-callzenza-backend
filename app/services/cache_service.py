import time
from typing import Any, Dict


class CacheService:
    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}

    def set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        """Stores a value in the memory cache with an expiration timestamp."""
        self._cache[key] = {
            "value": value,
            "expires_at": time.time() + ttl_seconds
        }
        # Run minor garbage collection
        self.cleanup()

    def get(self, key: str) -> Any | None:
        """Retrieves a cached value if it exists and hasn't expired yet."""
        item = self._cache.get(key)
        if not item:
            return None

        if time.time() > item["expires_at"]:
            self._cache.pop(key, None)
            return None

        return item["value"]

    def delete(self, key: str) -> None:
        """Removes a value from the cache explicitly."""
        self._cache.pop(key, None)

    def cleanup(self) -> None:
        """Evicts all expired keys to prevent memory leaks."""
        now = time.time()
        expired_keys = [k for k, v in self._cache.items() if now > v["expires_at"]]
        for k in expired_keys:
            self._cache.pop(k, None)


# Global singleton instance for app-wide import
cache_service = CacheService()
