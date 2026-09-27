# launchd jobs

The phone line runs on one Mac under launchd. These are the copies of the three LaunchAgents; the
live ones are in `~/Library/LaunchAgents/`. The paths inside are absolute to this checkout, so edit
`WorkingDirectory` and the log paths if the repo lives somewhere else.

| Job | What it runs | When |
|---|---|---|
| `com.shatsky.summit-air-agent` | `caffeinate -s uv run python src/agent.py start`, the worker | Always; `KeepAlive` restarts it if it exits |
| `com.shatsky.summit-air-watchdog` | `scripts/watchdog.py`: a health-check dispatch and an AC-power check, paging on a change | Every 5 minutes |
| `com.shatsky.summit-air-usage` | `scripts/usage.py --alert`: provider balances, paging below a floor | Hourly |

Load one (and start it now):

```sh
cp ops/launchd/com.shatsky.summit-air-watchdog.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.shatsky.summit-air-watchdog.plist
launchctl print gui/$(id -u)/com.shatsky.summit-air-watchdog | grep -E "state|last exit"
```

Unload with `launchctl bootout gui/$(id -u)/<label>`.

Deploy a new worker build (never while a `call-*` room is open, and never run a second worker or
`dev` mode alongside it):

```sh
set -a; source .env.local; set +a; lk room list   # must show no room named call-*
launchctl kickstart -k gui/$(id -u)/com.shatsky.summit-air-agent
tail -f logs/worker.log                            # wait for "registered worker"
```

Logs: `logs/worker.log`, `logs/watchdog.log`, `logs/usage-monitor.log`. The watchdog's last state is
`data/watchdog-state.json`.
