# 🎯 Venator — AI-Powered Telegram Order & Vacancy Radar

**Venator** is a self-hosted, ultra-low-power Telegram monitoring engine and web dashboard designed to capture freelance orders and remote software engineering roles in real time. Built with **Python, Telethon (MTProto), Flask, Gunicorn, and SQLite (WAL)**, it combines passive socket-level listening with an **OpenAI-compatible LLM pipeline** to block scams and filter opportunities strictly against a developer's technical stack.

Live Deployment: `https://venator.andatra.space`

---

## ✨ Key Capabilities

1. **Two-Stage Filtering Pipeline (Regex ➔ LLM)**
   - **Stage 1 (0 ms RAM Pre-Filter):** Discards resumes (`#cv`, `ищу работу`), course ads, and off-topic chatter using pre-compiled regular expressions stored in memory.
   - **Stage 2 (SHA-256 Cross-Post Deduplication):** Computes a normalized SHA-256 fingerprint of incoming posts (`deque(maxlen=300)`) so identical vacancies reposted across multiple channels trigger only **one** LLM evaluation.
   - **Stage 3 (Structured JSON AI Evaluation):** Evaluates candidate posts against your stack and anti-scam rules, returning a match score (`0–100%`), scam verdict, 1-sentence summary, and recommended outreach pitch (`A / B / C`).

2. **Hot-Swappable Multi-Provider AI Dashboard**
   - Switch between **Groq Cloud** (`openai/gpt-oss-120b`, `llama-3.3-70b-versatile`, `qwen/qwen3-32b`), **Google Gemini**, **OpenRouter**, **OpenAI**, or **Local Ollama** in one click without restarting the service.
   - Edit target/stop regex patterns, minimum match score, and the system prompt in real time with instant RAM cache synchronization.
   - Includes a live **AI Sandbox** to test prompts on sample posts and verify Telegram Bot API delivery.

3. **Three-Tier Ban & Restriction Detector**
   - **Group Kicks (`events.ChatAction`):** Immediately alerts you if the listener account is removed from a megagroup.
   - **Channel Bans (`events.Raw(types.UpdateChannel)`):** Detects `ChannelForbidden`, `ChannelPrivateError`, and `UserBannedInChannelError` events on broadcast channels.
   - **Account Freeze Protection:** Catches `UserDeactivatedBanError` and `AuthKeyUnregisteredError`, logging the incident and dispatching a critical alert to your primary Telegram account.

4. **Zero-Drain Energy Profile (Termux / Mobile Server Ready)**
   - Event-driven `epoll` MTProto listener with zero background polling loops.
   - RAM-cached configuration eliminates disk reads on incoming messages.
   - Client-side **Page Visibility API** automatically suspends dashboard state synchronization (`SYNC: SLEEPING`) whenever the browser tab is hidden or minimized.

---

## 🏗 Project Structure

```text
Venator/
├── .env.example            # Environment variable template (Port 5006)
├── .gitignore              # Protects .env, *.session, and SQLite WAL files
├── app.py                  # Flask WSGI application, auth, REST API & daemon bootstrap
├── database.py             # SQLite WAL storage, RAM settings cache & auto-pruning logs
├── monitor.py              # Telethon MTProto listener, SHA-256 dedup & LLM analyzer
├── requirements.txt        # Pure-Python dependencies (Flask, gunicorn, telethon)
├── README.md               # Project overview & quickstart
├── SERVER.md               # Termux Runit & Cloudflare Zero Trust architecture docs
└── templates/
    ├── login.html          # Password-protected terminal login
    └── dashboard.html      # Real-time AI configuration & order feed UI
```

---

## 🚀 Local Quickstart

1. **Create a virtual environment and install dependencies:**

   ```bash
   python -m venv venv
   ./venv/bin/pip install -r requirements.txt
   ```

2. **Configure environment variables:**
   Copy `.env.example` to `.env` and fill in your Telegram API credentials (`my.telegram.org`), `@BotFather` token, target chat ID, and initial LLM API key.

3. **Authorize the Telegram listener session (one-time interactive login):**

   ```bash
   ./venv/bin/python monitor.py --auth
   ```

4. **Run the server:**
   ```bash
   ./venv/bin/gunicorn -w 1 --threads 20 -b 127.0.0.1:5006 app:app
   ```
   Open `http://127.0.0.1:5006` and sign in with your `ADMIN_PASSWORD`.

---

## 📄 License

MIT License — Built by [Lekim Zlavdinov](https://github.com/Qoziwe).
