# /services - Background Services

## Purpose
Background tasks, scheduled jobs, and data processing.

## Files
| File | Description |
|------|-------------|
| scheduler.py | APScheduler jobs (renewal reminders, cleanup) |
| traffic.py | Traffic statistics collection from VPN servers |
| migration.py | v1 CSV to v2 SQLite data migration tool |

## Dependencies
- Internal: database, vpn
- External: apscheduler
