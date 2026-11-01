# 🏛 Architectural Documentation: Venator Server (Termux Edition)

**Venator** is an autonomous, ultra-low-power Telegram vacancy and freelance order radar with an integrated LLM anti-scam filter and web control center. Built on **Flask + Telethon (MTProto) + Gunicorn (gthread) + SQLite (WAL)**.

This document details the server architecture on Android (Termux), daemonization via `Runit` (`termux-services`), traffic tunneling via Cloudflare Zero Trust (`venator.andatra.space`), event-driven folder synchronization, error monitoring safeguards, and administrative workflows.

---

## 1. Traffic & Event Flow Architecture

The system operates across two decoupled network paths:
1. **Inbound Telegram Events:** Passive socket-level MTProto connection via `Telethon` receiving real-time channel updates from designated chat folders (`UpdateDialogFilter`).
2. **Outbound / Web Management:** Inbound HTTP traffic routed through Cloudflare Zero Trust tunnel (`termux-android`) to Gunicorn at `127.0.0.1:5006`, and outbound HTTPS pushes dispatching alerts to the Telegram Bot API (`api.telegram.org`).

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
                                       │      ├─► Dynamic Folder Peer Filter (Target Folder: Фриланс)
                                       │      ├─► SHA-256 Ring Buffer Dedup (300 hashes)
                                       │      ├─► OpenAI-Compatible LLM JSON Analyzer (Groq / Gemini / Ollama)
                                       │      ├─► Anti-Flood Error Notifier (HTTP 429 / API Failure Alerts)
                                       │      └─► Outbound HTTPS Push -> Telegram Bot API (@LekimZlavdinov)
                                       │
                                       ├──► [ Flask Web App (SSR + JSON REST API + AI Sandbox + QR Auth) ]
                                       └──► [ SQLite: database.db (WAL / synchronous=NORMAL) ]
```

---

## 2. Server Component Breakdown

### A. Database Layer (SQLite WAL + RAM Cache)
* **Database File:** `/data/data/com.termux/files/home/Venator/database.db`
* **Zero-Drain RAM Synchronization:**
  - Read-heavy settings (model IDs, API URLs, prompt rules, target folder names, and compiled stop-word regexes) are cached in memory. Incoming messages query RAM at `0 ms` without triggering physical flash storage I/O.
  - Updates from the Web Dashboard persist to SQLite and instantly hot-reload the RAM cache without requiring service restarts.
* **Concurrency & Durability:**
  - Configured with `PRAGMA journal_mode=WAL;`, `PRAGMA synchronous=NORMAL;`, and `timeout=15.0` to eliminate `database is locked` contentions between the Telethon background thread and concurrent dashboard sessions.
  - **Auto-Pruning:** The `orders` table automatically purges records older than the 300 most recent evaluations on every insert, safeguarding mobile internal storage from uncontrolled growth.

### B. WSGI Server & Single-Process Worker Model (Gunicorn + gthread)
* **Execution Mode:** Multi-threaded single worker (`-w 1 --threads 20`).
* **Why `-w 1` is Required:** Telethon locks `venator_listener.session` exclusively and communicates state via shared Python memory. Multiple worker processes would cause database/session lock contentions. A single worker handling 20 concurrent threads ensures smooth dashboard performance alongside the background listener.
* **Port Allocation:** `127.0.0.1:5006` (isolated from `Eventum` on `5001`, `AmadeusX` on `5002`, `Chan` on `5003`, `Presentum` on `5004`, and `Observatum` on `5005`).

### C. Telegram Folder Sync & MTProto Engine
* **Folder Peer Filtering:** Rather than listening to all joined dialogs, Venator inspects dialog filters (`GetDialogFiltersRequest`) matching the target name (e.g., `Фриланс`) and dynamically tracks incoming events strictly from matching channel and chat IDs.
* **Shared Folder Link Support (`t.me/addlist/...`):** When a shared folder invite link is provided, Venator checks and updates membership automatically via `CheckChatlistInviteRequest` and `JoinChatlistInviteRequest`.
* **Dynamic Re-Sync:** Subscribes to `UpdateDialogFilter` events; adding or removing channels in the Telegram client updates the monitored peer list instantly without restart.

### D. Automated Error Detection & Rate Limit Safeguards
* **HTTP 429 (Rate Limit) Alerting:** Detects Groq or OpenAI rate limit errors immediately and dispatches an emergency notification to Telegram detailing quota limits and reset schedules.
* **Anti-Flood Debounce:** Error alerts implement a 3-minute (`180s`) cooldown window per distinct error type, preventing notification storms when multiple posts arrive during provider downtime.
* **Ban & Kick Detection:** Monitors `events.ChatAction` and `types.UpdateChannel` (`ChannelForbidden`, `ChannelPrivateError`, `UserBannedInChannelError`), notifying the primary user if the listener account is removed from any monitored chat.

### E. Daemonization (Runit / termux-services)
* **Service Directory:** `$PREFIX/var/service/venator-backend/run`
* **Logging:** Symlinked to `$PREFIX/share/termux-services/svlogger` -> logs stored in `$PREFIX/var/log/sv/venator-backend/current`.
* **Run Script:**
  ```sh
  #!/data/data/com.termux/files/usr/bin/sh
  exec 2>&1
  cd /data/data/com.termux/files/home/Venator
  exec /data/data/com.termux/files/home/Venator/venv/bin/gunicorn -w 1 --threads 20 -b 127.0.0.1:5006 app:app
  ```

### F. Networking & Cloudflare Zero Trust Tunnel
* **Tunnel Connector:** `termux-android`
* **Route Configuration:** `venator.andatra.space` ➔ `http://127.0.0.1:5006`
* ⚠️ **IPv6 Address Binding Rule:** In Cloudflare Zero Trust Dashboard, the target must explicitly use `http://127.0.0.1:5006` rather than `http://localhost:5006` to avoid 5-second IPv6 `::1` resolution delays inside Android Bionic libc.

### G. Zero-Drain Hardware Power Profile
To allow permanent 24/7 background operation on a smartphone battery:
1. **Pure Epoll Push:** Zero background polling loops. Telethon suspends on Linux network sockets until an incoming Telegram packet arrives.
2. **SHA-256 Deduplication:** Cross-posted advertisements across regional freelance channels are hashed (`deque(maxlen=300)`) and skipped before invoking the network radio or LLM APIs.
3. **Client-Side Page Visibility API:** When the web dashboard is minimized or placed in a background tab (`document.hidden === true`), client polling stops (`SYNC: SLEEPING`), resulting in `0% CPU` overhead on the phone.

---

## 3. Directory Structure on Termux

```text
~/Venator/
├── .env                        # Local production secrets (keys, bot tokens, passwords)
├── .env.example                # Environment template
├── venator_listener.session    # Telethon MTProto session
├── database.db                 # SQLite WAL database (auto-initialized)
├── database.db-wal             # SQLite WAL journal
├── database.db-shm             # SQLite shared memory
├── app.py                      # Flask application & background daemon orchestrator
├── database.py                 # SQLite WAL engine, RAM cache & pruning routines
├── monitor.py                  # MTProto listener, LLM analyzer & error dispatcher
├── requirements.txt            # Python dependencies
├── README.md                   # User guide & project specification
├── SERVER.md                   # Architectural reference
├── venv/                       # Virtual environment
└── templates/
    ├── login.html              # Secure terminal login UI
    └── dashboard.html          # Control center, AI sandbox & live orders feed
```

---

## 4. Administration & Maintenance Cheat Sheet

### Service Management:
```bash
# Check service status
sv status venator-backend

# Restart service (after pulling new code or editing .env)
sv restart venator-backend

# Stop service
sv down venator-backend

# Start service
sv up venator-backend

# Stream live server logs
tail -f $PREFIX/var/log/sv/venator-backend/current
```

### Updating from GitHub:
```bash
cd ~/Venator
git pull
sv restart venator-backend
```

### Database Backup & Clean Slate:
```bash
# Create timestamped database backup
cp ~/Venator/database.db ~/venator_backup_$(date +%F).db

# Reset database to factory defaults
rm -f ~/Venator/database.db ~/Venator/database.db-wal ~/Venator/database.db-shm
sv restart venator-backend
```

