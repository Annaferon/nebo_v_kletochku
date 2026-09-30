import os
import sys
import random
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
LLM_MODEL = os.environ.get("LLM_MODEL", "meta-llama/llama-3.3-70b-instruct")


def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')


def send_tg(chat_id, text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=30)
    return r.json()


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
            "temperature": 0.85,
            "max_tokens": 900,
        },
        timeout=120,
    )
    data = r.json()
    if "choices" not in data:
        raise Exception(f"LLM error: {data}")
    return data["choices"][0]["message"]["content"].strip()


WRITER_PROMPT = """Ты — редактор анонимного Telegram-канала. Канал ведёт человек, который провёл несколько лет в местах лишения свободы. Он рассказывает реальные истории изнутри — про людей, быт, привычки, тишину.

ЗАДАЧА: написать один пост на заданную тему.

ВАЖНО — это истории от первого лица, реальные, простые. Не публицистика, не нравоучения. Просто воспоминание.

СТИЛЬ:
- От первого лица, мужской, простой разговорный язык.
- Без пафоса, без морализаторства, без выводов "я понял, что...".
- Конкретные детали вместо общих слов: как пахло, что говорили, кто сидел рядом, что было на столе.
- Короткие предложения. Абзацы по 2–3 строки.
- Длина 800–1200 знаков.
- Финал — открытый, без точки. Вопрос, образ, тишина.

ЧЕГО НЕ ДЕЛАТЬ:
- Не романтизировать преступность.
- Не упоминать реальные имена, клички, города, номера, годы.
- Не давать советов, не учить жизни.
- Не использовать штампы: "тюрьма научила", "я исправился", "на зоне не принято".
- Не использовать блатной сленг ради сленга.
- Не писать слова "тюрьма", "зона", "камера", "отсидел" — используй намёки, атмосферу, предметы.

ПРИМЕРЫ МОЕГО ГОЛОСА:
{samples}

ТЕМА: {topic}

Напиши один пост. Только текст. Без заголовка.
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
        send_tg(TG_ADMIN_ID, "⚠️ Тем в банке не осталось. Добавь новые в таблицу topics.")
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

    result = send_tg(TG_CHANNEL_ID, draft["content"])
    if result.get("ok"):
        cur.execute(
            "UPDATE drafts SET status = 'published', published_at = NOW() WHERE id = %s",
            (draft["id"],)
        )
        conn.commit()
        print(f"Published #{draft['id']}")
    else:
        print(f"Failed: {result}")

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
