#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выравнивание processed-state перед точечным боевым прогоном.

Зачем: E2E/dry-run снимает пометки processed со ВСЕХ триггерных писем окна, и
следующий боевой прогон переиграл бы старые (уже отвеченные) письма — повторные
сбросы и повторные ответы в треды.

Что делает: в окне последних N часов (по умолчанию 64 — как у бота) помечает
processed ВСЕ триггерные письма, КРОМЕ перечисленных в --pending (их оставляет
необработанными, чтобы бот взял ровно их).

Запуск:
  python3 pwbot_state_align.py                       # только показать расклад
  python3 pwbot_state_align.py --apply --pending <id1> <id2>
"""
import os, sys, json, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib
bot = importlib.import_module("student_pw_bot")

ap = argparse.ArgumentParser()
ap.add_argument("--hours", type=int, default=64)
ap.add_argument("--apply", action="store_true")
ap.add_argument("--pending", nargs="*", default=[])
a = ap.parse_args()

since = bot.time.strftime("%Y-%m-%dT%H:%M:%SZ", bot.time.gmtime(bot.time.time() - a.hours * 3600))
url = ("/users/%s/messages?$filter=receivedDateTime ge %s&$orderby=receivedDateTime desc"
       "&$top=200&$select=id,subject,from,receivedDateTime"
       % (bot.MAILBOX, bot.urllib.parse.quote(since)))
msgs = bot.graph("GET", url)["value"]
trig = [m for m in msgs
        if ((m.get("from", {}).get("emailAddress", {}).get("address") or "").lower() in bot.TRIGGERS)]

state = bot.load_state()
proc = set(state.get("processed") or [])
print("окно с %s | триггерных писем: %d | из них без пометки: %d"
      % (since, len(trig), sum(1 for m in trig if m["id"] not in proc)))
for m in trig:
    mark = "PENDING" if m["id"] in a.pending else ("processed" if m["id"] in proc else "no-mark")
    print("   %s | %-9s | %s" % (m["receivedDateTime"], mark, (m.get("subject") or "(нет темы)")[:50]))

if not a.apply:
    print("\n(dry) для применения: --apply --pending <id> …")
    sys.exit(0)

pend = set(a.pending)
for m in trig:
    if m["id"] in pend:
        bot.unprocess(m["id"])
        print("снял пометку (pending):", (m.get("subject") or "")[:50])
    else:
        state = bot.load_state()
        if m["id"] not in set(state.get("processed") or []):
            state.setdefault("processed", []).append(m["id"])
            bot.save_state(state)
            print("пометил processed:", (m.get("subject") or "")[:50])
print("готово")
