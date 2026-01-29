# /routing - Domain Routing Rules

## Purpose
Domain lists for traffic routing decisions. Lists are loaded into RoutingLists table and served via subscription headers.

## Files
| File | Description |
|------|-------------|
| lists/ru_bypass.txt | Russian domains for direct connection |
| lists/ads_block.txt | Advertising/tracking domains to block |
| lists/ru_blocked_proxy.txt | Sites blocked in Russia (force proxy) |

## List Format
One domain per line, supports wildcards:
```
# Comment line
domain.com
*.domain.com
regexp:.*\.example\.com$
```

## Dependencies
- Internal: database/models.py (RoutingLists table)
- External: None
