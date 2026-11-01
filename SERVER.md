````markdown
# 🏛 Architectural Documentation: Venator Server (Termux Edition)

**Venator** is an autonomous, low-power Telegram vacancy and freelance order radar with an integrated LLM anti-scam filter and web control center. Built on **Flask + Telethon (MTProto) + Gunicorn (gthread) + SQLite (WAL)**.

This document details the Termux server architecture, daemonization via `Runit` (`termux-services`), traffic tunneling via Cloudflare Zero Trust (`venator.andatra.space`), and administrative workflows.

---

## 1. Traffic & Event Flow Architecture

The application combines a background event-driven MTProto listener (`Telethon`), an outbound Telegram Bot API notifier, a password-protected web dashboard (`/`), and a local SQLite WAL database. All external web traffic is routed through the Cloudflare Zero Trust tunnel (`termux-android`) to the local IPv4 socket `127.0.0.1:5006`.

```text
[ Telegram MTProto Cloud ]                  [ Admin Browser / Mobile ]
            │                                            │
            ▼ (epoll Push: NewMessage / UpdateChannel)   ▼ (HTTPS: venator.andatra.space)
[ Smartphone (Android + Termux) ]             [ Cloudflare Edge: Zero Trust Tunnel ]
            │                                            │
            │                                            ▼ (termux-android -> 127.0.0.1:5006)
            └──────────────► [ Gunicorn (Runit Daemon, -w 1 --threads 20, Port 5006) ]
                                       │
                                       ├──► [ Background Daemon Thread: Telethon Worker ]
                                       │      ├─► Stage 1: RAM Regex Pre-Filter (0 ms)
                                       │      ├─► Stage 2: SHA-256 Ring Buffer Dedup (300 hashes)
                                       │      ├─► Stage 3: OpenAI-Compatible LLM JSON Analyzer
                                       │      └─► Outbound HTTPS Push -> Telegram Bot API
                                       │
                                       ├──► [ Flask Web App (SSR + JSON REST API + AI Sandbox) ]
                                       └──► [ SQLite: database.db (WAL / synchronous=NORMAL) ]
```
````

---

## 2. Server Component Breakdown

### A. Database Layer (SQLite WAL + RAM Cache)

- **Database File:** `/data/data/com.termux/files/home/Venator/database.db`
- **Rationale:** Native `sqlite3` with zero ORM overhead keeps memory consumption minimal (~30–40 MB total for both Flask and Telethon).
- **Concurrency & Durability:**
  - Configured with `PRAGMA journal_mode=WAL;`, `PRAGMA synchronous=NORMAL;`, and `timeout=15.0` to allow simultaneous writes from the Telethon worker and reads/updates from the Web Dashboard without `database is locked` errors.
  - **Auto-Pruning:** The `orders` table automatically prunes records older than the most recent 300 evaluations on insert, preventing storage bloat on mobile flash memory.

### B. WSGI Server & Single-Process Worker Model (Gunicorn + gthread)

- **Execution Mode:** Single worker process with 20 threads (`-w 1 --threads 20`).
- **Why `-w 1` is critical:** Because `Telethon` holds an exclusive lock on `venator_listener.session` and shares the in-memory settings cache (`_settings_cache`) with Flask, running a single multi-threaded worker guarantees zero session lock conflicts and instant `0 ms` hot-reloading when settings are saved in the UI.
- **Port Allocation:** `127.0.0.1:5006` (isolated from `Eventum` on `5001`, `AmadeusX` on `5002`, `Chan` on `5003`, `Presentum` on `5004`, and `Observatum` on `5005`).

### C. Daemonization (Runit / termux-services)

- **Service Directory:** `$PREFIX/var/service/venator-backend/run`
- **Run Script:**
  ```bash
  #!/data/data/com.termux/files/usr/bin/sh
  exec 2>&1
  cd /data/data/com.termux/files/home/Venator
  exec /data/data/com.termux/files/home/Venator/venv/bin/gunicorn -w 1 --threads 20 -b 127.0.0.1:5006 app:app
  ```

### D. Networking & Cloudflare Tunnel

- **Tunnel Name:** `termux-android`
- **Public Route:** `venator.andatra.space` ➔ `http://127.0.0.1:5006`
- ⚠️ **Critical Android IPv6 Nuance:** In the Cloudflare Zero Trust Dashboard (`Networks ➔ Tunnels ➔ termux-android ➔ Public Hostname`), the service target **must** be explicitly set to `http://127.0.0.1:5006` (and **not** `http://localhost:5006`) to avoid 5-second `::1` DNS resolution timeouts in Android's Bionic libc.

### E. Power Efficiency Profile (Zero-Drain Architecture)

To preserve smartphone battery life on Termux 24/7, four hardware-friendly optimizations are enforced:

1. **Pure Socket Push (`epoll`):** Telethon waits passively on the MTProto socket. Zero periodic scraping timers or busy loops exist.
2. **RAM Settings & Regex Cache:** Incoming messages are checked against pre-compiled regexes in RAM without touching SQLite or flash storage.
3. **Cross-Post SHA-256 Deduplication:** Reposted vacancies across multiple regional channels are deduplicated in memory before waking the network radio for an LLM API call.
4. **Client Sleep Mode (Page Visibility API):** When the administrator minimizes the browser or switches tabs (`document.hidden === true`), the dashboard halts all `/api/state` requests (`SYNC: SLEEPING (TAB HIDDEN)`), dropping dashboard CPU usage on the phone to `0%`.

---

## 3. Directory Structure on Termux

```text
~/Venator/
├── .env                        # Local secrets (ADMIN_PASSWORD, TG keys, LLM key)
├── .env.example                # Template for environment variables
├── venator_listener.session    # Authorized Telethon MTProto session file
├── database.db                 # SQLite WAL database (auto-created)
├── app.py                      # Flask WSGI server & background thread launcher
├── database.py                 # SQLite WAL schema, CRUD & RAM cache
├── monitor.py                  # Telethon listener, ban detector & LLM evaluator
├── requirements.txt            # Pure-Python package list
├── README.md                   # Project documentation
├── SERVER.md                   # Termux server architecture guide
├── venv/                       # Python virtual environment
└── templates/
    ├── login.html              # Authentication screen
    └── dashboard.html          # AI control panel & order feed
```

---

## 4. Step-by-Step Deployment on Termux from Scratch

```bash
# 1. Clone the repository
cd ~
git clone https://github.com/Qoziwe/Venator.git
cd ~/Venator

# 2. Create virtual environment and install dependencies
python -m venv venv
./venv/bin/pip install -r requirements.txt

# 3. Configure environment variables
cp .env.example .env
nano .env

# 4. Perform one-time interactive Telegram login for the listener account
./venv/bin/python monitor.py --auth

# 5. Create and enable the Runit service (termux-services)
mkdir -p $PREFIX/var/service/venator-backend/log
ln -sf $PREFIX/share/termux-services/svlogger $PREFIX/var/service/venator-backend/log/run

cat << 'EOF' > $PREFIX/var/service/venator-backend/run
#!/data/data/com.termux/files/usr/bin/sh
exec 2>&1
cd /data/data/com.termux/files/home/Venator
exec /data/data/com.termux/files/home/Venator/venv/bin/gunicorn -w 1 --threads 20 -b 127.0.0.1:5006 app:app
EOF

chmod +x $PREFIX/var/service/venator-backend/run
sv-enable venator-backend
sv up venator-backend
```

Next, in **Cloudflare Zero Trust ➔ Networks ➔ Tunnels ➔ `termux-android` ➔ Public Hostname**, click **+ Add a public hostname**:

- **Subdomain:** `venator`
- **Domain:** `andatra.space` (`venator.andatra.space`)
- **Service Type:** `HTTP`
- **URL:** `127.0.0.1:5006`

---

## 5. Administrator Cheat Sheet

### Service Management in Termux:

- **Check service status:**
  ```bash
  sv status venator-backend
  ```
- **Restart backend (after `git pull` or `.env` changes):**
  ```bash
  sv restart venator-backend
  ```
- **Stop / Start service:**
  ```bash
  sv down venator-backend
  sv up venator-backend
  ```
- **Follow live logs:**
  ```bash
  tail -f $PREFIX/var/log/sv/venator-backend/current
  ```

### Updating Codebase from GitHub:

```bash
cd ~/Venator
git pull
sv restart venator-backend
```

### Database Backup & Reset:

- **Backup SQLite database:**
  ```bash
  cp ~/Venator/database.db ~/venator_backup_$(date +%F).db
  ```
- **Full database reset (clean slate):**
  ```bash
  rm -f ~/Venator/database.db ~/Venator/database.db-wal ~/Venator/database.db-shm
  sv restart venator-backend
  ```
