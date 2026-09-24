# Part 18: production and server operations (short answers)

## Frappe Cloud

- **Application deployment.** The app lives in a Git repository connected to a *bench group*. Pushing to the tracked branch, or clicking *Deploy*, builds a new image (apps, requirements, assets). *Update sites* then moves each site onto it and runs `migrate`. You can pin a site to an older deploy to roll back code.
- **Backups.** Automatic daily database backups plus on-demand ones (with files), downloadable from the dashboard. An automatic backup is taken before each site update. Restore uses the dashboard, or `bench restore` on a self-hosted copy.
- **Logs.** The site dashboard shows web/request logs, the scheduler and worker logs, and the in-app **Error Log** doctype. Bench-level logs are under *Bench → Logs*.
- **Scheduler and workers.** Managed by the platform. The scheduler can be enabled or paused per site. Background workers are part of the plan, and the *Background Jobs* and *RQ Job* pages show the queues.
- **Site configuration.** `site_config.json` keys are edited in *Site → Configuration*, never in the repo: secrets, `developer_mode`, `maintenance_mode`, limits. App settings (for example Reno Logistics Settings) stay in the database.

## Self-hosted ERPNext: what each part does

| Component | Role |
|---|---|
| **Nginx** | TLS termination; serves `/assets` and public files directly; proxies HTTP to Gunicorn and `/socket.io` to the Node socket.io server; client body size and timeouts |
| **Gunicorn** | WSGI server running Frappe; N sync workers (about 2 × CPU + 1); `--timeout` kills requests that run too long |
| **Supervisor** (or systemd) | Keeps Gunicorn, workers, scheduler, socket.io and Redis running; restarts them on crash; `bench restart` goes through it |
| **Redis** | `redis_cache` for the document/meta cache and sessions; `redis_queue` for the RQ job queues (`short`, `default`, `long`); realtime pub/sub for socket.io |
| **MariaDB** | Primary data store (InnoDB); one database per site |
| **Workers** | `bench worker --queue …` processes that run RQ jobs: emails, reposts, this app's Delivery Note and logistics jobs |
| **Scheduler** | `bench schedule` enqueues `scheduler_events` (hourly, daily, cron) for every site whose scheduler is enabled |

## Troubleshooting

| Symptom | Check | Typical fix |
|---|---|---|
| **502 Bad Gateway** | Nginx `error.log` ("connect() failed" / "upstream prematurely closed"); `supervisorctl status`; gunicorn/`web.error.log` for crashes or `WORKER TIMEOUT` | Start or restart the web process; fix the import error that stops Gunicorn booting; for timeouts, move slow work to a job (see ARCHITECTURE §7) and only then consider raising `--timeout` |
| **Worker queue backlog** | `bench --site <s> show-pending-jobs`; *RQ Job* list; `bench doctor`; `logs/worker*.log`; are workers running and on which queues? | Add workers, or dedicated workers for `long`; find the job flooding the queue; `purge-jobs` only for jobs that are safe to drop |
| **Scheduler not running** | `bench doctor`; `bench --site <s> scheduler status`; `pause_scheduler` in site config; *Scheduled Job Log*; is the `schedule` process up? | `bench --site <s> enable-scheduler` / `scheduler resume`; start the process; fix the failing job |
| **High CPU** | `top`/`htop`: gunicorn, workers or mysqld? MariaDB `SHOW PROCESSLIST`; the slow query log; Frappe's *Recorder* for a slow request | Fix the query or index; cap worker concurrency; move a heavy report to a prepared report or job; scale |
| **Slow MariaDB queries** | Slow query log (`long_query_time`); `EXPLAIN`/`ANALYZE`; `information_schema.innodb_trx` and `innodb_lock_waits` for **lock waits** (a slow save is often a *waiting* one) | Add or adjust an index (see PERFORMANCE.md); shorten transactions; batch big updates; tune `innodb_buffer_pool_size` |
| **Disk full** | `df -h`, `du -sh` on `sites/*/private/backups`, `logs/`, MariaDB binlogs and tmp, `/var/log` | Rotate and prune old backups and logs; purge binlogs (`expire_logs_days`); move backups off-server; grow the volume |
| **Failed migration** | The traceback printed by `bench migrate`; `logs/<site>/frappe.log`; the **Patch Log** shows how far it got | Fix and **re-run** migrate (patches are idempotent). If data is wrong, restore the pre-deploy backup and redeploy the previous tag. Keep the site in maintenance mode until it is consistent |
