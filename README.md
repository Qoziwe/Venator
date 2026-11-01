# 🎯 Venator — AI-Powered Telegram Order & Vacancy Radar

**Venator** is an autonomous, self-hosted Telegram monitoring platform and web control center designed to capture freelance software engineering orders and remote jobs in real time. Powered by **Python, Telethon (MTProto), Flask, Gunicorn, and SQLite (WAL)**, it combines socket-level message ingestion with an **OpenAI-compatible LLM pipeline (Groq, Gemini, Ollama)** to eliminate scams and filter opportunities strictly against your technical stack.

🌐 **Live Control Center:** `https://venator.andatra.space`  
💻 **GitHub Repository:** `https://github.com/Qoziwe/Venator`

---

## ✨ Features & Capabilities

- **Zero-Polling Event Engine:** Passively listens to Telegram MTProto events via `epoll`. Zero scraping loops, zero CPU drain on low-power devices.
- **Smart Folder Synchronization:** Listens strictly to designated Telegram folders (e.g., `Фриланс`) or shared folder links (`t.me/addlist/...`). Automatically tracks newly added channels in real time.
- **Strict Anti-Scam & Stack Verification:** Every incoming post is parsed through an LLM with strict JSON schemas. Eliminates pyramid schemes, carding, betting, unbudgeted work, and incompatible tech stacks.
- **SHA-256 Cross-Post Deduplication:** Maintains an in-memory ring buffer of message fingerprints. Vacancies reposted across multiple channels are analyzed only once, saving 80%+ of API limits.
- **Instant Telegram Bot Dispatch:** Delivers qualified opportunities (`match_score >= 70%`) directly to your private Telegram account with task summaries, fit explanations, and tailored pitch recommendations (`A / B / C`).
- **Comprehensive Error Alerting:** Immediately alerts your Telegram if an AI provider exceeds rate limits (`HTTP 429`), encounters invalid keys (`401`), or drops connections, backed by anti-flood debouncing.
- **Web-Based Terminal Authentication:** Interactive QR code and 2FA password login directly in the browser—no terminal access required to pair your listener account.
- **Live AI Sandbox:** In-browser testing playground to evaluate sample posts, verify prompts, and test bot delivery in real time.
- **Ultra-Low Energy Footprint:** Engineered for 24/7 unattended operation on Android/Termux smartphones or budget Linux VPS nodes.

---

## 📖 How to Use Venator (Complete User Guide)

### 1. Preparing Your Telegram Channels

1. In your Telegram client, organize all freelance channels, job boards, and developer chats into a dedicated folder (e.g., named **`Фриланс`** or **`Work`**).
2. Right-click the folder tab ➔ select **Share Folder** ➔ copy the generated invite link (`https://t.me/addlist/...`).
3. Whenever you discover a new channel, simply add it to this folder; Venator detects `UpdateDialogFilter` events and monitors it immediately without needing a restart.

---

### 2. Accessing the Control Center

1. Navigate to your deployed instance URL: `https://venator.andatra.space` (or `http://127.0.0.1:5006` locally).
2. Enter your `ADMIN_PASSWORD` (configured in `.env`) to unlock the terminal.
3. If the listener account is not yet authorized:
   - Click the green **`Generate QR Code`** button in the notification banner.
   - Open Telegram on your listener phone ➔ **Settings ➔ Devices ➔ Link Desktop Device** ➔ scan the code.
   - If Cloud Password (2FA) is enabled on the account, enter your password into the input field and click **Confirm 2FA Password**.
   - The badge in the top bar will turn green: `ONLINE`.

---

### 3. Configuring the AI Engine on the Fly

Under **AI Engine & Model Switcher**:

1. **Target Folder:** Enter your folder name (`Фриланс`) or paste your `t.me/addlist/...` invite link.
2. **AI Provider Preset:** Select from **Groq Cloud** (ultra-fast, free tier), **Google Gemini**, **OpenRouter**, **OpenAI**, or **Custom / Local Ollama**.
3. **Model Selection:** Choose a pre-configured model (e.g., `openai/gpt-oss-120b` or `gemini-2.5-flash`) or enter any custom model identifier.
4. **API Key:** Paste your provider key (e.g., `gsk_...`). The key is securely stored in SQLite; leave the field blank on subsequent edits to preserve the current key.
5. **Threshold:** Set the minimum `Min Score (%)` (default: `70%`). Any post evaluated below this score will be logged as rejected and will not notify your Telegram.
6. **Save & Validate:** Click **Save & Hot-Reload Settings in RAM**. Venator sends a test verification ping to the model:
   - If valid: confirms `Active & Verified` in green and hot-reloads memory caches.
   - If invalid: rejects the change, alerts you with the exact provider error, and keeps your previous working configuration.

---

### 4. Testing with the AI Sandbox

Before waiting for live posts, you can test how the active model evaluates any vacancy:

1. Paste a sample job offer into the **AI Sandbox & Telegram Alert Tester** text box.
2. Check **Also push result to Telegram** if you want to verify bot delivery.
3. Click **Test AI Model**.
4. The raw JSON evaluation from the LLM will render instantly in the terminal sandbox, detailing `approved`, `match_score`, `is_scam`, and `recommended_pitch`.

---

### 5. Reviewing Orders & Taking Action

- The **Evaluated Vacancies & Orders** feed streams every post captured from your folder:
  - 🟢 **APPROVED:** Matches your stack with budget and contact link.
  - 🔴 **SCAM BLOCKED:** Blocked phishing, gambling, MLM, deposit schemes, or unpaid equity traps.
  - ⚪ **REJECTED:** Incompatible tech stack (e.g., 1C, PHP, C#) or corporate Senior-only positions.
- Use the filter tabs (`All`, `Approved`, `Rejected`, `Scams`) to inspect decisions.
- Click **Open Post ↗** to open the exact Telegram message and reply to the client immediately.

---

### 6. Outreach Pitch Templates

When Venator sends an alert to your Telegram bot, it recommends an outreach pitch template:

- **Pitch A (Web / Backend / VPS):** For Next.js, React, Flask, Django, PostgreSQL, Docker, and Linux server deployment tasks.
- **Pitch B (Bots / Parsers / AI):** For Telegram bots, TMA web apps, data scrapers, and LLM automation pipelines.
- **Pitch C (Mobile Apps / Cross-Platform):** For React Native and Expo projects.

Copy your pre-configured pitch template, tailor the first sentence to the client's name and specific problem, and send it within 1–3 minutes of publication.

---

### 7. Telegram Error Notifications

You don't need to check server logs to monitor system health. Venator dispatches automated alerts to your Telegram bot whenever:

- **Rate Limits Exceeded (`HTTP 429`):** Indicates Groq/OpenAI quota limits are hit, explaining daily reset times.
- **API Failures (`HTTP 401 / 404 / 500`):** Alerts if an API key expires, a model is discontinued, or a network timeout occurs.
- **Channel Bans / Kicks:** Alerts if the listener account is removed from any monitored chat.

---

## 🛠 Tech Stack

- **Backend:** Python 3.12+, Flask 3.1, Gunicorn 23 (gthread worker)
- **Telegram MTProto:** Telethon 1.40 (pure socket event listening)
- **Telegram Bot API:** Native standard library HTTP engine (`urllib.request`)
- **Database:** SQLite 3 (WAL mode, zero ORM overhead, auto-pruning to 300 entries)
- **Frontend:** Jinja2 SSR, Tailwind CSS (CDN), QRCode.js, Page Visibility API
- **AI Integrations:** Groq Cloud (`openai/gpt-oss-120b`), Google Gemini, OpenRouter, Ollama
- **Infrastructure:** Termux (Android Linux ARM64), Runit (`termux-services`), Cloudflare Zero Trust Tunnel

---

## 🚀 Local Installation & Quickstart

```bash
# 1. Clone repository
git clone https://github.com/Qoziwe/Venator.git
cd Venator

# 2. Setup virtual environment & dependencies
python -m venv venv
./venv/bin/pip install -r requirements.txt

# 3. Configure secrets
cp .env.example .env
nano .env

# 4. Start server
./venv/bin/gunicorn -w 1 --threads 20 -b 127.0.0.1:5006 app:app
```

Open `http://127.0.0.1:5006`, enter your `ADMIN_PASSWORD`, and pair your listener account via QR code.

For full Termux deployment and Runit service setup, refer to [SERVER.md](SERVER.md).

---

## 📄 License

MIT License — Developed by [Lekim Zlavdinov](https://github.com/Qoziwe).
