import os
import sys
import json
import time
import random
import requests
import psycopg2
from urllib.parse import quote
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ["DATABASE_URL"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]
TG_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_ADMIN_ID = int(os.environ["TELEGRAM_ADMIN_ID"])
TG_CHANNEL_ID = int(os.environ["TELEGRAM_CHANNEL_ID"])
LLM_MODEL = os.environ.get("LLM_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")

CANDIDATES_PER_DAY = 3
TG_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"


def db():
    return psycopg2.connect(DATABASE_URL, sslmode='require')


def tg(method, **kwargs):
    r = requests.post(f"{TG_API}/{method}", json=kwargs, timeout=30)
    return r.json()


def send_tg(chat_id, text, parse_mode="HTML", reply_markup=None):
    chunks = []
    while text:
        if len(text) <= 4000:
            chunks.append(text); break
        split = text.rfind("\n\n", 0, 4000) or 4000
        chunks.append(text[:split]); text = text[split:].lstrip()
    out = []
    for i, c in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": c, "parse_mode": parse_mode,
                   "disable_web_page_preview": True}
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        out.append(tg("sendMessage", **payload))
        time.sleep(0.4)
    return out


def send_tg_photo(chat_id, photo_url, caption):
    if len(caption) > 1024:
        caption = caption[:1020] + "…"
    return tg("sendPhoto", chat_id=chat_id, photo=photo_url,
              caption=caption, parse_mode="HTML")


def call_llm(prompt):
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}",
                 "Content-Type": "application/json"},
        json={"model": LLM_MODEL,
              "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.95, "max_tokens": 1800},
        timeout=240,
    )
    data = r.json()
    if "choices" not in data:
        raise Exception(f"LLM error: {data}")
    return data["choices"][0]["message"]["content"].strip()


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except Exception:
        import re
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise


WRITER_PROMPT = """Ты — автор анонимного Telegram-канала с историями человека, который провёл несколько лет в местах лишения свободы. Пишешь от первого лица, тихо, честно, без морали и без пафоса.

ЗАДАЧА: написать пост на заданную тему. Сгенерируй один вариант.

ТЕМА: {topic}

ТРЕБОВАНИЯ:
- Язык строго русский. Только кириллица, никакой латиницы в русских словах.
- От первого лица, прошедшее время, мужской голос.
- Длина тела поста 1400–2000 знаков (критично).
- Короткие абзацы (2–4 строки), разделены пустой строкой.
- Много конкретных деталей: как пахло, что звучало, что было на столе, во что был одет, что говорили рядом.
- Живой разговорный язык, можно прямую речь.
- Простой жаргон и имена (Саня, Вовка, Димон) — можно. Города — можно.
- Финал — крючок: недосказанность, резкий поворот, короткая фраза. Без морали.
- Смайлы — не используй.

ЗАГОЛОВОК:
- 2–4 слова, с двойным смыслом. Примеры: «Жизнь, о которой мечтают», «Сигареты дороже денег», «Тишина гуще стен».

КАРТИНКА (image_prompt):
- Промпт на английском, 8–15 слов, для атмосферного фото.
- Всегда: dark cinematic photo, no people, realistic, moody.
- Без текста, без блатных символов.

ЧЕГО НЕ ДЕЛАТЬ:
- Не использовать слова «тюрьма», «зона», «камера», «отсидел» напрямую.
- Не использовать штампы: «тюрьма научила», «я исправился».
- Не писать мораль в финале.
- Не выдумывать детали, которых не может быть.

ПРИМЕРЫ (твой стиль, лексика, ритм):
{samples}

ОТВЕТЬ СТРОГО JSON:
{{"title": "...", "body": "...", "image_prompt": "..."}}
"""


def get_samples(cur):
    cur.execute("SELECT content FROM publish_queue ORDER BY RANDOM() LIMIT 3")
    rows = cur.fetchall()
    if not rows:
        return "(примеров нет)"
    return "\n\n---\n\n".join(r["content"][:1500] for r in rows)


def task_write():
    conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""SELECT COUNT(*) AS c FROM ai_drafts
                   WHERE created_at > NOW() - INTERVAL '24 hours' AND status='pending'""")
    if cur.fetchone()["c"] > 0:
        print("Свежие черновики уже есть — пропуск")
        cur.close(); conn.close(); return

    cur.execute("SELECT title FROM publish_queue ORDER BY RANDOM() LIMIT 1")
    topic_row = cur.fetchone()
    topic = topic_row["title"] if topic_row else "Свобода"

    samples = get_samples(cur)

    created = []
    for i in range(CANDIDATES_PER_DAY):
        try:
            raw = call_llm(WRITER_PROMPT.format(samples=samples, topic=topic))
            p = parse_json(raw)
            title = p.get("title", "").strip()
            body = p.get("body", "").strip()
            img = p.get("image_prompt", "").strip()
            if not (title and body and img):
                continue
            cur.execute("""INSERT INTO ai_drafts (topic, title, content, image_prompt, status)
                           VALUES (%s, %s, %s, %s, 'pending') RETURNING id""",
                        (topic, title, body, img))
            did = cur.fetchone()["id"]; conn.commit()
            created.append((did, title, body, img))
        except Exception as e:
            print(f"Error {i+1}: {e}")

    cur.close(); conn.close()

    if not created:
        send_tg(TG_ADMIN_ID, "⚠️ Writer ничего не сгенерил. Проверь лог.")
        return

    for idx, (did, title, body, img) in enumerate(created, 1):
        text = f"<b>Вариант {idx} из {len(created)}</b>\n\n<b>{title}</b>\n\n{body}"
        kb = {"inline_keyboard": [[
            {"text": "✅ Опубликовать", "callback_data": f"ok:{did}"},
            {"text": "📁 Сохранить", "callback_data": f"save:{did}"},
            {"text": "❌ Удалить", "callback_data": f"no:{did}"},
        ]]}
        send_tg(TG_ADMIN_ID, text, reply_markup=kb)

    send_tg(TG_ADMIN_ID, f"📌 Тема дня: <b>{topic}</b>.")


def task_publish():
    """Публикация выключена флагом publishing_enabled."""
    conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT value FROM bot_state WHERE key='publishing_enabled'")
    row = cur.fetchone()
    enabled = row and row["value"] == "true"

    if not enabled:
        print("Публикация выключена (publishing_enabled=false)")
        cur.close(); conn.close(); return

    cur.execute("""SELECT 1 FROM publish_queue
                   WHERE published_at > NOW() - INTERVAL '20 hours' LIMIT 1""")
    if cur.fetchone():
        print("Сегодня уже был пост")
        cur.close(); conn.close(); return

    cur.execute("""SELECT * FROM publish_queue WHERE published_at IS NULL
                   ORDER BY position NULLS LAST, id LIMIT 1""")
    d = cur.fetchone()
    if not d:
        print("Очередь пуста"); cur.close(); conn.close(); return

    img_prompt = d["image_prompt"] or "dark moody cinematic scene, no people, realistic"
    image_url = f"https://image.pollinations.ai/prompt/{quote(img_prompt)}?width=1024&height=576&nologo=true&model=flux"

    caption = f"<b>{d['title']}</b>\n\n{d['content']}"
    send_tg_photo(TG_CHANNEL_ID, image_url, caption)

    cur.execute("UPDATE publish_queue SET published_at=NOW() WHERE id=%s", (d["id"],))
    conn.commit()
    print(f"Published #{d['id']}")
    cur.close(); conn.close()


def task_stats():
    conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT COUNT(*) AS c FROM publish_queue WHERE published_at IS NULL")
    queue = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM ai_drafts WHERE status='pending'")
    pending = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM ai_drafts WHERE status='saved'")
    saved = cur.fetchone()["c"]
    cur.execute("SELECT value FROM bot_state WHERE key='publishing_enabled'")
    enabled = cur.fetchone()["value"]
    msg = (f"📊 <b>Статус</b>\n\n"
           f"Твоих постов в очереди: {queue}\n"
           f"AI-черновиков на проверке: {pending}\n"
           f"AI-постов в банке: {saved}\n"
           f"Публикация: <b>{enabled}</b>")
    send_tg(TG_ADMIN_ID, msg)
    cur.close(); conn.close()


def task_callbacks():
    conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT value FROM bot_state WHERE key='tg_offset'")
    row = cur.fetchone()
    offset = int(row["value"]) if row else 0

    r = requests.get(f"{TG_API}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=30).json()
    if not r.get("ok"):
        print(f"getUpdates error: {r}"); cur.close(); conn.close(); return

    updates = r.get("result", [])
    max_id = offset - 1
    processed = 0

    for u in updates:
        max_id = max(max_id, u["update_id"])
        cb = u.get("callback_query")
        if not cb: continue
        data = cb.get("data", "")
        cb_id = cb["id"]
        msg_id = cb["message"]["message_id"]

        try:
            action, did = data.split(":", 1); did = int(did)
        except Exception:
            continue

        cur.execute("SELECT * FROM ai_drafts WHERE id=%s", (did,))
        d = cur.fetchone()
        if not d:
            tg("answerCallbackQuery", callback_query_id=cb_id, text="Черновик не найден")
            continue

        if action == "ok":
            cur.execute("""INSERT INTO publish_queue (title, content, image_prompt, source, position)
                           VALUES (%s, %s, %s, 'ai',
                           (SELECT COALESCE(MAX(position), 0) + 1 FROM publish_queue))""",
                        (d["title"], d["content"], d["image_prompt"]))
            cur.execute("UPDATE ai_drafts SET status='approved' WHERE id=%s", (did,))
            new_text = f"<b>{d['title']}</b>\n\n{d['content']}\n\n✅ <i>Опубликовать (в очередь)</i>"
            tg("answerCallbackQuery", callback_query_id=cb_id, text="✅ В очередь")
        elif action == "save":
            cur.execute("UPDATE ai_drafts SET status='saved' WHERE id=%s", (did,))
            new_text = f"<b>{d['title']}</b>\n\n{d['content']}\n\n📁 <i>Сохранено в банк AI</i>"
            tg("answerCallbackQuery", callback_query_id=cb_id, text="📁 Сохранено")
        elif action == "no":
            cur.execute("UPDATE ai_drafts SET status='rejected', rejected_at=NOW() WHERE id=%s", (did,))
            new_text = f"<b>{d['title']}</b>\n\n{d['content']}\n\n❌ <i>Удалено</i>"
            tg("answerCallbackQuery", callback_query_id=cb_id, text="❌ Удалено")
        else:
            continue

        conn.commit()
        try:
            tg("editMessageText", chat_id=cb["message"]["chat"]["id"], message_id=msg_id,
               text=new_text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            print(f"edit error: {e}")
        processed += 1

    cur.execute("""INSERT INTO bot_state (key, value, updated_at) VALUES ('tg_offset', %s, NOW())
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
                (str(max_id + 1),))
    conn.commit(); cur.close(); conn.close()
    print(f"Обработано: {processed}, offset={max_id+1}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python agent.py [write|publish|stats|callbacks]"); sys.exit(1)
    t = sys.argv[1]
    if t == "write": task_write()
    elif t == "publish": task_publish()
    elif t == "stats": task_stats()
    elif t == "callbacks": task_callbacks()
    else: print(f"Unknown: {t}")
