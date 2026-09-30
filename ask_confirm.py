#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v13: задать отправителю уточняющий вопрос по конкретному аккаунту (вручную).

Нужен, когда заказчик/оператор видит сомнение там, где AI уверен (например,
перестановка имён: «John Smith» ↔ «Petrov Ivan»). Делает ровно то же,
что автоматический путь: reply в тред с человеческим вопросом + запись в
state.pending_confirm — сброс произойдёт ТОЛЬКО после явного подтверждения.

  python3 ask_confirm.py --upn student.smith@example.com --label "John Smith" [--hours 12]
  python3 ask_confirm.py --upn … --label … --msg <messageId>     # конкретное письмо
"""
import os, sys, argparse, re, html as _h
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib
bot = importlib.import_module("student_pw_bot")

ap = argparse.ArgumentParser()
ap.add_argument("--upn", required=True)
ap.add_argument("--label", required=True)
ap.add_argument("--msg", default=None)
ap.add_argument("--hours", type=int, default=12)
ap.add_argument("--dry", action="store_true")
a = ap.parse_args()

directory = bot.fetch_directory()
upn = a.upn.lower()
if upn not in directory:
    print("нет такого аккаунта в каталоге:", a.upn)
    sys.exit(1)

msgs = []
if a.msg:
    m = bot.graph("GET", "/users/%s/messages/%s?$select=id,subject,conversationId,receivedDateTime"
                  % (bot.MAILBOX, urllib.parse.quote(a.msg)))
    msgs = [m]
else:
    since = bot.time.strftime("%Y-%m-%dT%H:%M:%SZ", bot.time.gmtime(bot.time.time() - a.hours * 3600))
    r = bot.graph("GET", ("/users/%s/messages?$filter=receivedDateTime ge %s&$orderby=receivedDateTime asc"
                          "&$top=100&$select=id,subject,from,receivedDateTime,conversationId"
                          % (bot.MAILBOX, urllib.parse.quote(since))))["value"]
    for m in r:
        frm = ((m.get("from", {}).get("emailAddress", {}) or {}).get("address") or "").lower()
        if frm not in bot.TRIGGERS:
            continue
        thr = bot.thread_messages(m.get("conversationId") or "")
        after = [c for c in thr if (c.get("receivedDateTime") or "") > (m.get("receivedDateTime") or "")]
        if any(bot.is_admin_reply(c) for c in after):
            continue          # на письмо уже отвечали
        body = bot.newest_text(_h.unescape(re.sub(r"<[^>]+>", " ",
                   (bot.graph("GET", "/users/%s/messages/%s?$select=body" % (bot.MAILBOX,
                    urllib.parse.quote(m["id"])))["body"] or {}).get("content", ""))))
        if a.label.lower() in body.lower() or upn.split("@")[0] in body.lower():
            msgs.append(m)

print("писем под вопрос:", len(msgs))
question = bot.clarify_question(a.label, [upn], directory)
print("ВОПРОС:", question)
for m in msgs:
    print("  →", m["receivedDateTime"], "|", (m.get("subject") or "")[:50])
if a.dry or not msgs:
    sys.exit(0)

st = bot.load_state()
pend = st.setdefault("pending_confirm", {})
for m in msgs:
    body = {"message": {"body": {"contentType": "Text", "content": question}}}
    bot.graph("POST", "/users/%s/messages/%s/reply" % (urllib.parse.quote(bot.MAILBOX), m["id"]), body)
    pend[m["conversationId"]] = {"upn": upn, "label": a.label, "candidates": [upn],
                                 "asked": bot.time.time(), "asked_ru": bot.reset_when_ru(bot.time.time())}
    bot.audit_log("(ручной запрос заказчика)", upn, "ask", "уточняю у отправителя: %s" % a.label)
    print("отправлен вопрос в тред:", m["conversationId"][:24], "…")
bot.save_state(st)
print("pending_confirm записан:", len(pend))
