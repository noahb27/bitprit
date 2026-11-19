# Railway cloud worker

This is the same paper-only recorder and frozen-model inference worker as the
local CLI. It has no real-order path, paid LLM calls, or automatic training or
promotion. The normal $5 inclusive entry cap remains in force. With a new
database it records data and WAIT / UNTRAINED decisions.

## Deploy

1. Confirm the account has free/trial credits available. Do not upgrade or add
   paid billing for a free test. Trial limits and network eligibility are
   account-specific; free credits do not guarantee a full month of uptime.
2. Create one private Railway project and one empty worker service. No public
   domain or port is required.
3. Attach a persistent volume at `/data` **before** connecting the source. Start
   with 500 MB. Configure exactly one replica and at most 0.5 GB memory and
   1 vCPU. Disable PR environments to avoid duplicate recording and costs.
4. Connect this branch/commit from `noahb27/bitprit`. Railway uses the included
   Dockerfile and `railway.json`. The entry point refuses to run if `/data` is
   not a mount point; the SQLite database is `/data/quarter-hour.sqlite3`.
5. Wait for deployment SUCCESS, then inspect runtime logs and source status.
   Cloud logs include a compact health summary on startup and every 12 samples
   (about one minute), including source readiness, record counts and quality.
   HTTP 403, network restrictions, missing orderbooks, or missing targets must
   be reported as unavailable data. A running process alone does not establish
   live-feed readiness. Do not bypass provider restrictions.

Keep this deployment pinned to a reviewed commit while other development
continues. Do not run a second worker against this database. This container
includes only the runtime package, with no local databases, secrets, research
dependencies, or sample performance.

## Operate

Using Railway's service shell, commands run from `/app`:

```sh
python -m quarter_hour --db /data/quarter-hour.sqlite3 status
python -m quarter_hour --db /data/quarter-hour.sqlite3 decisions --limit 5
python -m quarter_hour --db /data/quarter-hour.sqlite3 report
python -m quarter_hour --db /data/quarter-hour.sqlite3 backup /data/backup-UNIQUE.sqlite3
```

Download backups off the volume; a backup on the same volume does not protect
against volume loss. Check volume usage and export regularly: the recorder saves
raw API responses and the volume will eventually fill. No data is automatically
deleted. Do not rely on trial storage retention after credits expire.

SIGTERM is translated to the worker's existing interrupt handler so shutdown
records a stop; a forced kill still leaves an explicit unclean-restart/gap
record next time. SQLite transactions and the volume preserve committed data
across ordinary restarts and redeployments. Removing the volume destroys it.

Monitor credit balance, runtime memory/CPU, errors, source timestamps, and disk
usage in Railway. Free testing must stop when its credit allowance ends rather
than automatically upgrading. No continuous deployment, live connectivity,
training, or profitable performance is claimed until separately verified.
