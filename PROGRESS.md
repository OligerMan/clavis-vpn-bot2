# Clavis VPN Bot v2 - Progress

## Version 0.1.0 (In Development)

### Completed
- [x] Project structure setup
- [x] Documentation files created (CLAUDE.md, WIKI.md, PROGRESS.md)
- [x] Folder DOCS.md files created
- [x] Database schema designed (8 tables)
- [x] Subscription URL system designed (v2raytun compatible)
- [x] Routing lists structure defined
- [x] SQLite database implementation (SQLAlchemy models)
- [x] Database tests created

### In Progress
- [ ] 3x-ui API client

### Planned
- [ ] Subscription URL endpoint (Flask/FastAPI)
- [ ] Bot handlers migration
- [ ] Payment integration
- [ ] v1 data migration tool
- [ ] Traffic logging scheduler
- [ ] Abuse detection system
- [ ] Routing lists population (ru_bypass, ads_block, ru_blocked_proxy)

---

## Database Tables

| Table | Status | Description |
|-------|--------|-------------|
| Users | Implemented | Telegram users |
| Subscriptions | Implemented | Time-based access with token |
| Keys | Implemented | VPN keys (multiple per subscription) |
| Servers | Implemented | VPN server configs |
| UserConfigs | Implemented | Per-user routing preferences |
| RoutingLists | Implemented | Admin-managed domain lists |
| TrafficLogs | Implemented | Periodic traffic snapshots |
| Transactions | Implemented | Payment history |

---

## Changelog

### [Unreleased]
- Initial project setup
- Documentation structure
- Complete database schema design
- Subscription URL system with v2raytun headers support
- Renewal reminders via announce headers
- Traffic logging for abuse detection (no enforcement)
