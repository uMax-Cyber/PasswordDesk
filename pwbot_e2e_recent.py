#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E2E-регрессия на РЕАЛЬНЫХ письмах за последние N часов (по умолчанию 72).
DRY-RUN: решения без действий, без отправок, без сбросов.

Отличие от pwbot_e2e.py: окно считается от текущего времени (та версия была
прибита к 2026-09-14 и перестала видеть свежие письма — грабля 18.09).
Запуск: python3 pwbot_e2e_recent.py [часы]
"""
import sys, os, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib
bot = importlib.import_module("student_pw_bot")

hours = int(sys.argv[1]) if len(sys.argv) > 1 else 72
since = bot.time.strftime("%Y-%m-%dT%H:%M:%SZ", bot.time.gmtime(bot.time.time() - hours * 3600))
url = ("/users/%s/messages?$filter=receivedDateTime ge %s&$orderby=receivedDateTime asc"
       "&$top=200&$select=id,subject,from,receivedDateTime,bodyPreview"
       % (bot.MAILBOX, bot.urllib.parse.quote(since)))
msgs = bot.graph("GET", url)["value"]
test_set = [m for m in msgs
            if ((m.get("from", {}).get("emailAddress", {}).get("address") or "").lower() in bot.TRIGGERS)]
print("окно: с %s (%d ч) | писем в ящике: %d | триггерных: %d" % (since, hours, len(msgs), len(test_set)))
for m in test_set:
    print("   ", m["receivedDateTime"], "|", (m.get("subject") or "(нет темы)")[:44],
          "|", (m.get("bodyPreview") or "")[:52].replace("\n", " "))

for m in test_set:
    bot.unprocess(m["id"])

env = dict(os.environ, PW_BOT_DRY_RUN="1")
r = subprocess.run(["python3", "-u", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "student_pw_bot.py")],
                   capture_output=True, text=True, timeout=1800, env=env)
print("\n========== РЕШЕНИЯ БОТА (DRY-RUN) ==========")
print(r.stdout or "(тихо — все пропущены по правилам)")
if r.stderr:
    print("STDERR:", r.stderr[-400:])
print("EXIT:", r.returncode)
