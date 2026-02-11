"""Thread-safe TTL cache for subscription responses."""

import threading
import time
from collections import OrderedDict
from typing import Optional, Dict, Any


class TTLCache:
    """Thread-safe LRU cache with TTL expiration."""

    def __init__(self, max_size: int = 1000, ttl_seconds: int = 300):
        """Initialize cache.

        Args:
            max_size: Maximum number of entries
            ttl_seconds: Time-to-live in seconds
        """
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found/expired
        """
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None

            value, expiry_time = self._cache[key]

            # Check if expired
            if time.time() > expiry_time:
                del self._cache[key]
                self._misses += 1
                return None

            # Move to end (LRU)
            self._cache.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: Any) -> None:
        """Store value in cache with TTL.

        Args:
            key: Cache key
            value: Value to cache
        """
        with self._lock:
            expiry_time = time.time() + self.ttl_seconds

            # If key exists, update and move to end
            if key in self._cache:
                self._cache[key] = (value, expiry_time)
                self._cache.move_to_end(key)
                return

            # Evict oldest if at capacity
            if len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)

            # Add new entry
            self._cache[key] = (value, expiry_time)

    def delete(self, key: str) -> bool:
        """Delete specific key from cache.

        Args:
            key: Cache key to delete

        Returns:
            True if key was found and deleted, False otherwise
        """
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def clear(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> Dict[str, Any]:
        """Get cache statistics.

        Returns:
            Dictionary with cache stats
        """
        with self._lock:
            current_time = time.time()
            expired_count = sum(
                1 for _, expiry in self._cache.values()
                if current_time > expiry
            )

            total_requests = self._hits + self._misses
            hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0

            return {
                "total_entries": len(self._cache),
                "expired_entries": expired_count,
                "active_entries": len(self._cache) - expired_count,
                "max_size": self.max_size,
                "ttl_seconds": self.ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate_percent": round(hit_rate, 2),
            }


# Global cache instance (will be initialized with config values)
_subscription_cache = None


def _get_cache() -> TTLCache:
    """Get or create global cache instance.

    Returns:
        Global TTLCache instance
    """
    global _subscription_cache

    if _subscription_cache is None:
        # Import here to avoid circular dependency
        from config.settings import SUBSCRIPTION_CACHE_SIZE, SUBSCRIPTION_CACHE_TTL

        _subscription_cache = TTLCache(
            max_size=SUBSCRIPTION_CACHE_SIZE,
            ttl_seconds=SUBSCRIPTION_CACHE_TTL
        )

    return _subscription_cache


def get_cached_subscription(token: str) -> Optional[str]:
    """Get cached subscription response.

    Args:
        token: Subscription token

    Returns:
        Cached base64 response or None
    """
    cache = _get_cache()
    return cache.get(token)


def cache_subscription_response(token: str, response: str) -> None:
    """Cache subscription response.

    Args:
        token: Subscription token
        response: Base64-encoded response
    """
    cache = _get_cache()
    cache.set(token, response)


def invalidate_subscription_cache(token: str) -> bool:
    """Invalidate cache for specific subscription token.

    Args:
        token: Subscription token to invalidate

    Returns:
        True if cache entry was found and deleted
    """
    cache = _get_cache()
    return cache.delete(token)


def clear_cache() -> None:
    """Clear all cached subscriptions."""
    cache = _get_cache()
    cache.clear()


def get_cache_stats() -> Dict[str, Any]:
    """Get cache statistics.

    Returns:
        Dictionary with cache statistics
    """
    cache = _get_cache()
    return cache.stats()
