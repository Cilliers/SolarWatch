# SolarWatch

One dashboard for residential hybrid solar systems spread across Victron VRM, Sunsynk Connect,
GoodWe SEMS+, SMA Sunny Portal, Deye Cloud and LuxPower. It polls each platform on a schedule,
stores the readings in a local SQLite database, and shows every site in a single list sorted by what needs attention.

- **Fleet view**: live solar, home load, grid and battery for every site, with faults,
  silent loggers and low batteries pulled to the top.
- **Site view**: live power-flow diagram, today's energy, and 24 h / 7 day charts.
- **Account health**: per-account status, last data, next poll and a "Poll now" button.
- **Safe polling**: per-account backoff on errors, and polling **stops** on a refused login
  (repeated bad logins can lock a Sunsynk or SEMS+ account) until you press *Try sign-in again*.
- **Self-hosted**: one Python process, no external database, optional HTTP basic auth.

## Quick start

Try it with simulated sites first (no credentials needed):

```bash
pip install -r requirements.txt
python -m solarwatch --config config.demo.yaml
# open http://127.0.0.1:8080
```

Then with your real accounts:

```bash
cp config.example.yaml config.yaml     # edit: accounts, alert thresholds
cp .env.example .env                   # put passwords and tokens here
python -m solarwatch
```

Python 3.10+ is required.

## Install with Docker

Works on any machine with Docker: a home server, a NAS (Synology, Unraid, TrueNAS), a
Raspberry Pi 4/5 or a cloud VM. You need [Docker](https://docs.docker.com/get-docker/) with
the Compose plugin (`docker compose version` should print a version).

A ready-made image is published at `ghcr.io/OWNER/REPO` for both regular PCs/servers and
ARM boards, so there is nothing to build.

1. **Get the two files** into a new folder:
   ```bash
   mkdir solarwatch && cd solarwatch
   curl -fsSLO https://raw.githubusercontent.com/OWNER/REPO/main/docker-compose.yml
   curl -fsSL  https://raw.githubusercontent.com/OWNER/REPO/main/.env.example -o .env
   ```
2. **Add your logins:** fill in `.env`: a dashboard password (`SOLARWATCH_PASSWORD`) and the
   logins for the platforms you use. Leave the rest blank.
3. **Start it:** `docker compose up -d`. Docker downloads the image on first start.
4. **Choose your platforms:** the first start creates `data/config.yaml`. Open it, delete the
   accounts you don't use, then `docker compose restart`.
5. **Open the dashboard** at `http://<this machine's IP>:8080` and sign in as `admin` with the
   password from `.env`.

Everything SolarWatch keeps (config, database, saved tokens) is in the `data` folder, so back
that folder up and you have backed up SolarWatch.

| Task | Command |
|---|---|
| See the logs | `docker compose logs -f` |
| Restart after editing `config.yaml` or `.env` | `docker compose restart` (`.env` changes need `docker compose up -d`) |
| Update to the latest version | `docker compose pull && docker compose up -d` (your `data` and `.env` stay) |
| Stay on a specific version | change `:latest` in `docker-compose.yml` to a release, e.g. `:1.0.0` |
| Build from source instead | in a full copy of the code: `docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build` |
| Stop | `docker compose down` |
| Try the demo instead | `docker compose run --rm -p 8080:8080 -e SOLARWATCH_CONFIG=/app/config.demo.yaml solarwatch` |

**Settings in `docker-compose.yml`:**
- **Port:** change `"8080:8080"` to e.g. `"9090:8080"` to use port 9090 on your machine.
- **Time zone:** `TZ` decides what "today" means for daily totals.
- **File owner:** `PUID`/`PGID` set which user owns the files in `data`. The default 1000 is
  the first user on most Linux machines; run `id -u` and `id -g` to check yours. On Synology,
  use the IDs of your DSM user.

The container runs the app as that unprivileged user, not as root, and checks its own health
every minute (`docker ps` shows *healthy*).

### Publishing new versions (maintainers)

GitHub Actions (`.github/workflows/docker-publish.yml`) runs the tests, then builds and
publishes the image:

- **every push to `main`** updates `:latest`;
- **a version tag** publishes a release: `git tag v1.0.0 && git push origin v1.0.0` gives
  `:1.0.0`, `:1.0` and `:1`;
- **pull requests** are built and tested but not published.

If the repository is private the image is private too, and each machine must sign in first:
`docker login ghcr.io` with a GitHub token that has `read:packages`. To let anyone pull it,
open the package on GitHub (your profile → Packages → solarwatch → Package settings) and set
its visibility to public.

## Connecting each platform

| Platform | What you need | Notes |
|---|---|---|
| **Victron VRM** | A personal access token: VRM → Preferences → Integrations → Access tokens | Official, documented API. An installer account's token sees every installation shared with it. Username/password also works. |
| **Sunsynk Connect** | The email/password used in the Sunsynk Connect app | Unofficial API. MFA-enabled accounts can't log in through the API. For Deye/Inteless accounts set `base_url: https://pv.inteless.com`. |
| **GoodWe SEMS+** (`goodwe_plus`) | SEMS+ email/password | Unofficial gateway API of semsplus.goodwe.com. Owner and installer accounts both work; stations are auto-discovered, or list them in `station_ids`. Today's yield comes from the station list. The old SEMS portal is retired and no longer supported; a config still using `platform: goodwe` gets a message to switch. |
| **SMA Sunny Portal** (`sma`) | `mode: portal`: your ennexOS Sunny Portal login | Unofficial: signs in like the web app. **Solar power and today's yield only**; battery, grid and load aren't exposed in a form we could confirm. Two-step sign-in accounts can't use it. |
| | `mode: monitoring_api`: SMA client ID/secret + owner's refresh token | SMA's official Monitoring API: full PV, load, battery, grid and SoC. Needs an SMA API contract (developer.sma.de, billed per system) and the plant owner's consent. |
| **Deye Cloud** (`deye`) | App ID + App Secret from developer.deyecloud.com, plus your Deye Cloud email/password | Official developer API. Creating the app is free and self-service. EU data centre by default; set `base_url` for US. |
| **LuxPower** (`luxpower`) | LuxPower web portal username/password | Unofficial web API. Set `base_url` to your region's server: `af.luxpowertek.com` for Southern Africa. Inverters in one plant (parallel systems) are summed. Also works for EG4. |

You can add several accounts per platform (e.g. one per client or installer login); give each
a unique `id`. Set `enabled: false` to pause one.

### About the unofficial APIs

Only Victron, Deye and SMA (Monitoring API) offer official APIs. For Sunsynk, GoodWe SEMS+,
SMA portal mode and LuxPower, SolarWatch uses the same calls their own apps
make, as documented by the open-source community. They can change without notice:

- **Sunsynk** moved to an RSA-encrypted, signed login in 2025-26. SolarWatch implements the
  current flow (`/anonymous/publicKey` → `/oauth/token/new`). If sign-in starts failing with
  *"Invalid login interface request"*, the signing scheme has changed again; the fix lives in
  `solarwatch/connectors/sunsynk.py` (`public_key_sign`, `token_sign`).
- **GoodWe SEMS+** signs every request (`x-signature`). If calls start failing with signature or
  token errors after working before, GoodWe has changed the scheme; see `connectors/goodwe_plus.py`.
- **SMA portal mode** depends on the ennexOS web app's own sign-in page and API. SMA's
  official route is the Monitoring API (`mode: monitoring_api`), which is stable but contract-only.
- **LuxPower** reports a dropped-off inverter with a `lost` flag rather than a stale timestamp;
  SolarWatch shows such a plant as offline. Its `consumptionPower` field reads 0 on some models,
  so home load is worked out from the energy balance instead.
- **Deye**: the daily "consumption" figure Deye returns is its own residual; SolarWatch computes
  today's home use from generation, import, export and battery energy instead.

Every platform's last raw payload is kept and shown under *Platform data* on the site page,
which makes it quick to see what changed when a field moves.

## How it works

```
config.yaml ─▶ Poller ──(one task per account, every poll_interval)──▶ Connector.poll()
                  │                                                     victron / sunsynk / goodwe_plus / sma / deye / luxpower
                  ▼
             SQLite (sites, readings, latest raw)  ◀── Web API ◀── Dashboard (static HTML/JS)
```

Each connector turns its platform's answers into one shape (`solarwatch/models.py`):

| Field | Meaning |
|---|---|
| `pv_w`, `load_w` | Solar and home load now, W |
| `battery_w` | W, **+ charging / − discharging** |
| `grid_w` | W, **+ importing / − exporting** |
| `soc` | Battery state of charge, % |
| `pv/load/import/export_kwh_today` | Today's energy, kWh |
| `reported_at` | When the system itself last uploaded |

A site is flagged **offline** when the system hasn't uploaded for `stale_minutes` (the cloud
may still answer with zeros, which SolarWatch dims rather than shows as real), **fault** when the
platform reports one, **warning** below `low_soc`, and **read error** when its platform call failed.

### Adding another platform

Write a class in `solarwatch/connectors/` that subclasses `Connector`, implements `poll()`
returning a list of `Snapshot(SiteInfo, Reading)`, raises `AuthError` for refused credentials and
`TransientError` for anything worth retrying. Register it in `connectors/__init__.py`.

## API

| Endpoint | |
|---|---|
| `GET /api/overview` | Fleet summary, every site with its latest reading, alerts, account states |
| `GET /api/sites/{id}` | One site |
| `GET /api/sites/{id}/history?hours=24` | Readings for charts (down-sampled to ~600 points) |
| `GET /api/sites/{id}/raw` | Last raw platform payload |
| `POST /api/poll` / `POST /api/poll/{account_id}` | Poll now (also clears a sign-in pause) |
| `GET /healthz` | Liveness, no auth |

## Tests

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests
```

The connector tests run against fake servers, including one that checks the Sunsynk login
signatures and decrypts the RSA-encrypted password, so the login flow is verified end to end
without a real account.

## Security notes

- Turn on `auth` in the config whenever the dashboard is reachable from a network; it shows
  customer addresses and live data. Put it behind HTTPS (e.g. a reverse proxy) if exposed beyond
  your LAN.
- Credentials are read from the environment / `.env`, never written to the database.
