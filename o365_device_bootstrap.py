#!/usr/bin/env python3
"""Device-code bootstrap delegated-токена для PW-бота.
Приложение задаётся через O365_APP_ID (права admin-consented, AllPrincipals).
Код выводится в stdout + Telegram; после подтверждения токены пишутся в o365_tokens.json."""
import os, json, sys, time, urllib.request, urllib.parse
HOME_DIR = os.environ.get("PW_BOT_HOME", "/opt/password-bot")
TENANT = os.environ.get("O365_TENANT", "")
CLIENT_ID = os.environ.get("O365_APP_ID", "")
SCOPES="offline_access Mail.ReadWrite Mail.Send User.ReadWrite.All UserAuthenticationMethod.ReadWrite.All"
TOKENS = os.path.join(HOME_DIR, "state/o365_tokens.json")
def req(url, data=None):
    r=urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None)
    return json.loads(urllib.request.urlopen(r, timeout=30).read())
def tg(msg):
    try:
        tok=[l.split("=",1)[1].strip() for l in open(os.path.join(HOME_DIR, ".env")) if l.startswith("TELEGRAM_BOT_TOKEN=")][0]
        urllib.request.urlopen(urllib.request.Request("https://api.telegram.org/bot%s/sendMessage"%tok,
            data=urllib.parse.urlencode({"chat_id":os.environ.get("PW_BOT_TG_CHAT",""),"text":msg[:3000]}).encode()), timeout=20)
    except Exception: pass
d=req("https://login.microsoftonline.com/%s/oauth2/v2.0/devicecode"%TENANT,
      {"client_id":CLIENT_ID,"scope":SCOPES})
msg="🔑 PW-BOT FIX: подтверди вход\n\n1. Открой https://microsoft.com/devicelogin\n2. Введи код: %s\n(войти как admin@example.com, код живёт 15 мин)"%d["user_code"]
print(msg); tg(msg)
t0=time.time()
while time.time()-t0<900:
    try:
        tok=req("https://login.microsoftonline.com/%s/oauth2/v2.0/token"%TENANT,
                {"client_id":CLIENT_ID,"grant_type":"urn:ietf:params:oauth:grant-type:device_code",
                 "device_code":d["device_code"]})
        tok["expires_at"]=time.time()+tok.get("expires_in",3600)
        tok["client_id"]=CLIENT_ID
        json.dump(tok, open(TOKENS,"w"), indent=1)
        print("TOKEN OK:", tok.get("scope","")[:150])
        tg("✅ PW-BOT: делегированный токен получен, бот снова работает.")
        sys.exit(0)
    except urllib.error.HTTPError as e:
        body=e.read().decode()
        if "authorization_pending" in body: time.sleep(5); continue
        print("ERR:", body[:300]); tg("❌ PW-BOT bootstrap: "+body[:200]); sys.exit(1)
