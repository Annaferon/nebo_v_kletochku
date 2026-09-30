import os
import sys
import random
import time
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone

# --- Config ---
DATABASE_URL = os.environ["DATABASE_URL"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]
TG_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_ADMIN_ID = int(os.environ["TELEGRAM_ADMIN_ID"])
TG_CHANNEL_ID = int(os.environ["TELEGRAM_CHANNEL_ID"])
LLM_MODEL = os.environ.get("LLM_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")


def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')


def send_tg(chat_id, text):
    """Отправляет сообщение, разбивая на части до 4000 символов."""
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    results = []
    chunks = []
    while text:
        if len(text) <= 4000:
            chunks.append(text)
            break
        split_at = text.rfind("\n\n", 0, 4000)
        if split_at == -1:
            split_at = 4000
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()

    for chunk in chunks:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=30)
        results.append(r.json())
        print(f"TG response: {r.json()}")
        time.sleep(0.5)
    return results


def call_llm(prompt):
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.9,
            "max_tokens": 900,
        },
        timeout=120,
    )
    data = r.json()
    if "choices" not in data:
        raise Exception(f"LLM error: {data}")
    return data["choices"][0]["message"]["content"].strip()


WRITER_PROMPT = """Напиши один пост на русском языке для Telegram-канала.

ТЕМА: {topic}

ЖЁСТКИЕ ПРАВИЛА:
- Отвечай ТОЛЬКО текстом поста. Не пиши рассуждений, не переводи задачу, не комментируй свои действия, не пиши "вот пост".
- Текст строго на русском языке.
- От первого лица, мужской голос, разговорный, простой.
- Тема — воспоминания человека, который провёл несколько лет в местах лишения свободы. Пиши атмосферно, через детали: что было, что говорили, как пахло, кто был рядом.
- Длина 800–1200 знаков.
- Никаких нравоучений и выводов. Просто воспоминание.
- Не упоминай реальные имена, города, годы, номера.
- Не используй слова "тюрьма", "зона", "камера", "отсидел" — только намёки и атмосфера.
- Без заголовков, без "Пост:", без кавычек вокруг текста.

ПРИМЕРЫ ГОЛОСА АВТОРА (если есть):
{samples}

Пиши сразу текст поста. Начинай с первой фразы истории.
"""


def task_write():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT content FROM style_samples LIMIT 20")
    samples = [s["content"] for s in cur.fetchall()]
    samples_text = "\n\n---\n\n".join(samples[:5]) if samples else "(примеров пока нет, пиши в общем стиле)"

    cur.execute("SELECT id, topic FROM topics WHERE used = FALSE LIMIT 20")
    topics = cur.fetchall()

    if not topics:
        send_tg(TG_ADMIN_ID, "⚠️ Тем в банке не осталось.")
        cur.close()
        conn.close()
        return

    picked = random.sample(topics, min(3, len(topics)))
    created = []

    for t in picked:
        prompt = WRITER_PROMPT.format(samples=samples_text, topic=t["topic"])
        try:
            text = call_llm(prompt)
        except Exception as e:
            print(f"Error: {e}")
            continue

        cur.execute(
            "INSERT INTO drafts (topic, content, status) VALUES (%s, %s, 'pending') RETURNING id",
            (t["topic"], text)
        )
        cur.execute("UPDATE topics SET used = TRUE WHERE id = %s", (t["id"],))
        conn.commit()
        created.append((t["topic"], text))

    cur.close()
    conn.close()

    if created:
        msg = "📝 <b>Черновики на сегодня:</b>\n\n"
        for i, (topic, text) in enumerate(created, 1):
            msg += f"<b>{i}. {topic}</b>\n\n{text}\n\n———\n\n"
        msg += "Одобрить: Neon → SQL Editor → UPDATE drafts SET status = 'approved' WHERE id = X;"
        send_tg(TG_ADMIN_ID, msg)


def task_publish():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        "SELECT * FROM drafts WHERE status = 'approved' AND published_at IS NULL ORDER BY created_at LIMIT 1"
    )
    draft = cur.fetchone()

    if not draft:
        print("No approved drafts")
        cur.close()
        conn.close()
        return

    send_tg(TG_CHANNEL_ID, draft["content"])
    cur.execute(
        "UPDATE drafts SET status = 'published', published_at = NOW() WHERE id = %s",
        (draft["id"],)
    )
    conn.commit()
    print(f"Published #{draft['id']}")

    cur.close()
    conn.close()


def task_stats():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT status, COUNT(*) as count FROM drafts GROUP BY status")
    rows = cur.fetchall()
    msg = "📊 <b>Статус системы</b>\n\n"
    for r in rows:
        msg += f"{r['status']}: {r['count']}\n"
    send_tg(TG_ADMIN_ID, msg)
    cur.close()
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python agent.py [write|publish|stats]")
        sys.exit(1)
    task = sys.argv[1]
    if task == "write":
        task_write()
    elif task == "publish":
        task_publish()
    elif task == "stats":
        task_stats()
    else:
        print(f"Unknown: {task}")
