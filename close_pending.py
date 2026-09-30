#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v13: закрыть соседние треды, где висит наш уточняющий вопрос по УЖЕ сброшенному
аккаунту — отправить туда ТОТ ЖЕ пароль (не второй сброс!) и снять pending.

Зачем: фронтдеск часто пишет в два треда об одном ученике и подтверждает только в
одном. Второй тред нельзя оставлять в тишине, но и второй сброс делать нельзя —
второй пароль отменил бы первый.

  python3 close_pending.py --upn student.smith@example.com --password Pw-abc123
"""
import os, sys, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib
bot = importlib.import_module("student_pw_bot")

ap = argparse.ArgumentParser()
ap.add_argument("--upn", required=True)
ap.add_argument("--password", required=True)
ap.add_argument("--dry", action="store_true")
a = ap.parse_args()

upn = a.upn.lower()
st = bot.load_state()
pend = st.get("pending_confirm") or {}
targets = [cid for cid, v in pend.items() if (v.get("upn") or "").lower() == upn]
print("тредов с нашим вопросом по %s: %d" % (upn, len(targets)))

for cid in targets:
    thr = bot.thread_messages(cid)
    # письмо, на которое отвечаем: последнее НЕ от админа (запрос отправителя)
    ask = [c for c in thr if not bot.is_admin_reply(c)]
    if not ask:
        print("  в треде нет письма отправителя:", cid[:20], "— пропускаю")
        continue
    last = ask[-1]
    text = ("Hello! %s — the password has been reset as you confirmed. This is the same "
            "password that was sent in the reply to your other email:\n%s Password: %s"
            % (upn, upn, a.password))
    print("  → ответ в тред %s (письмо %s)" % (cid[:20], last.get("receivedDateTime")))
    if a.dry:
        continue
    bot.graph("POST", "/users/%s/messages/%s/reply"
              % (bot.MAILBOX, bot.urllib.parse.quote(last["id"])),
              {"message": {"body": {"contentType": "Text", "content": text}}})
    bot.audit_log("(бот, закрытие соседнего треда)", upn, "ask", "тот же пароль отправлен в соседний тред")
    pend.pop(cid, None)

if not a.dry:
    st["pending_confirm"] = pend
    bot.save_state(st)
    print("pending_confirm после закрытия:", len(pend))
