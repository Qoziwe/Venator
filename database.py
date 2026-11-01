import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database.db"
ENV_PATH = BASE_DIR / ".env"


def load_env_file(filepath: Path = ENV_PATH) -> None:
    """Lightweight .env loader with zero external dependencies."""
    if not filepath.exists():
        return
    for raw_line in filepath.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


load_env_file()

# Pre-configured AI Provider Presets for 1-click switching in the Dashboard
LLM_PRESETS: dict[str, dict[str, Any]] = {
    "groq": {
        "label": "Groq Cloud (Free / Ultra-Fast)",
        "api_url": "https://api.groq.com/openai/v1/chat/completions",
        "models": [
            "openai/gpt-oss-120b",
            "llama-3.3-70b-versatile",
            "qwen/qwen3-32b",
            "llama-3.1-8b-instant",
        ],
    },
    "gemini": {
        "label": "Google Gemini (AI Studio Free Tier)",
        "api_url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "models": [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
        ],
    },
    "openrouter": {
        "label": "OpenRouter (Multi-Model Gateway)",
        "api_url": "https://openrouter.ai/api/v1/chat/completions",
        "models": [
            "deepseek/deepseek-chat-v3-0324:free",
            "google/gemini-2.5-flash",
            "anthropic/claude-3.5-haiku",
            "openai/gpt-4o-mini",
        ],
    },
    "openai": {
        "label": "OpenAI Official",
        "api_url": "https://api.openai.com/v1/chat/completions",
        "models": [
            "gpt-4o-mini",
            "gpt-4o",
        ],
    },
    "custom": {
        "label": "Custom OpenAI-Compatible / Local Ollama",
        "api_url": "http://127.0.0.1:11434/v1/chat/completions",
        "models": [
            "qwen2.5-coder:7b",
            "llama3.2:3b",
        ],
    },
}

DEFAULT_TARGET_KEYWORDS = [
    r"python", r"питон", r"flask", r"django", r"fastapi",
    r"react", r"next\.?js", r"typescript", r"javascript", r"node\.?js",
    r"react native", r"expo", r"мобильн", r"приложени",
    r"парсер", r"спарсить", r"parser", r"scraping", r"скрейпинг",
    r"бот", r"bot", r"mini[\s\-]?app", r"tma", r"web[\s\-]?app",
    r"деплой", r"задеплоить", r"docker", r"ubuntu", r"linux", r"vps", r"сервер",
    r"llm", r"openai", r"claude", r"gpt", r"ии", r"нейро",
    r"fullstack", r"full[\s\-]?stack", r"фуллстек", r"фронтенд", r"бэкенд",
    r"frontend", r"backend", r"mvp", r"сайт", r"верстк", r"доработ", r"фикс",
    r"postgresql", r"supabase", r"баз[аыу] данных",
]

DEFAULT_STOP_KEYWORDS = [
    r"#резюме", r"#cv", r"ищу работу", r"предлагаю услуги",
    r"выполню заказы", r"готов взяться", r"приглашаем на курс",
    r"бесплатный вебинар", r"школа программирования",
]

DEFAULT_SYSTEM_PROMPT = """You are a strict technical job & freelance order filter for a Junior/Middle Full-Stack & Mobile Developer.

Developer Profile:
- Frontend: React, Next.js (App Router), TypeScript, JavaScript, HTML/CSS, Tailwind CSS, Zustand
- Mobile: React Native, Expo, Telegram Mini Apps (TMA)
- Backend & Data: Python (Flask, Django, SQLAlchemy), Node.js, PostgreSQL, Supabase, SQLite, Socket.IO, REST API, WebSockets
- Automation & AI: Telegram Bots, Web Scraping / Parsers, LLM integrations (OpenAI, Claude, Groq, etc.)
- Infrastructure: Linux (Ubuntu/Arch VPS), Docker, Gunicorn, Cloudflare (Tunnel, R2), Vercel
- Availability: Remote freelance tasks, MVP development, bug fixing, server deployment, or Junior/Middle remote roles.

Your Task:
Analyze the incoming Telegram post and return ONLY a valid JSON object (no markdown fences) evaluating if this is a real, safe, and matching opportunity.

REJECT (set "approved": false) if ANY of the following is true:
1. SCAM / FRAUD:
   - Requires upfront payment, security deposit, buying a course/subscription, or paying for "verification".
   - Casino, gambling, betting, crypto drainers, phishing, carding, darknet, or illegal data leaks.
   - "Easy money" spam (e.g., "$300/day for clicking/liking", pyramid schemes, arbitrage scams, drop/mule recruiting).
   - Unpaid work for "future startup equity only" with zero budget.
   - Fake freelancer resume or someone advertising their own development services instead of hiring.
2. STACK / LEVEL MISMATCH:
   - Strictly requires incompatible stacks: 1C, PHP/WordPress/Bitrix/Laravel-only, C#/.NET, Java/Spring, Go, Rust, C++, Swift/Kotlin native-only.
   - Strictly requires Senior/Architect/Team Lead with 5+ years of corporate experience and explicitly rejects Juniors/Middles/Freelancers.
   - Strictly requires offline office presence outside Almaty or Kuala Lumpur (remote must be allowed).

Respond ONLY with this exact JSON schema:
{
  "approved": true or false,
  "match_score": integer from 0 to 100,
  "is_scam": true or false,
  "summary": "1 short sentence describing the task/role and budget if mentioned",
  "reason": "1 short sentence explaining why it matches or why it was rejected",
  "recommended_pitch": "A (Web/Backend/VPS) | B (Bot/Parser/AI) | C (Mobile/TMA)"
}"""

_cache_lock = threading.Lock()
_settings_cache: dict[str, Any] | None = None
_compiled_regex_cache: dict[str, list[re.Pattern]] = {"targets": [], "stops": []}


def get_connection() -> sqlite3.Connection:
    """Create a thread-safe SQLite connection configured for WAL low-power operation."""
    conn = sqlite3.connect(str(DB_PATH), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    """Initialize SQLite schema and populate default settings if empty."""
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                chat_title TEXT NOT NULL,
                msg_link TEXT NOT NULL,
                approved INTEGER NOT NULL DEFAULT 0,
                is_scam INTEGER NOT NULL DEFAULT 0,
                match_score INTEGER NOT NULL DEFAULT 0,
                summary TEXT NOT NULL,
                reason TEXT NOT NULL,
                recommended_pitch TEXT NOT NULL,
                model_used TEXT NOT NULL,
                raw_text TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ban_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                chat_title TEXT NOT NULL,
                details TEXT NOT NULL
            )
            """
        )

        defaults = {
            "is_paused": "0",
            "target_folder": os.getenv("TG_TARGET_FOLDER", "Фриланс"),
            "folder_invite_link": os.getenv("TG_FOLDER_INVITE_LINK", ""),
            "llm_provider": os.getenv("LLM_PROVIDER", "groq"),
            "llm_api_url": os.getenv(
                "LLM_API_URL", "https://api.groq.com/openai/v1/chat/completions"
            ),
            "llm_api_key": os.getenv("LLM_API_KEY", ""),
            "llm_model": os.getenv("LLM_MODEL", "openai/gpt-oss-120b"),
            "min_score": "70",
            "target_keywords": "\n".join(DEFAULT_TARGET_KEYWORDS),
            "stop_keywords": "\n".join(DEFAULT_STOP_KEYWORDS),
            "system_prompt": DEFAULT_SYSTEM_PROMPT,
        }

        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (k, str(v)),
            )
        conn.commit()

    _refresh_memory_cache()


def _refresh_memory_cache() -> dict[str, Any]:
    """Load settings from SQLite into RAM and pre-compile regex patterns."""
    global _settings_cache, _compiled_regex_cache
    with get_connection() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        raw = {row["key"]: row["value"] for row in rows}

    # If .env has an API key and DB still has an empty string, sync it automatically
    env_key = os.getenv("LLM_API_KEY", "")
    if not raw.get("llm_api_key") and env_key and not env_key.startswith("gsk_your_"):
        raw["llm_api_key"] = env_key

    target_lines = [
        line.strip()
        for line in raw.get("target_keywords", "").splitlines()
        if line.strip()
    ]
    stop_lines = [
        line.strip()
        for line in raw.get("stop_keywords", "").splitlines()
        if line.strip()
    ]

    compiled_targets: list[re.Pattern] = []
    for pattern in target_lines:
        try:
            compiled_targets.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            compiled_targets.append(re.compile(re.escape(pattern), re.IGNORECASE))

    compiled_stops: list[re.Pattern] = []
    for pattern in stop_lines:
        try:
            compiled_stops.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            compiled_stops.append(re.compile(re.escape(pattern), re.IGNORECASE))

    parsed = {
        "is_paused": raw.get("is_paused", "0") == "1",
        "target_folder": raw.get("target_folder", "Фриланс").strip(),
        "folder_invite_link": raw.get("folder_invite_link", "").strip(),
        "llm_provider": raw.get("llm_provider", "groq"),
        "llm_api_url": raw.get(
            "llm_api_url", "https://api.groq.com/openai/v1/chat/completions"
        ),
        "llm_api_key": raw.get("llm_api_key", ""),
        "llm_model": raw.get("llm_model", "openai/gpt-oss-120b"),
        "min_score": int(raw.get("min_score", "70") or 70),
        "target_keywords": "\n".join(target_lines),
        "stop_keywords": "\n".join(stop_lines),
        "system_prompt": raw.get("system_prompt", DEFAULT_SYSTEM_PROMPT),
    }

    with _cache_lock:
        _settings_cache = parsed
        _compiled_regex_cache = {
            "targets": compiled_targets,
            "stops": compiled_stops,
        }
    return parsed


def get_settings() -> dict[str, Any]:
    """Return cached settings from RAM (Zero disk I/O during message filtering)."""
    with _cache_lock:
        if _settings_cache is not None:
            return dict(_settings_cache)
    return _refresh_memory_cache()


def get_compiled_regex() -> tuple[list[re.Pattern], list[re.Pattern]]:
    """Return pre-compiled target and stop regex lists from RAM."""
    if _settings_cache is None:
        _refresh_memory_cache()
    with _cache_lock:
        return (
            list(_compiled_regex_cache["targets"]),
            list(_compiled_regex_cache["stops"]),
        )


def update_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Persist updated configuration to SQLite and refresh RAM cache."""
    allowed_keys = {
        "is_paused",
        "target_folder",
        "folder_invite_link",
        "llm_provider",
        "llm_api_url",
        "llm_model",
        "llm_api_key",
        "min_score",
        "target_keywords",
        "stop_keywords",
        "system_prompt",
    }
    with get_connection() as conn:
        for k, v in updates.items():
            if k not in allowed_keys:
                continue
            if k == "is_paused":
                val_str = "1" if v in (True, "1", 1, "true", "on") else "0"
            else:
                val_str = str(v).strip()
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (k, val_str),
            )
        conn.commit()
    return _refresh_memory_cache()


def log_order(
    chat_title: str,
    msg_link: str,
    approved: bool,
    is_scam: bool,
    match_score: int,
    summary: str,
    reason: str,
    recommended_pitch: str,
    model_used: str,
    raw_text: str,
) -> int:
    """Save evaluated order to SQLite and prune records older than 300 entries."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO orders (
                created_at, chat_title, msg_link, approved, is_scam,
                match_score, summary, reason, recommended_pitch, model_used, raw_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_str,
                chat_title,
                msg_link,
                1 if approved else 0,
                1 if is_scam else 0,
                int(match_score),
                summary,
                reason,
                recommended_pitch,
                model_used,
                raw_text[:2000],
            ),
        )
        row_id = cursor.lastrowid or 0
        # Keep database compact on Termux (max 300 recent evaluations)
        conn.execute(
            """
            DELETE FROM orders
            WHERE id NOT IN (
                SELECT id FROM orders ORDER BY id DESC LIMIT 300
            )
            """
        )
        conn.commit()
    return row_id


def get_orders(filter_type: str = "all", limit: int = 60) -> list[dict[str, Any]]:
    """Fetch evaluated orders for the Web Dashboard."""
    query = "SELECT * FROM orders"
    params: list[Any] = []

    if filter_type == "approved":
        query += " WHERE approved = 1"
    elif filter_type == "rejected":
        query += " WHERE approved = 0 AND is_scam = 0"
    elif filter_type == "scam":
        query += " WHERE is_scam = 1"

    query += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def clear_orders() -> None:
    """Clear all logged order evaluations."""
    with get_connection() as conn:
        conn.execute("DELETE FROM orders")
        conn.commit()


def log_ban_event(event_type: str, chat_title: str, details: str) -> None:
    """Record a channel/group kick, ban, or account restriction event."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO ban_events (created_at, event_type, chat_title, details)
            VALUES (?, ?, ?, ?)
            """,
            (now_str, event_type, chat_title, details),
        )
        conn.commit()


def get_ban_events(limit: int = 30) -> list[dict[str, Any]]:
    """Fetch recent ban/kick alerts."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ban_events ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in rows]


def get_stats() -> dict[str, int]:
    """Compute aggregate KPI counters for the Dashboard header."""
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        approved = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE approved = 1"
        ).fetchone()[0]
        scams = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE is_scam = 1"
        ).fetchone()[0]
        rejected = total - approved
        bans = conn.execute("SELECT COUNT(*) FROM ban_events").fetchone()[0]

    return {
        "total_analyzed": total,
        "approved_count": approved,
        "rejected_count": rejected,
        "scam_blocked": scams,
        "ban_alerts": bans,
    }