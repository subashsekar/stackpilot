# FAQ

End-user FAQ lives in the main README so there is a single source of truth:

- [FAQ](README.md#faq)
- [Troubleshooting](README.md#troubleshooting)
- [Known Limitations](README.md#known-limitations)
- [External Dependencies](README.md#external-dependencies)
- [Health Checks](README.md#health-checks)
- [Issue Tracking](README.md#issue-tracking)
- [CLI: stop](README.md#stackpilot-stop)
- [CLI: run --force](README.md#stackpilot-run)

Topics covered there include:

- `stackpilot stop` and stale session recovery (`run --force`)
- Flask automatic port reassignment (`flask run --port`)
- MongoDB / RabbitMQ external dependency detection
- NestJS HTTP `/health` (Terminus / controllers) with TCP fallback
- Ctrl+C shutdown, hot reload, Stackfile trust model, and the Issue Tracker
