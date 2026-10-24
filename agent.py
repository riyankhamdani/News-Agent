import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime
from urllib.parse import urlparse, urlunparse
import requests
import telebot
from google import genai
from google.genai import types
from google.genai.errors import APIError

# Environment Credentials
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_NEWS_FILE = os.path.join(BASE_DIR, "seen_news.json")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None


def normalize_url(raw_url: str) -> str:
    """Membersihkan URL dari query parameters dan trailing slash."""
    try:
        parsed = urlparse(raw_url)
        return urlunparse(
            (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", "")
        )
    except Exception:
        return raw_url.strip().rstrip("/")


def load_seen_news() -> set:
    """Membaca riwayat URL berita. Menginisialisasi file jika belum ada."""
    if not os.path.exists(SEEN_NEWS_FILE):
        try:
            with open(SEEN_NEWS_FILE, "w") as f:
                json.dump([], f)
            print(f"📄 File {SEEN_NEWS_FILE} berhasil diinisialisasi otomatis.")
        except Exception as e:
            print(f"⚠️ Gagal inisialisasi awal {SEEN_NEWS_FILE}: {e}")
        return set()

    try:
        with open(SEEN_NEWS_FILE, "r") as f:
            return set(json.load(f))
    except Exception as e:
        print(f"⚠️ Gagal membaca {SEEN_NEWS_FILE}: {e}")
        return set()


def save_seen_news(seen_news: set):
    """Menyimpan riwayat URL ke file JSON."""
    try:
        with open(SEEN_NEWS_FILE, "w") as f:
            json.dump(list(seen_news), f, indent=2)
        print(f"💾 Berhasil menyimpan {len(seen_news)} URL ke {SEEN_NEWS_FILE}")
    except Exception as e:
        print(f"⚠️ Gagal menyimpan {SEEN_NEWS_FILE}: {e}")


def search_tavily(query: str, seen_urls: set, domains: list = None, topic: str = "general"):
    if not TAVILY_API_KEY:
        print("⚠️ Warning: TAVILY_API_KEY tidak ditemukan.")
        return []

    url = "https://api.tavily.com/search"
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "topic": topic,
        "days": 3,
        "max_results": 6,
        "search_depth": "advanced",
    }
    if domains:
        payload["include_domains"] = domains

    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code == 200:
            results = response.json().get("results", [])
            valid_articles = []
            for r in results:
                raw_link = r.get("url", "")
                clean_link = normalize_url(raw_link)

                if clean_link in seen_urls or raw_link in seen_urls:
                    continue

                valid_articles.append({
                    "title": r.get("title", "No Title"),
                    "url": raw_link,
                    "clean_url": clean_link,
                    "snippet": r.get("content", "")[:350],
                })

            return valid_articles[:2]
        else:
            print(f"❌ Tavily Error status code: {response.status_code}")
            return []
    except Exception as e:
        print(f"❌ Error fetching from Tavily: {e}")
        return []


def generate_summary_with_fallback(client, prompt, retries_per_model=3, initial_delay=5):
    """Memanggil Gemini API secara dinamis dengan seluruh fallback model aktif di akun."""
    candidate_models = []

    # 1. Deteksi dinamis model aktif dari API key kamu
    try:
        active_models = [m.name.replace("models/", "") for m in client.models.list()]
        preferred = [
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-1.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.5-pro",
            "gemini-1.5-pro",
        ]
        for pref in preferred:
            if pref in active_models:
                candidate_models.append(pref)
        for m in active_models:
            if "gemini" in m and "embedding" not in m and m not in candidate_models:
                candidate_models.append(m)
    except Exception as e:
        print(f"⚠️ Gagal membaca model dinamis: {e}. Menggunakan daftar fallback default.")

    if not candidate_models:
        candidate_models = [
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-1.5-flash",
            "gemini-2.5-pro",
        ]

    config = types.GenerateContentConfig(
        temperature=0.2,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    for model_name in candidate_models:
        print(f"🔄 Mencoba generate dengan model: {model_name}")
        for attempt in range(1, retries_per_model + 1):
            try:
                response = client.models.generate_content(
                    model=model_name, contents=prompt, config=config
                )
                if response.text:
                    print(f"✅ Sukses generate summary menggunakan model: {model_name}")
                    return response.text
            except Exception as e:
                err_msg = str(e)
                print(f"⚠️ [{model_name}] Percobaan {attempt}/{retries_per_model} Gagal: {err_msg[:120]}")

                # Langsung lewati model jika tidak didukung akun / 404
                if any(k in err_msg.lower() for k in ["not found", "404", "deprecated", "limit: 0", "unsupported"]):
                    print(f"⏩ Model {model_name} tidak didukung akun ini. Lewati...")
                    break

                delay = (initial_delay * attempt) + random.uniform(1.0, 3.0)
                time.sleep(delay)

    raise Exception("Gagal mendapatkan respons dari seluruh kandidat model Gemini.")


def get_news_digest(seen_urls: set):
    """Mengambil intel teknis dan radar hackathon/bounty."""
    today_str = datetime.now().strftime("%B %Y")

    search_configs = {
        "OUTAGES": {
            "query": f'("post-mortem" OR "outage analysis" OR "incident retrospective") ("Kubernetes" OR "AWS" OR "Cloudflare" OR "cloud") {today_str}',
            "domains": None,
            "topic": "news",
        },
        "CLOUD_AI_INFRA": {
            "query": f'("Kubernetes security" OR "CVE" OR "vLLM" OR "AI infrastructure" OR "GPU cluster") ("release" OR "architecture") {today_str}',
            "domains": None,
            "topic": "news",
        },
        "HACKATHONS_BOUNTIES": {
            "query": '("AI hackathon" OR "cloud challenge" OR "bounty" OR "developer grant") ("prize pool" OR "cash prize" OR "reward") ("online" OR "global")',
            "domains": [
                "devpost.com",
                "dorahacks.io",
                "lablab.ai",
                "algora.io",
                "polar.sh",
                "taikai.network",
            ],
            "topic": "general",
        },
    }

    digest_data = {}
    for cat, cfg in search_configs.items():
        digest_data[cat] = search_tavily(
            cfg["query"], seen_urls, domains=cfg["domains"], topic=cfg["topic"]
        )

    total_items = sum(len(items) for items in digest_data.values())
    if total_items == 0:
        print("ℹ️ Tidak ada update baru (berita teknis maupun hackathon) hari ini.")
        return None, []

    if not GEMINI_API_KEY:
        return "❌ Error: GEMINI_API_KEY belum dikonfigurasi.", []

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
You are an Executive Tech Advisor and Opportunity Scout for Riyan (Senior SRE / Cloud Engineer).
Summarize this raw data into concise, high-density Bahasa Indonesia.

RAW DATA:
{digest_data}

Instructions:
1. Divide into 2 primary sections:
   - 🚨 **SRE & CLOUD INCIDENT INTELLIGENCE** (from OUTAGES and CLOUD_AI_INFRA)
   - 🏆 **GLOBAL HACKATHONS & BOUNTY RADAR** (from HACKATHONS_BOUNTIES)
2. For Tech Intelligence items:
   - Bold Title
   - 1-2 sentence core engineering takeaway (root cause, vulnerability, or infra enhancement).
   - Direct valid URL.
3. For Hackathon / Bounty items:
   - Bold Competition / Bounty Name
   - Mention Prize Pool / Reward if mentioned in data, deadline/status, and technical focus (AI Agents, Cloud Native, Go, Python, etc.)
   - Direct application / registration URL.
4. If either section is empty, output "Belum ada update baru hari ini." for that section.

Format Template:
📰 **TECH & OPPORTUNITY DIGEST** 📰
====================================

🚨 **SRE & CLOUD INCIDENT INTELLIGENCE**
• **[Judul Artikel / Insiden]**
  [Analisis teknis 1-2 kalimat: Masalah / Root cause / Mitigasi]
  🔗 Baca laporan: [URL]

🏆 **GLOBAL HACKATHONS & BOUNTY RADAR**
• **[Nama Hackathon / Proyek Bounty]**
  💰 Hadiah: [Estimasi Prize Pool / Reward] | 🛠️ Fokus: [Tech stack / Track tantangan]
  🔗 Info & Daftar: [URL]
"""
    summary_text = generate_summary_with_fallback(client, prompt)

    new_urls = [
        item["clean_url"]
        for cat in digest_data.values()
        for item in cat
    ]
    return summary_text, new_urls


def send_safe_message(chat_id, text):
    if not bot:
        print("❌ Bot instance tidak ditemukan.")
        return False
    try:
        bot.send_message(chat_id, text, disable_web_page_preview=True, parse_mode="Markdown")
        return True
    except Exception as e:
        print(f"⚠️ Gagal kirim via Markdown ({e}). Mencoba teks biasa...")
        try:
            bot.send_message(chat_id, text, disable_web_page_preview=True)
            return True
        except Exception as err:
            print(f"❌ Gagal total kirim ke Telegram: {err}")
            return False


if __name__ == "__main__":
    is_cron_mode = "--cron" in sys.argv or os.getenv("RUN_MODE") == "cron"

    if is_cron_mode:
        print("🚀 Menjalankan Digest Bot dalam MODE CRON (GitHub Actions)...")
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("❌ Error: Secrets Telegram belum diset.")
            sys.exit(1)

        seen_urls = load_seen_news()
        print(f"🔎 Menyisir radar berita teknis & hackathon (Riwayat: {len(seen_urls)} data)...")

        try:
            report, new_urls = get_news_digest(seen_urls)
            if report:
                print(f"📤 Mengirim laporan ke Chat ID: {TELEGRAM_CHAT_ID}...")
                success = send_safe_message(TELEGRAM_CHAT_ID, report)
                if success and new_urls:
                    seen_urls.update(new_urls)
                    save_seen_news(seen_urls)
                    print("✅ Berhasil dikirim dan state seen_news.json diperbarui.")
            else:
                print("ℹ️ Tidak ada pesan terkirim (tidak ada entri baru).")
            sys.exit(0)
        except Exception as e:
            print(f"❌ Terjadi error: {e}")
            sys.exit(1)  # Menghentikan runner agar git push tidak dijalankan saat terjadi kegagalan
    else:
        print("🤖 Bot interaktif aktif (Mode Polling)...")
        if bot:
            bot.infinity_polling()
        else:
            print("❌ Error: TELEGRAM_BOT_TOKEN belum diset.")
