#!/usr/bin/env python3
"""STUDENT PASSWORD BOT v8 — AI-поиск учеников (2026-09-14).
Ключевое изменение по требованию заказчика: имена/фамилии НЕ парсим регулярками —
AI (nemotron) читает письмо + весь каталог O365 и точно находит пользователя,
без дублей и без ложных целей из подписей/цитат/писем «не про пароли».

Схема авторизации (v7, forever):
 - app-токен (client_credentials, своё приложение, долгоживущий секрет) — почта,
   поиск, создание юзеров + A3, отправка ответов;
 - delegated-токен admin@example.com (rolling refresh через своё приложение,
   admin-consent AllPrincipals) — только resetPassword;
 - смерть delegated → автозапуск device-code bootstrap + код заказчику в Telegram.

Поток обработки письма:
 1. гейт релевантности (password/credential/account/…);
 2. AI: письмо + каталог → JSON-решения [reset|create|unlock_keep];
    конвенция школы: «голое имя ученика» = просьба сбросить пароль;
 3. точные UPN из письма добавляются, только если AI их не решил;
 4. валидация кодом: reset/unlock — UPN обязан быть в каталоге; create —
    дубликат-чек (difflib ≥0.85 по displayName / совпадение mailNickname)
    → при близком совпадении понижаем до reset;
 5. AI недоступен → работаем ТОЛЬКО по точным UPN (создание запрещено) + алерт;
 6. ответ — настоящий reply в тред (POST /messages/{id}/reply).

v12 (2026-09-18, инцидент «фронтдеск написал дважды и оба раза в тишину»):
ПРАВИЛО ЗАКАЗЧИКА — уже сброшенный аккаунт НЕ сбрасывать повторно, если автор не
попросил его именно в последних (свежих) сообщениях и мы ещё не ответили сбросом по
нему. Семантику решает AI (v11), код страхует два случая: аккаунт, всплывший ТОЛЬКО
из цитаты; аккаунт, по которому сброс отправлен только что (cooldown — иначе два
письма в разных тредах дали бы два разных пароля, и первый стал бы нерабочим).
В обоих случаях бот ОБЯЗАН ответить в тред пояснением — молчаливый SKIP выглядел
как «бот умер». Плюс русские заголовки цитаты в newest_text (От:/Кому:/Дата:/Тема:)."""
import json, re, time, sys, os, secrets, string, subprocess, difflib, calendar
import urllib.request, urllib.parse, urllib.error

# ---- конфигурация: всё берётся из окружения (см. README.md и .env.example) ----
HOME_DIR   = os.environ.get("PW_BOT_HOME", "/opt/password-bot")
TENANT     = os.environ.get("O365_TENANT", "")          # tenant id (GUID) или contoso.onmicrosoft.com
APP_ID     = os.environ.get("O365_APP_ID", "")          # app registration: client_credentials + delegated
MAILBOX    = os.environ.get("PW_BOT_MAILBOX", "")       # ящик-робот, напр. servicedesk@example.com
UPN_DOMAIN = os.environ.get("PW_BOT_UPN_DOMAIN", "")    # домен для новых учёток, напр. example.com
# кому бот отвечает: адреса через запятую
TRIGGERS = {s.strip().lower() for s in os.environ.get("PW_BOT_TRIGGERS", "").split(",") if s.strip()}
PW_PREFIX  = os.environ.get("PW_BOT_PW_PREFIX", "Pw-")  # префикс генерируемого пароля
TELEGRAM_CHAT = os.environ.get("PW_BOT_TG_CHAT", "")    # куда шлём алерты (пусто = не шлём)
APP_CREDS  = os.path.join(HOME_DIR, "state/o365_app_creds.json")
TOKENS     = os.path.join(HOME_DIR, "state/o365_tokens.json")
BOOTSTRAP  = os.path.join(HOME_DIR, "scripts/o365_device_bootstrap.py")
AUTH_JSON  = os.path.join(HOME_DIR, "auth.json")        # пул токенов LLM-провайдера
ENV_FILE   = os.path.join(HOME_DIR, ".env")             # здесь лежит TELEGRAM_BOT_TOKEN
STATE      = os.path.join(HOME_DIR, "state/o365_bot_state.json")
AUDIT_LOG  = os.path.join(HOME_DIR, "state/o365_pw_audit.log")
ALERT_STATE= os.path.join(HOME_DIR, "state/o365_bot_alert.json")
LLM_MODELS = os.environ.get(
    "PW_BOT_LLM_MODELS", "nvidia/nemotron-3-ultra-550b-a55b,deepseek-ai/deepseek-v4.1-flash").split(",")
LLM_BASE = os.environ.get("PW_BOT_LLM_BASE", "https://integrate.api.nvidia.com/v1")
DELEG_SCOPES = "offline_access Mail.ReadWrite Mail.Send User.ReadWrite.All UserAuthenticationMethod.ReadWrite.All"
A3_SKU = "18250162-5d87-4436-a834-d795c15c80f3"         # Microsoft 365 A3 for students (публичный SKU)
DRY_RUN = os.environ.get("PW_BOT_DRY_RUN") == "1"
TEST_MODE = os.environ.get("PW_BOT_TEST") == "1"
LOG = print

def tg(msg):
    try:
        tok = ""
        for line in open(ENV_FILE):
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                tok = line.split("=",1)[1].strip().strip('"').strip("'")
        urllib.request.urlopen(urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % tok,
            data=urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": msg[:3500]}).encode()), timeout=20)
    except Exception:
        pass

def alert(key, msg):
    try:
        a = json.load(open(ALERT_STATE))
    except Exception:
        a = {}
    if time.time() - a.get(key, 0) < 3600:
        return
    a[key] = time.time()
    json.dump(a, open(ALERT_STATE, "w"))
    tg("🚨 PW-BOT: " + msg)

# ---- токены (v7) ----
_app = {"token": None, "exp": 0}
def token_app():
    if _app["token"] and _app["exp"] > time.time() + 60:
        return _app["token"]
    c = json.load(open(APP_CREDS))
    data = urllib.parse.urlencode({"client_id": c["client_id"], "client_secret": c["client_secret"],
        "grant_type": "client_credentials", "scope": "https://graph.microsoft.com/.default"}).encode()
    r = json.loads(urllib.request.urlopen(urllib.request.Request(
        "https://login.microsoftonline.com/%s/oauth2/v2.0/token" % TENANT, data=data), timeout=30).read())
    _app["token"] = r["access_token"]; _app["exp"] = time.time() + r.get("expires_in", 3600) - 120
    return r["access_token"]

_del = {"token": None, "exp": 0}
def token_del(allow_bootstrap=True):
    if _del["token"] and _del["exp"] > time.time() + 60:
        return _del["token"]
    try:
        t = json.load(open(TOKENS))
        cid = t.get("client_id") or APP_ID
    except Exception:
        raise RuntimeError("нет o365_tokens.json — нужен bootstrap")
    if t.get("expires_at", 0) > time.time() + 60:
        _del["token"] = t["access_token"]; _del["exp"] = t["expires_at"]
        return t["access_token"]
    data = urllib.parse.urlencode({"client_id": cid,
        "grant_type": "refresh_token", "refresh_token": t["refresh_token"],
        "scope": DELEG_SCOPES}).encode()
    try:
        r = json.loads(urllib.request.urlopen(urllib.request.Request(
            "https://login.microsoftonline.com/%s/oauth2/v2.0/token" % TENANT, data=data), timeout=30).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        if allow_bootstrap:
            alert("delegated_dead", "делегированный токен умер (%s). Запускаю device-code — подтверди код в Telegram. "
                  "Почта и создание работают на app-токене." % body[:80])
            try:
                subprocess.Popen(["python3", BOOTSTRAP],
                                 stdout=open("/tmp/pwbot_bootstrap.log", "w"), stderr=subprocess.STDOUT)
            except Exception:
                pass
        raise RuntimeError("delegated refresh fail: %s" % body)
    r["expires_at"] = time.time() + r.get("expires_in", 3600)
    r["client_id"] = cid
    json.dump(r, open(TOKENS, "w"), indent=1)
    _del["token"] = r["access_token"]; _del["exp"] = r["expires_at"]
    return r["access_token"]

def graph(method, path, body=None, delegated=False):
    tok = token_del() if delegated else token_app()
    h = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
    req = urllib.request.Request("https://graph.microsoft.com/v1.0" + path.replace(" ", "%20").replace(chr(39), "%27"),
        data=json.dumps(body).encode() if body is not None else None, headers=h, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=30)
        raw = r.read()
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")[:300]
        raise RuntimeError("HTTP %s %s: %s" % (e.code, path, err))

def gen_password():
    # v9 (правило заказчика): пароль всегда ПРОСТОЙ: PW_PREFIX + 6 символов.
    # Без спецсимволов и путающихся пар (0/o, 1/l/i) — легко набрать ученику.
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return PW_PREFIX + "".join(secrets.choice(alphabet) for _ in range(6))

# ---- LLM (nemotron, ретраи + фолбэк-модель) ----
def llm_key():
    return json.load(open(AUTH_JSON))["credential_pool"]["nvidia"][0]["access_token"]

def llm_json(prompt, attempts=6):
    """Строгий JSON от модели. Ретраи по 503/таймаутам, фолбэк на вторую модель.
    Возвращает dict или бросает RuntimeError."""
    last_err = ""
    for model in LLM_MODELS:
        for i in range(attempts):
            try:
                body = {"model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0, "max_tokens": 2000}
                req = urllib.request.Request(LLM_BASE + "/chat/completions",
                    data=json.dumps(body).encode(),
                    headers={"Authorization": "Bearer " + llm_key(), "Content-Type": "application/json"})
                r = json.loads(urllib.request.urlopen(req, timeout=120).read())
                txt = r["choices"][0]["message"]["content"].strip()
                txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
                m = re.search(r"\{.*\}", txt, re.S)
                if not m:
                    raise ValueError("no JSON in reply: " + txt[:120])
                return json.loads(m.group(0))
            except urllib.error.HTTPError as e:
                last_err = "HTTP %s" % e.code
                if e.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError("LLM %s: %s" % (model, last_err))
            except Exception as e:
                last_err = str(e)[:100]
            time.sleep(3 + i * 3)
    raise RuntimeError("LLM недоступен: %s" % last_err)

# ---- каталог O365 ----
def fetch_directory():
    """{upn_lower: {"upn":…, "display":…, "first":…, "last":…, "nick":…}}"""
    out, url = {}, "/users?$select=userPrincipalName,displayName,givenName,surname&$top=999"
    while url:
        r = graph("GET", url)
        for u in r.get("value", []):
            upn = (u.get("userPrincipalName") or "").lower()
            if upn:
                out[upn] = {"upn": u.get("userPrincipalName"),
                            "display": u.get("displayName") or "",
                            "first": u.get("givenName") or "",
                            "last": u.get("surname") or "",
                            "nick": upn.split("@")[0]}
        url = None
        nxt = r.get("@odata.nextLink")
        if nxt:
            url = nxt.replace("https://graph.microsoft.com/v1.0", "")
    return out

# ---- AI-резолвер целей ----
PROMPT = """You are the decision engine of a school IT password bot. You receive:
(1) the NEW MESSAGE you must process (clearly delimited below), (2) the HISTORY of the
thread BEFORE it (context only), (3) the directory of existing O365 accounts, (4) accounts
recently reset by the bot (already handled).

Your task: understand what the sender is asking IN THE NEW MESSAGE and decide the actions.

RULES:
- ACTIONS COME ONLY FROM THE NEW MESSAGE (the first delimited block). The HISTORY section
  is context only: use it to resolve names/UPNs and understand what was already done, but
  NEVER produce actions from history. "Yes"/"them"/"thanks"/praise in the new message
  without an explicit request = no action (not_a_password_request: true).
- A status-check request lists accounts and asks to check/verify them — return action
  "check" for each listed account (report-only).
- SCHOOL CONVENTION: teachers/frontdesk often send JUST a student name (or several names)
  as the only content. That means: reset that student's password. A bare student name IS
  a password reset request. NEVER classify such an email as not_a_password_request.
- Actions:
  * "reset" — explicit password reset request (bare name counts). Match to an existing
    account from the directory: handle typos, transliteration (Jakhongir/Jahongir,
    apostrophes o`/ʻ), swapped name order, short names.
  * "check" — status check ("can we check these ones?", "are they active?"). Report-only,
    never reset on a check request even if UPNs are listed.
  * "unlock_keep" — account locked, sender asks to keep the same password AND provides it.
  * "create" — clearly a new student not in the directory even fuzzily. If any directory
    entry is a close match (same surname + similar first name, similar mailNickname) —
    reset the existing account instead, NEVER create a duplicate.
- If an account was recently reset (see list) and the new message does not explicitly
  ask for it again — do not include it.
- TIME AND DATE (v13.1, правило заказчика): every message carries its date/time in
  [brackets]. ALWAYS read them. The dialogue with the sender is what you judge by, and
  the NEWEST message decides. A request that appears only in HISTORY was already handled —
  never act on it again; if the history shows the account was already answered, do not
  repeat the action.
- Never process an email that has already been answered: if the newest message needs no
  action (thanks, acknowledgement, signature, schedule talk), return an empty targets list.
- IGNORE signatures ("Get Outlook", "Kind regards"), quoted reply headers (From:/To:/Cc:),
  staff names in cover schedules/meeting chatter.

Return STRICT JSON, nothing else:
{"targets": [{"action": "reset"|"create"|"unlock_keep"|"check",
  "upn": "<existing upn, for reset/unlock_keep/check>",
  "first": "<for create>", "last": "<for create>", "password": "<for unlock_keep>",
  "label": "<the student's name EXACTLY as the sender wrote it in the new message>",
  "confidence": "high"|"low",
  "candidates": ["<upn>", "…"],
  "confirmed": true|false,
  "reason": "<short>"}],
 "not_a_password_request": false}

IDENTITY RULES (v13 — правило заказчика «сомневаешься — спроси отправителя»):
- "confidence": "low" ONLY when the person named in the NEW message cannot be matched to
  exactly ONE directory account: several plausible students, or the match is doubtful.
  Put the plausible accounts in "candidates". Never guess a student in such a case.
- "confirmed": true ONLY when the NEW message is a clear positive answer to OUR PENDING
  QUESTION below (a plain "yes", "correct", "that one", "да", "верно"). A thank-you,
  a signature, "ok", or anything vague is NOT a confirmation — never set it then.

INPUT:
%s

DIRECTORY (upn | First Last):
%s

RECENTLY RESET (already handled, do not repeat without explicit new request):
%s

PENDING QUESTION (we already asked the sender about this; only a clear positive answer
counts as confirmation, and it may cover ONLY these accounts):
%s"""

def newest_text(text):
    """v10.9: только СВЕЖАЯ часть письма — до первого маркера цитаты
    (From:/Get Outlook/-----Original Message/On ... wrote/Отправлено).
    Цитированная история не должна влиять на решения (согласие, цели).
    v12: добавлены РУССКИЕ заголовки цитаты (От:/Кому:/Копия:/Дата:/Тема:) —
    русский Outlook втягивал цитату в «свежий» текст (грабля 18.09)."""
    part = re.split(r"\bFrom:|Get\s+Outlook|-----Original Message|On .+ wrote:|Отправлено"
                    r"|^\s*(?:От|Кому|Копия|Дата|Тема)\s*:",
                    text or "", 1, re.I | re.M)[0]
    return part.strip()

# v12: повторный сброс — семантику решает AI (v11), код страхует только крайние случаи.
RESET_COOLDOWN = 3600                        # сек: «мы уже ответили сбросом по этому аккаунту» (тот же эпизод)
BLOCK_NOTE_MARK = "already been reset"       # маркер нашего пояснения в треде (п. is_bot_reply)
ASK_NOTE_MARK = "Just to be sure before we reset"   # маркер уточняющего вопроса (v13)

def referenced_in_new_text(user, fresh_low):
    """v12: аккаунт РЕАЛЬНО упомянут в свежей части письма (UPN, ник, display или
    фамилия + любое из имён). Отличает настоящий повторный запрос от аккаунта,
    всплывшего только из цитаты (инцидент v10.8: цитаты сбрасывали чужие учётки)."""
    t = fresh_low or ""
    if not t:
        return False
    upn = (user.get("userPrincipalName") or "").lower()
    if upn and upn in t:
        return True
    nick = upn.split("@")[0]
    if len(nick) > 2 and nick in t:
        return True
    # v13.4: имена собираем из displayName + givenName + surname. В каталоге displayName
    # часто слитный («muhammadaminmamurjonov») — разбиение только по нему теряло фамилию,
    # и гвард объявлял свежий запрос «аккаунтом из цитаты» (ложный блок 30.09).
    name_parts = " ".join([user.get("displayName") or "", user.get("givenName") or "",
                           user.get("surname") or ""]).lower()
    words = {w for w in re.split(r"[\s.,'`’-]+", name_parts) if len(w) > 2}
    if not words:
        return False
    if all(w in t for w in words):
        return True
    # «Ivan Petrov» в письме часто «Petrov Ivan»: фамилия + любое имя
    surname = (user.get("surname") or "").lower()
    if surname and surname in words and surname in t and any(w in t for w in words if w != surname):
        return True
    # v13.4: опечатки и транслитерация (Mamarjonov ↔ Mamurjonov). Совпадением считается
    # почти-совпавшая фамилия И присутствие имени ученика рядом — цитаты сюда не попадают
    # (в fresh_low только свежая часть письма, см. newest_text).
    if surname:
        text_words = {w for w in re.split(r"[\s.,'`’»«()\-]+", t) if len(w) > 3}
        near_surname = any(difflib.SequenceMatcher(None, surname, w).ratio() >= 0.85
                           for w in text_words)
        first = (user.get("givenName") or "").lower()
        near_first = bool(first) and (first in t or any(
            difflib.SequenceMatcher(None, first, w).ratio() >= 0.85 for w in text_words))
        if near_surname and near_first:
            return True
    return False

def local_hm(iso):
    """Локальное (Ташкент) время письма из ISO-строки Graph — AI и логи должны видеть
    и дату, и местное время (правило заказчика: «всегда учитывай время и дату писем»)."""
    try:
        ts = calendar.timegm(time.strptime((iso or "")[:19], "%Y-%m-%dT%H:%M:%S"))
        lt = time.localtime(ts)
        return "%d %s, %02d:%02d" % (lt.tm_mday, MONTHS_RU[lt.tm_mon-1], lt.tm_hour, lt.tm_min)
    except Exception:
        return iso or "?"

def reset_when_ru(ts):
    """Локальное время последнего сброса словами — для пояснения в тред."""
    lt = time.localtime(ts)
    return "%d %s, %02d:%02d" % (lt.tm_mday, MONTHS_RU[lt.tm_mon-1], lt.tm_hour, lt.tm_min)

# v13 (правило заказчика, 18.09): «сомневаешься в том, кого сбрасывать — спроси
# отправителя письмом, человеческим языком; сбрасывай только после явного "да"».
# Без формул вида «X = Y@example.com» — в школе так не читают.
CONFIRM_WORDS = re.compile(
    r"\b(yes|yeah|yep|correct|right|that'?s (him|her|the one|right)|exactly|confirmed?|"
    r"affirmative|да|верно|ага|именно|подтверждаю|той самый|тот самый|это он|это она)\b", re.I)

def clarify_question(label, cands, directory):
    """Человеческий вопрос отправителю: однофамильцы/перестановка имён — уточняем.
    label — как автор назвал ученика, cands — кого мы нашли в каталоге."""
    people = []
    for c in cands:
        v = directory.get((c or "").lower())
        if v:
            people.append("%s (%s)" % (v["display"], v["upn"]))
    if len(people) == 1:
        return ("Hello! Just to be sure before we reset the password: is \"%s\" the same "
                "student as %s? Please confirm and we will send the new password right away."
                % (label, people[0]))
    if people:
        return ("Hello! Just to be sure before we reset the password: you asked about \"%s\". "
                "We have %s — which student do you mean? Please confirm and we will send the "
                "new password right away." % (label, "; ".join(people)))
    return ("Hello! Just to be sure before we reset the password: could you please send the "
            "student's full name and class for \"%s\"?" % label)

def exact_upn_targets(text, directory, action="reset"):
    """Точные UPN из текста письма. v10.3: действие определяет контекст —
    reset по умолчанию, но для check-запросов (фолбэк без AI) — check."""
    targets, seen = [], set(TRIGGERS) | {MAILBOX}
    for m in re.findall(r"[A-Za-zÀ-ÿʻ`'.-]+@[A-Za-z0-9.-]+\.uz", text or ""):
        m = m.strip(".,;:()<>'\"").lower()
        if m in seen or m not in directory:
            continue
        seen.add(m)
        targets.append({"action": action, "upn": directory[m]["upn"],
                        "reason": "точный UPN из письма"})
    return targets

RESET_WORDS = re.compile(r"reset|new password|new details|сброс|новый парол", re.I)

def ai_resolve(subject, text, directory, latest_body=None, thread_ctx=None, recent_resets=None,
               pending=None):
    """Возвращает (targets, ai_raw). v11: ВСЕ семантические решения — AI (структурированный
    тред + каталог + recently reset). Скрипт не решает, «о чём письмо». Фолбэк при
    LLM-отказе — только точные UPN из свежей части.
    v13: AI помечает неуверенную цель (confidence=low + candidates) и подтверждение
    нашего уточняющего вопроса (confirmed=true) — заказчик: «сомневаешься — спроси»."""
    dir_lines = "\n".join("%s | %s" % (v["upn"], v["display"]) for v in directory.values())
    rr = "\n".join(recent_resets) if recent_resets else "(none)"
    pend_line = "(none)"
    if pending:
        pend_line = "asked \"%s\" → %s (%s), asked at %s" % (
            pending.get("label", "?"), directory.get(pending.get("upn", ""), {}).get("display", "?"),
            pending.get("upn", "?"), pending.get("asked_ru", "?"))
    ai = llm_json(PROMPT % ((thread_ctx or ((subject or "") + "\n" + text))[:12000], dir_lines, rr, pend_line))
    targets = []
    seen = set(TRIGGERS) | {MAILBOX}
    for t in ai.get("targets", []):
        act = t.get("action")
        if act == "create":
            first, last = (t.get("first") or "").strip(), (t.get("last") or "").strip()
            if not first or not last:
                continue
            # дубликат-чек кодом (не верим AI на слово)
            cand = "%s %s" % (first, last)
            nick = ("%s.%s" % (first, last)).lower().replace("ʻ", "").replace("`", "")
            for v in directory.values():
                ratio = difflib.SequenceMatcher(None, cand.lower(),
                    (v["display"] or cand).lower()).ratio()
                if ratio >= 0.85 or nick == v["nick"] or (last.lower() == (v["last"] or "").lower()
                        and difflib.SequenceMatcher(None, first.lower(), (v["first"] or first).lower()).ratio() >= 0.8):
                    targets.append({"action": "reset", "upn": v["upn"],
                                    "reason": "AI:create→reset (дубликат %s)" % v["upn"]})
                    break
            else:
                targets.append({"action": "create", "first": first, "last": last,
                                "reason": t.get("reason", "AI create")})
        elif act in ("reset", "unlock_keep", "check"):
            upn = (t.get("upn") or "").lower()
            if upn in directory and upn not in seen and upn != MAILBOX:
                seen.add(upn)
                t2 = {"action": act, "upn": directory[upn]["upn"], "reason": t.get("reason", "AI")}
                if act == "unlock_keep" and t.get("password"):
                    t2["password"] = t["password"]
                # v13: признак неуверенности и подтверждение уточняющего вопроса
                if t.get("confidence"):
                    t2["label"] = (t.get("label") or "").strip()
                if (t.get("confidence") or "").lower() == "low":
                    t2["confidence"] = "low"
                    t2["candidates"] = [c for c in (t.get("candidates") or []) if isinstance(c, str)]
                if t.get("confirmed"):
                    t2["confirmed"] = True
                targets.append(t2)
    # v11: филла нет — семантика целиком у AI. Точные UPN из свежей части используются
    # только в фолбэке main() при LLM-отказе.
    return targets, ai

# ---- история треда (v8.1) ----
def thread_messages(conv_id):
    """Все письма треда по chronological. Graph не даёт $filter+$orderby вместе —
    кодируем conversationId и сортируем на клиенте."""
    cid = urllib.parse.quote(conv_id, safe="")
    r = graph("GET", "/users/%s/messages?$filter=conversationId eq '" % urllib.parse.quote(MAILBOX)
              + cid + "'&$top=50&$select=subject,body,bodyPreview,from,receivedDateTime")
    return sorted(r.get("value", []), key=lambda x: x.get("receivedDateTime") or "")

def is_admin_reply(c):
    f = (c.get("from", {}).get("emailAddress", {}).get("address") or "").lower()
    return f == MAILBOX or (f.startswith("/o=exchangelabs") and f.endswith("-admin"))

BOT_REPLY_MARKERS = ("Password:", "Account check results", "Correction:", "\u2014 account",
                     BLOCK_NOTE_MARK, ASK_NOTE_MARK)

def is_bot_reply(c):
    """Ответ отправлен ботом/скриптом (Graph): узнаваем по маркерам содержимого —
    Graph-отправки в thread_messages отображаются как admin@example.com, отправитель
    не различим. Человеческие ответы (Outlook) таких маркеров не содержат."""
    prev = (c.get("bodyPreview") or "")[:300]
    return any(mk in prev for mk in BOT_REPLY_MARKERS)

def already_answered(thread, trigger_dt):
    """Правило заказчика: если admin уже отвечал в треде ПОСЛЕ этого письма —
    бот не вмешивается. v11.3: учитываются только ЧЕЛОВЕЧЕСКИЕ ответы (без
    бот-маркеров); бот-ответы трекаются через processed-state и не блокируют
    более новые письма (класс «потерянный запрос»)."""
    for c in thread:
        if (c.get("receivedDateTime") or "") > trigger_dt and is_admin_reply(c) and not is_bot_reply(c):
            return True
    return False

# ---- проверка пароля и аудит (v9) ----
MONTHS_RU = ["января","февраля","марта","апреля","мая","июня","июля","августа",
             "сентября","октября","ноября","декабря"]
ACTION_RU = {"reset": "сброс пароля", "create": "создание учётки + A3",
             "unlock_keep": "разблокировка (пароль сохранён)", "check": "проверка статуса",
             "ask": "уточняю у отправителя", "hold": "жду подтверждения"}

def verify_password(upn, pwd, attempts=4):
    """Проверка работоспособности пароля: ROPC-логин от имени ученика.
    AAD-репликация занимает секунды — ретраи с паузами 5/10/15с.
    True = работает, False = не работает, None = проверка недоступна (ROPC заблокирован)."""
    delays = [0, 5, 10, 15, 20][:attempts]
    last = None
    for d in delays:
        if d:
            time.sleep(d)
        try:
            data = urllib.parse.urlencode({"client_id": APP_ID, "grant_type": "password",
                "username": upn, "password": pwd, "scope": "openid"}).encode()
            urllib.request.urlopen(urllib.request.Request(
                "https://login.microsoftonline.com/%s/oauth2/v2.0/token" % TENANT, data=data), timeout=30)
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if "AADSTS50126" in body or ("invalid_grant" in body and "password" in body.lower()):
                last = False
                continue  # возможно репликация — пробуем ещё
            return None  # ROPC заблокирован политикой
        except Exception:
            return None
    return last

def audit_log(sender, target, action, extra=""):
    """Журнал сбросов: кто / когда (дата словами, лок. время) / что сделал.
    v12: в DRY_RUN в боевой журнал НЕ пишем — прогоны E2E засоряли аудит
    несуществующими сбросами (грабля 18.09: строки «сброс» за 12:13 от dry-run)."""
    lt = time.localtime()
    when = "%d %s %d, %02d:%02d" % (lt.tm_mday, MONTHS_RU[lt.tm_mon-1], lt.tm_year, lt.tm_hour, lt.tm_min)
    line = "%s | %s | запросил: %s | %s%s" % (when, target, sender,
                                              ACTION_RU.get(action, action),
                                              (" | " + extra) if extra else "")
    if DRY_RUN:
        return line
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    return line

def guess_activity(last_signin, enabled):
    """v10.4: мои выводы по активности (подсказка заказчика: в конце — догадки)."""
    import datetime
    if not enabled:
        return "account disabled"
    if last_signin == "никогда":
        return "never signed in — password likely lost or account unused"
    try:
        dt = datetime.datetime.fromisoformat(str(last_signin).replace("Z", "+00:00"))
        days = (datetime.datetime.now(datetime.timezone.utc) - dt).days
        if days <= 14:
            return "online recently, actively using the account"
        if days <= 60:
            return "inactive for %d days" % days
        return "inactive for %d days — password possibly lost" % days
    except Exception:
        return "no sign-in data"

def check_account(upn):
    """Статус учётки: активна ли + последний вход (v10.3; v10.6: signInActivity
    доступен только по id, не по UPN — «Get By Key only supports UserId»)."""
    try:
        u0 = graph("GET", "/users/%s?$select=id,displayName,accountEnabled" % urllib.parse.quote(upn))
        last = "нет данных"
        try:
            u1 = graph("GET", "/users/%s?$select=signInActivity" % u0["id"])
            last = ((u1.get("signInActivity") or {}).get("lastSignInDateTime") or "никогда")
        except Exception:
            pass
        return {"upn": upn, "display": u0.get("displayName"), "enabled": u0.get("accountEnabled"), "last_signin": last}
    except Exception:
        return None

# ---- операции ----
def find_user_by_upn(upn):
    # v12: тянем displayName/givenName/surname — их требует referenced_in_new_text
    # (иначе «John Smith» неотличим от упоминания в цитате; грабля 18.09).
    try:
        return graph("GET", "/users/%s?$select=id,userPrincipalName,displayName,givenName,surname"
                     % urllib.parse.quote(upn))
    except Exception:
        return None

def reset_password(user, fixed_pwd=None):
    # v10: единый PATCH passwordProfile (app-токен, право User-PasswordProfile.ReadWrite.All).
    # Старый resetPassword (delegated) ставил флаг «пароль истёк» (AADSTS50055) — известное
    # ограничение API; PATCH passwordProfile ставит пароль чисто: force=false, never-expired.
    pwd = fixed_pwd or gen_password()
    graph("PATCH", "/users/" + user["id"],
          {"passwordProfile": {"password": pwd, "forceChangePasswordNextSignIn": False},
           "passwordPolicies": "DisablePasswordExpiration"})
    return pwd

def create_student(first, last):
    upn = ("%s.%s@" + UPN_DOMAIN) % (first.lower().replace("ʻ", "").replace("`", ""), last.lower().replace("ʻ", "").replace("`", ""))
    pwd = gen_password()
    body = {
        "accountEnabled": True,
        "displayName": "%s %s" % (first, last),
        "givenName": first, "surname": last,
        "mailNickname": upn.split("@")[0],
        "userPrincipalName": upn,
        "usageLocation": "UZ",
        "passwordProfile": {"password": pwd, "forceChangePasswordNextSignIn": False},
        "passwordPolicies": "DisablePasswordExpiration"}
    u = graph("POST", "/users", body)
    graph("POST", "/users/%s/assignLicense" % u["id"],
          {"addLicenses": [{"skuId": A3_SKU, "disabledPlans": []}], "removeLicenses": []})
    return u, upn, pwd

# ---- главный цикл ----
def load_state():
    try: return json.load(open(STATE))
    except Exception: return {"processed": []}

def save_state(s):
    # v10.2: merge с текущим файлом — защита от гонки параллельных прогонов (cron vs ручной).
    # v10.5: явные удаления (s["removed"]) больше не теряются — merge вычитает их.
    removed = set(s.pop("removed", []))
    try:
        cur = json.load(open(STATE))
        cur_ids = set(cur.get("processed", []))
        for mid in s.get("processed", []):
            cur_ids.add(mid)
        s["processed"] = list(cur_ids - removed)
    except Exception:
        pass
    json.dump(s, open(STATE, "w"), indent=1)

def unprocess(mid):
    """Снять пометку processed (для ручной перепоследовательной обработки).
    v10.5: прямой атомарный рерайт — merge в save_state не должен возвращать id."""
    st = load_state()
    st["processed"] = [x for x in st.get("processed", []) if x != mid]
    st.pop("removed", None)
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1)
    import os
    os.replace(tmp, STATE)

def main():
    state = load_state()
    # v10.1: окно 64 ч — bot ходит раз в 10 мин только в рабочие часы;
    # утренний запуск понедельника подбирает письма выходных (дубли отсекает processed-state)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 64 * 3600))
    try:
        msgs = graph("GET", "/users/%s/messages?$filter=receivedDateTime ge %s&$orderby=receivedDateTime desc&$top=100&$select=id,subject,from,bodyPreview,conversationId,receivedDateTime" % (urllib.parse.quote(MAILBOX), now))
    except Exception as e:
        LOG("Graph error:", e)
        alert("graph_dead", "не могу читать почту app-токеном: %s" % str(e)[:150])
        return
    for m in msgs.get("value", []):
        frm = (m.get("from", {}).get("emailAddress", {}).get("address") or "").lower()
        if frm not in TRIGGERS: continue
        if m["id"] in state["processed"]: continue
        full = graph("GET", "/users/%s/messages/%s?$select=subject,body,conversationId,from" % (urllib.parse.quote(MAILBOX), m["id"]))
        import html as _h
        body_text = _h.unescape(re.sub(r"<[^>]+>", " ", full.get("body", {}).get("content", "")))
        text_all = full.get("subject", "") + "\n" + body_text
        trigger_dt = m.get("receivedDateTime") or ""
        try:
            thread = thread_messages(full["conversationId"])
            # v8.1: письмо уже обработано человеком (есть ответ админа после него) — не вмешиваемся
            if already_answered(thread, trigger_dt):
                state["processed"].append(m["id"]); save_state(state)
                LOG("SKIP (в треде уже есть ответ админа после письма):", full.get("subject", "")[:50])
                continue
            if len(thread) > 1:
                parts = []
                for c in thread:
                    ct = _h.unescape(re.sub(r"<[^>]+>", " ", c.get("body", {}).get("content", "")))
                    parts.append(c.get("subject", "") + "\n" + ct)
                text_all = "\n---\n".join(parts)
        except Exception:
            pass
        # v11: гейт релевантности удалён — «о чём письмо» решает AI
        # v11.1: NEW MESSAGE отдельным блоком; HISTORY — только письма ДО триггерного
        # (более поздние исключаются: при догоне пропущенных они сбивали AI)
        thread = thread_messages(full["conversationId"])
        trigger_dt = m.get("receivedDateTime") or ""
        def _ctx(c):
            who = "admin (bot)" if is_admin_reply(c) else (c.get("from", {}).get("emailAddress", {}).get("address") or "?")
            body_c = newest_text(_h.unescape(re.sub(r"<[^>]+>", " ", (c.get("body") or {}).get("content", ""))))
            if not body_c:
                body_c = (c.get("subject") or "")[:80]
            return "[%s | %s] From: %s | %s\n%s" % (c.get("receivedDateTime") or "?", local_hm(c.get("receivedDateTime")),
                                                who, (c.get("subject") or "")[:60], body_c[:1500])
        newer = [c for c in thread if (c.get("receivedDateTime") or "") > trigger_dt and not is_admin_reply(c)]
        older = [c for c in thread if (c.get("receivedDateTime") or "") < trigger_dt]
        thread_ctx = "NEW MESSAGE TO PROCESS:\n%s\n\n=== HISTORY BEFORE (context only, no actions from it) ===\n%s" % (
            _ctx({"receivedDateTime": trigger_dt, "from": m.get("from"), "subject": full.get("subject"),
                  "body": full.get("body")}),
            "\n---\n".join(_ctx(c) for c in older) or "(none)")
        recent_resets = sorted((state.get("reset_log") or {}).keys())
        # v13: наш уточняющий вопрос в этом треде (если задавали) — уходит в контекст AI
        pending = (state.get("pending_confirm") or {}).get(full["conversationId"])
        # AI-резолв (fallback: только точные UPN, создание запрещено)
        directory = fetch_directory()
        ai_raw = {}   # v13.3: блок «пусто» ниже читает not_a_password_request
        try:
            targets, ai_raw = ai_resolve(full.get("subject", ""), text_all, directory,
                                         latest_body=newest_text(body_text),
                                         thread_ctx=thread_ctx, recent_resets=recent_resets,
                                         pending=pending)
        except RuntimeError as e:
            alert("llm_dead", "AI-резолвер недоступен (%s). Обрабатываю письмо только по точным UPN, "
                  "создание учёток приостановлено: %s" % (str(e)[:60], full.get("subject", "?")[:50]))
            act = "reset" if RESET_WORDS.search(newest_text(text_all)) else "check"
            targets = exact_upn_targets(newest_text(body_text), directory, act)
            if not targets:
                # v10.2: голое имя без AI не разрешить — НЕ помечаем processed,
                # повторим на следующем запуске (иначе запрос молча теряется)
                LOG("LLM недоступен, имя без UPN — откладываю:", full.get("subject", "")[:50])
                continue
        if not targets:
            # v13.3 (инцидент «письмо ушло в тишину»): пустой ответ AI — не повод уйти в тишину.
            # Если в треде висит НАШ уточняющий вопрос, это письмо почти наверняка ответ на
            # него: пометка processed теряла запрос навсегда (frontdesk ждал 2.5 часа).
            # Если AI не сказал явно «это не запрос» — тоже подозрительно (сбой/битый ответ).
            waiting = bool(pending) or bool((state.get("pending_confirm") or {}).get(full["conversationId"]))
            if waiting or not (ai_raw or {}).get("not_a_password_request", False):
                LOG("EMPTY-AI %s (%s) — письмо НЕ помечено, повтор на следующем прогоне"
                    % (frm, "ждём ответа на наш вопрос" if waiting else "AI вернул пусто без флага"))
                alert("empty_ai", "AI вернул пустое решение по письму от %s («%s»%s). "
                      "Письмо не помечено processed — обработаю снова."
                      % (frm, full.get("subject", "?")[:60],
                         ", в треде висит наш уточняющий вопрос" if waiting else ""))
                continue
            state["processed"].append(m["id"]); save_state(state); continue
        # v10.10: механизма согласия через ответ больше НЕТ — сбросы только по явным
        # запросам (имена/UPN + reset-слова в СВЕЖЕМ письме), гвард v12 страхует повторы.
        results, log, check_results, block_notes, ask_notes = [], [], [], [], []
        just_reset = {}   # v12.2: upn_l → пароль, выданный в ЭТОМ прогоне (для соседних тредов)
        # v12: правило заказчика «один сброс без повторов» сохранено, но решает AI;
        # код блокирует cooldown и аккаунт-из-цитаты (см. ниже) — и всегда отвечает в тред.
        reset_log = state.setdefault("reset_log", {})
        body_low = newest_text(body_text or "").lower()

        def was_reset_before(upn):
            ts = reset_log.get((upn or "").lower())
            return bool(ts)

        for t in targets:
            if t["action"] == "create":
                if DRY_RUN:
                    log.append("WOULD-CREATE %s %s (%s)" % (t["first"], t["last"], t.get("reason"))); continue
                try:
                    u, upn, pwd = create_student(t["first"], t["last"])
                    v = verify_password(upn, pwd)
                    audit_log(frm, upn, "create", "пароль проверен: %s" % ("да" if v else ("НЕТ!" if v is False else "недоступна")))
                    results.append((upn, pwd))
                    log.append("CREATED %s + A3 (%s) | проверка: %s" % (upn, t.get("reason"), "✓" if v else ("✗" if v is False else "?")))
                except Exception as e:
                    log.append("CREATE-FAIL %s %s: %s" % (t.get("first"), t.get("last"), str(e)[:120]))
                continue
            if t["action"] == "check":
                # v10.3: запрос статуса — НЕ сбрасывать (кейс «Can we check these ones?»)
                st = check_account(t["upn"])
                if st:
                    status = "ACTIVE" if st["enabled"] else "DISABLED"
                    log.append("CHECK %s: %s, last sign-in: %s" % (st["upn"], status, st["last_signin"]))
                    check_results.append("%s — account %s, last active: %s (%s)" % (st["upn"], status, st["last_signin"], guess_activity(st["last_signin"], st["enabled"])))
                    audit_log(frm, st["upn"], "check", "статус: %s, последний вход: %s" % (status, st["last_signin"]))
                else:
                    log.append("CHECK %s: NOT FOUND" % t["upn"])
                    check_results.append("%s — account NOT FOUND" % t["upn"])
                continue
            user = find_user_by_upn(t["upn"])
            if not user:
                log.append("NOT-FOUND %s" % t["upn"]); continue
            if (user.get("userPrincipalName") or "").lower() == MAILBOX:
                log.append("SKIP admin@ (запрещено)"); continue
            upn_l = (user.get("userPrincipalName") or "").lower()
            confirmed_now = False   # v13: сброс разрешён подтверждением отправителя
            # v13: сомневается AI (confidence=low) — НЕ угадываем, а спрашиваем отправителя
            if t.get("confidence") == "low":
                label = (t.get("label") or "").strip() or "the student you mentioned"
                cands = [c for c in (t.get("candidates") or []) if c.lower() in directory] or [upn_l]
                ask_notes.append(("ask", label, cands, upn_l))
                log.append("ASK %s (сомнение по имени «%s» — уточняю у отправителя)" % (upn_l, label))
                audit_log(frm, upn_l, "ask", "уточняю у отправителя: %s" % label)
                continue
            # v13: по этому аккаунту уже задан уточняющий вопрос — сбрасываем ТОЛЬКО
            # после явного положительного ответа (благодарность/«ок» — не подтверждение)
            if pending and upn_l == (pending.get("upn") or "").lower():
                if t.get("confirmed") and CONFIRM_WORDS.search(body_low):
                    log.append("CONFIRMED %s — отправитель подтвердил: %s" % (upn_l, (t.get("reason") or "")[:60]))
                    state.setdefault("pending_confirm", {}).pop(full["conversationId"], None)
                    pending = None
                    confirmed_now = True   # подтверждение = явный запрос, гвард «только цитата» не применяем
                else:
                    log.append("HOLD %s (ждём явного подтверждения отправителя; ответ: %s)"
                               % (upn_l, (body_low or "")[:60].replace("\n", " ")))
                    audit_log(frm, upn_l, "hold", "уточнение не подтверждено явно — сброса нет")
                    continue
            # v12: уже сброшенный аккаунт сбрасываем ТОЛЬКО если автор попросил его
            # ИМЕННО в свежем (последнем) письме и мы ещё НЕ ответили сбросом по нему
            # (правило заказчика, 18.09: «ни в коем случае не сбрасывать уже сброшенный
            # аккаунт, если это не спросил автор именно в последних сообщениях; если я не
            # ответил сбросом»). Лазейки «настойчивый повтор» НЕТ.
            # Блокируем молча-нельзя: в обоих случаях обязателен поясняющий ответ в тред
            # (инцидент 18.09 — молчаливый SKIP выглядел как «бот умер»).
            recent_ts = max(reset_log.get(upn_l) or [0])
            # cooldown = «мы уже ответили сбросом по этому аккаунту только что» (тот же
            # эпизод, часто соседний тред). Без него два письма в разных тредах дали бы
            # два разных пароля — второй отменяет первый, фронтдеск выдал бы нерабочий.
            cooling = bool(recent_ts) and (time.time() - recent_ts) < RESET_COOLDOWN
            blocking = None
            if cooling:
                # v12.2: если сброс сделан В ЭТОМ ЖЕ прогоне (соседний тред того же
                # эпизода) — отдаём в пояснении тот же самый пароль: запрос автора не
                # остаётся без ответа, а второго сброса (и второго пароля) не происходит.
                same = just_reset.get(upn_l)
                blocking = ("по аккаунту уже отправлен сброс %s (тот же эпизод)" % reset_when_ru(recent_ts),
                            ("%s — the password was reset %s and is the same for both requests: "
                             "%s Password: %s" % (user["userPrincipalName"], reset_when_ru(recent_ts),
                                                  user["userPrincipalName"], same))
                            if same else
                            ("%s — the password has already been reset %s (a few minutes ago). Please use the "
                             "password from the most recent reply. If it still does not work, reply to this "
                             "email and we will reset it again." % (user["userPrincipalName"], reset_when_ru(recent_ts))))
            elif (was_reset_before(user["userPrincipalName"]) and not confirmed_now
                  and not referenced_in_new_text(user, body_low)):
                blocking = ("аккаунт упомянут только в цитате",
                            "%s — this account has already been reset. If the student still needs a new "
                            "password, please send a request naming the account." % user["userPrincipalName"])
            if blocking:
                why, note = blocking
                log.append("SKIP %s (%s; пояснение в тред)" % (user["userPrincipalName"], why))
                audit_log(frm, user["userPrincipalName"], "skip", "повторный сброс заблокирован: %s" % why)
                block_notes.append(note)
                continue
            if DRY_RUN:
                log.append("WOULD-%s %s (%s)" % (t["action"].upper(), user["userPrincipalName"], t.get("reason"))); continue
            try:
                pwd = reset_password(user, t.get("password"))
                reset_log.setdefault((user["userPrincipalName"] or "").lower(), []).append(time.time())
                if len(reset_log) > 400:
                    state["reset_log"] = {k: v2 for k, v2 in list(reset_log.items())[-200:]}
                v = verify_password(user["userPrincipalName"], pwd)
                audit_log(frm, user["userPrincipalName"], t["action"],
                          "пароль проверен: %s" % ("да" if v else ("НЕТ!" if v is False else "недоступна")))
                results.append((user["userPrincipalName"], pwd))
                just_reset[upn_l] = pwd   # v12.2: для пояснений в соседних тредах того же эпизода
                log.append("%s %s (%s) | проверка: %s" % (t["action"].upper(), user["userPrincipalName"], t.get("reason"), "✓" if v else ("✗" if v is False else "?")))
            except Exception as e:
                log.append("RESET-FAIL %s: %s" % (user["userPrincipalName"], str(e)[:100]))
        if check_results and not DRY_RUN:
            # v10.10: ТОЛЬКО отчёт. Никакого «reply to confirm» — вопрос в письме создавал
            # ловушку (любой ответ, даже благодарность, становился «да»). Сброс — только
            # по явному новому запросу с именами/UPN.
            reply_body = {"message": {"body": {"contentType": "Text",
                "content": "\n".join(check_results)
                           + "\n\nIf you need the passwords reset, please send a request with the accounts."}}}
            if TEST_MODE:
                reply_body["message"]["toRecipients"] = [{"emailAddress": {"address": "admin@example.com"}}]
            graph("POST", "/users/%s/messages/%s/reply" % (urllib.parse.quote(MAILBOX), m["id"]), reply_body)
            log.append("REPLY (проверка статусов, в тред) sent to " + frm)
        if ask_notes and not DRY_RUN:
            # v13: не уверены, кого сбрасывать — спрашиваем отправителя ЧЕЛОВЕЧЕСКИМ
            # языком (без «X = Y@example.com»: в школе так не читают) и запоминаем вопрос
            # в state. Сброс — только после явного «да» (см. CONFIRM_WORDS).
            lines = []
            for _, label, cands, upn in ask_notes:
                lines.append(clarify_question(label, cands, directory))
                state.setdefault("pending_confirm", {})[full["conversationId"]] = {
                    "upn": upn, "label": label, "candidates": cands,
                    "asked": time.time(), "asked_ru": reset_when_ru(time.time())}
            reply_body = {"message": {"body": {"contentType": "Text", "content": "\n\n".join(lines)}}}
            if TEST_MODE:
                reply_body["message"]["toRecipients"] = [{"emailAddress": {"address": "admin@example.com"}}]
            graph("POST", "/users/%s/messages/%s/reply" % (urllib.parse.quote(MAILBOX), m["id"]), reply_body)
            log.append("REPLY (уточняющий вопрос, в тред) sent to " + frm)
        if block_notes and not DRY_RUN:
            # v12: запрос не выполнен (cooldown / аккаунт из цитаты) — но requester
            # НЕ остаётся в тишине: пояснение в тред + TG-сводка ниже.
            reply_body = {"message": {"body": {"contentType": "Text", "content": "\n".join(block_notes)}}}
            if TEST_MODE:
                reply_body["message"]["toRecipients"] = [{"emailAddress": {"address": "admin@example.com"}}]
            graph("POST", "/users/%s/messages/%s/reply" % (urllib.parse.quote(MAILBOX), m["id"]), reply_body)
            log.append("REPLY (пояснение по повторному запросу, в тред) sent to " + frm)
        if results and not DRY_RUN:
            reply_lines = ["New details:"]
            for upn, pwd in results:
                reply_lines.append("%s Password: %s" % (upn, pwd))
            reply_body = {"message": {"body": {"contentType": "Text", "content": "\n".join(reply_lines)}}}
            if TEST_MODE:
                reply_body["message"]["toRecipients"] = [{"emailAddress": {"address": "admin@example.com"}}]
            graph("POST", "/users/%s/messages/%s/reply" % (urllib.parse.quote(MAILBOX), m["id"]), reply_body)
            log.append("REPLY (в тред) sent to " + frm)
        state["processed"].append(m["id"])
        save_state(state)
        tg("🔑 PW-BOT v12: %s\n%s" % (full.get("subject", "?")[:60], "\n".join(log[:12])))
        LOG("\n".join(log))
    save_state(state)

if __name__ == "__main__":
    main()
