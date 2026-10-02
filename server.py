import hashlib, hmac, json, os, random, sqlite3, time
from urllib.parse import parse_qsl
from urllib.request import Request as UrlRequest, urlopen

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")   # بدون @
CHANNEL = os.environ.get("CHANNEL", "")             # مثل @mychannel (اختياري، والبوت يجب أن يكون مشرفاً فيها)
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")         # رابط اللعبة العام (HTTPS)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")  # نص عشوائي طويل تختاره أنت، يجعل مسار الـ webhook غير قابل للتخمين
DB_PATH = os.environ.get("DB_PATH", "game.db")       # على Render: مسار داخل القرص الدائم، مثل /var/data/game.db
DEV = os.environ.get("DEV") == "1"                  # للتجربة خارج تيليغرام فقط
TAPS_TO_HATCH = 10
MIN_TAP_GAP = 0.06
REF_BONUS_NEW, REF_BONUS_INVITER = 200, 300
DUCKS = [("duck", 70, 10), ("chick", 20, 25), ("golden", 8, 40), ("royal", 2, 60)]  # المعرّف، النسبة، العملات
NAMES = {"duck": "بطة عادية", "chick": "كتكوت", "golden": "بطة ذهبية", "royal": "بطة ملكية",
         "fan": "بطة المشجع", "wizard": "بطة الساحر", "astro": "بطة الفضاء"}
SHOP = {"fan": 400, "wizard": 800, "astro": 1500}   # المعرّف -> السعر
INCOME = {"duck": 2, "chick": 4, "golden": 10, "royal": 20, "fan": 10, "wizard": 22, "astro": 50}  # عملات/ساعة
CAP_HOURS = 3   # أقصى مدة تتراكم فيها الأرباح قبل أن تتوقف حتى يجمعها اللاعب
LEGACY = {"🦆": "duck", "🐤": "chick", "🐥": "golden", "🦢": "royal"}  # لتحويل بيانات النسخة القديمة

# المهام: id -> (العنوان، المكافأة، شرط الإكمال)
TASKS = {
    "hatch_3":  ("افقس 3 بيضات", 50, lambda p: len(p["ducks"]) >= 3),
    "hatch_10": ("افقس 10 بيضات", 150, lambda p: len(p["ducks"]) >= 10),
    "invite_1": ("ادعُ صديقاً", 100, lambda p: p["refs"] >= 1),
    "invite_5": ("ادعُ 5 أصدقاء", 500, lambda p: p["refs"] >= 5),
}
if CHANNEL:
    TASKS["channel"] = ("انضم إلى القناة", 100, lambda p: in_channel(p["id"]))


def in_channel(uid: int) -> bool:
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember?chat_id={CHANNEL}&user_id={uid}"
        return json.load(urlopen(url, timeout=5))["result"]["status"] in ("member", "administrator", "creator")
    except Exception:
        return False


def rate(p: dict) -> int:
    return sum(INCOME.get(d, 0) for d in p["ducks"])


def pending(p: dict, now: float) -> float:
    """الأرباح المتراكمة حتى الآن (بدون تعديل اللاعب)."""
    r = rate(p)
    return min(p["stash"] + max(0, now - p["last_collect"]) * r / 3600, r * CAP_HOURS)


def settle(p: dict, now: float):
    """يثبّت الأرباح المتراكمة؛ يُستدعى قبل أي تغيير في عدد البط حتى لا تُحسب بأثر رجعي."""
    p["stash"], p["last_collect"] = pending(p, now), now


db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS players(
  id INTEGER PRIMARY KEY, name TEXT, coins INTEGER DEFAULT 0, taps INTEGER DEFAULT 0,
  ducks TEXT DEFAULT '[]', last_claim REAL DEFAULT 0, last_tap REAL DEFAULT 0,
  refs INTEGER DEFAULT 0, referred_by INTEGER DEFAULT 0, tasks TEXT DEFAULT '[]',
  last_collect REAL DEFAULT 0, stash REAL DEFAULT 0)""")
for col in ("last_collect REAL DEFAULT 0", "stash REAL DEFAULT 0"):   # ترقية قواعد البيانات القديمة
    try:
        db.execute("ALTER TABLE players ADD COLUMN " + col)
    except sqlite3.OperationalError:
        pass
app = FastAPI()
COLS = ["id", "name", "coins", "taps", "ducks", "last_claim", "last_tap", "refs", "referred_by", "tasks", "last_collect", "stash"]


def verify(init_data: str) -> dict:
    if DEV and not init_data:
        return {"id": 1, "first_name": "Dev", "start_param": ""}
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    their_hash = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), their_hash):
        raise HTTPException(401, "invalid signature")
    if time.time() - int(data.get("auth_date", 0)) > 86400:
        raise HTTPException(401, "expired")
    return json.loads(data["user"]) | {"start_param": data.get("start_param", "")}


def load(uid: int):
    r = db.execute(f"SELECT {','.join(COLS)} FROM players WHERE id=?", (uid,)).fetchone()
    if not r:
        return None
    p = dict(zip(COLS, r))
    p["ducks"] = [LEGACY.get(d, d) for d in json.loads(p["ducks"])]
    p["tasks"] = json.loads(p["tasks"])
    return p


def save(p: dict):
    db.execute("""UPDATE players SET coins=?,taps=?,ducks=?,last_claim=?,last_tap=?,refs=?,tasks=?,last_collect=?,stash=? WHERE id=?""",
               (p["coins"], p["taps"], json.dumps(p["ducks"], ensure_ascii=False), p["last_claim"],
                p["last_tap"], p["refs"], json.dumps(p["tasks"]), p["last_collect"], p["stash"], p["id"]))
    db.commit()


def player(auth: str) -> dict:
    user = verify(auth.removeprefix("tma ").strip())
    p = load(user["id"])
    if p:
        return p
    # لاعب جديد: سجّله وطبّق مكافأة الإحالة مرة واحدة
    ref = 0
    sp = user.get("start_param", "")
    if sp.startswith("ref_") and sp[4:].isdigit() and int(sp[4:]) != user["id"]:
        ref = int(sp[4:])
    inviter = load(ref) if ref else None
    db.execute("INSERT INTO players(id,name,coins,referred_by) VALUES(?,?,?,?)",
               (user["id"], user.get("first_name", ""), REF_BONUS_NEW if inviter else 0, ref if inviter else 0))
    if inviter:
        inviter["refs"] += 1
        inviter["coins"] += REF_BONUS_INVITER
        save(inviter)
    db.commit()
    return load(user["id"])


def view(p: dict, msg: str = "") -> dict:
    tasks = [{"id": k, "title": t, "reward": r, "done": k in p["tasks"]} for k, (t, r, _) in TASKS.items()]
    link = f"https://t.me/{BOT_USERNAME}?startapp=ref_{p['id']}" if BOT_USERNAME else ""
    return {"name": p["name"], "coins": p["coins"], "taps": p["taps"], "ducks": p["ducks"],
            "taps_to_hatch": TAPS_TO_HATCH, "next_claim": p["last_claim"] + 86400,
            "refs": p["refs"], "ref_link": link, "ref_bonus": REF_BONUS_INVITER,
            "tasks": tasks, "names": NAMES,
            "shop": [{"id": k, "price": v, "income": INCOME.get(k, 0)} for k, v in SHOP.items()],
            "income": rate(p), "pending": pending(p, time.time()), "cap_hours": CAP_HOURS,
            "income_by_type": INCOME, "msg": msg}


@app.get("/api/state")
def state(authorization: str = Header("")):
    return view(player(authorization))


@app.post("/api/tap")
def tap(authorization: str = Header("")):
    p = player(authorization)
    now = time.time()
    if now - p["last_tap"] < MIN_TAP_GAP:
        return view(p, "بسرعة كبيرة")
    p["last_tap"], p["taps"] = now, p["taps"] + 1
    msg = ""
    if p["taps"] >= TAPS_TO_HATCH:
        duck, _, reward = random.choices(DUCKS, weights=[d[1] for d in DUCKS])[0]
        settle(p, now)
        p["ducks"].append(duck); p["coins"] += reward; p["taps"] = 0
        msg = f"فقست بيضة جديدة: {NAMES[duck]} (+{reward})"
    save(p)
    return view(p, msg)


@app.post("/api/claim")
def claim(authorization: str = Header("")):
    p = player(authorization)
    if time.time() < p["last_claim"] + 86400:
        return view(p, "المكافأة غير جاهزة بعد")
    p["coins"] += 100; p["last_claim"] = time.time()
    save(p)
    return view(p, "حصلت على 100 عملة")


@app.get("/api/leaderboard")
def leaderboard(by: str = "coins", authorization: str = Header("")):
    p = player(authorization)
    col = "json_array_length(ducks)" if by == "ducks" else "coins"   # قيمتان ثابتتان فقط، لا مدخلات من المستخدم
    mine = len(p["ducks"]) if by == "ducks" else p["coins"]
    rows = db.execute(f"SELECT id, name, {col} FROM players ORDER BY {col} DESC, id LIMIT 20").fetchall()
    rank = db.execute(f"SELECT COUNT(*) + 1 FROM players WHERE {col} > ?", (mine,)).fetchone()[0]
    return {"by": by, "me": {"rank": rank, "value": mine},
            "top": [{"rank": i + 1, "name": (n or "لاعب")[:16], "value": v, "me": uid == p["id"]}
                    for i, (uid, n, v) in enumerate(rows)]}


@app.post("/api/collect")
def collect(authorization: str = Header("")):
    p = player(authorization)
    settle(p, time.time())
    amount = int(p["stash"])
    if amount < 1:
        return view(p, "لا توجد أرباح بعد")
    p["coins"] += amount; p["stash"] -= amount
    save(p)
    return view(p, f"جمعت {amount} عملة")


@app.post("/api/task/{task_id}")
def task(task_id: str, authorization: str = Header("")):
    p = player(authorization)
    if task_id not in TASKS:
        raise HTTPException(404, "unknown task")
    title, reward, check = TASKS[task_id]
    if task_id in p["tasks"]:
        return view(p, "أخذت هذه المكافأة من قبل")
    if not check(p):
        return view(p, "لم تكمل المهمة بعد")
    p["tasks"].append(task_id); p["coins"] += reward
    save(p)
    return view(p, f"أُنجزت المهمة: +{reward} عملة")


@app.post("/api/buy/{duck_id}")
def buy(duck_id: str, authorization: str = Header("")):
    p = player(authorization)
    if duck_id not in SHOP:
        raise HTTPException(404, "not for sale")
    price = SHOP[duck_id]
    if p["coins"] < price:
        return view(p, f"تحتاج {price - p['coins']} عملة إضافية")
    settle(p, time.time())
    p["coins"] -= price; p["ducks"].append(duck_id)
    save(p)
    return view(p, f"اشتريت {NAMES[duck_id]}")


@app.post("/webhook/{secret}")
def webhook(secret: str, update: dict = Body(...)):
    """يستقبل رسائل البوت من تيليغرام، ويرد على /start بزر يفتح اللعبة."""
    if not WEBHOOK_SECRET or not hmac.compare_digest(secret, WEBHOOK_SECRET):
        raise HTTPException(404)
    msg = update.get("message") or {}
    if (msg.get("text") or "").startswith("/start") and WEBAPP_URL:
        body = {"chat_id": msg["chat"]["id"],
                "text": "أهلاً بك في مزرعة البط! افقس البيض واجمع البط.",
                "reply_markup": {"inline_keyboard": [[{"text": "🦆 العب الآن", "web_app": {"url": WEBAPP_URL}}]]}}
        try:
            urlopen(UrlRequest(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                               json.dumps(body).encode(), {"Content-Type": "application/json"}), timeout=5)
        except Exception:
            pass
    return {"ok": True}


@app.get("/static/ducks/{name}.svg")
def duck_image(name: str):
    svg = IMAGES.get(name)
    if not svg:
        raise HTTPException(404)
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/")
def index():
    return FileResponse("index.html")


# صور البط والبيض (SVG). لاستبدال أي صورة غيّر نصها هنا.
IMAGES = {
    'astro': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 -12 100 112\"><ellipse cx=\"50\" cy=\"96\" rx=\"30\" ry=\"4\" fill=\"#000\" opacity=\".18\"/><ellipse cx=\"38\" cy=\"90\" rx=\"8\" ry=\"4\" fill=\"#ff8a1f\"/><ellipse cx=\"62\" cy=\"90\" rx=\"8\" ry=\"4\" fill=\"#ff8a1f\"/><ellipse cx=\"50\" cy=\"68\" rx=\"30\" ry=\"24\" fill=\"#f1f3f8\"/><ellipse cx=\"32\" cy=\"68\" rx=\"9\" ry=\"14\" fill=\"#c9cfdd\" transform=\"rotate(12 32 68)\"/><circle cx=\"50\" cy=\"36\" r=\"22\" fill=\"#f1f3f8\"/><ellipse cx=\"50\" cy=\"44\" rx=\"12\" ry=\"7\" fill=\"#ff8a1f\"/><path d=\"M40 44 Q50 48 60 44\" stroke=\"#c2570c\" stroke-width=\"1.5\" fill=\"none\"/><circle cx=\"41\" cy=\"33\" r=\"3.4\" fill=\"#2a2233\"/><circle cx=\"59\" cy=\"33\" r=\"3.4\" fill=\"#2a2233\"/><circle cx=\"42.2\" cy=\"31.8\" r=\"1.1\" fill=\"#fff\"/><circle cx=\"60.2\" cy=\"31.8\" r=\"1.1\" fill=\"#fff\"/><circle cx=\"35\" cy=\"42\" r=\"3.5\" fill=\"#ff9c8a\" opacity=\".55\"/><circle cx=\"65\" cy=\"42\" r=\"3.5\" fill=\"#ff9c8a\" opacity=\".55\"/><circle cx=\"50\" cy=\"36\" r=\"27\" fill=\"#bfe6ff\" opacity=\".35\" stroke=\"#fff\" stroke-width=\"3\"/><path d=\"M33 22 Q40 14 50 13\" stroke=\"#fff\" stroke-width=\"3\" fill=\"none\" stroke-linecap=\"round\" opacity=\".8\"/><rect x=\"38\" y=\"76\" width=\"24\" height=\"8\" rx=\"3\" fill=\"#ff7a29\"/></svg>",
    'chick': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 -12 100 112\"><g transform=\"translate(15 20) scale(.7)\"><ellipse cx=\"50\" cy=\"96\" rx=\"30\" ry=\"4\" fill=\"#000\" opacity=\".18\"/><ellipse cx=\"38\" cy=\"90\" rx=\"8\" ry=\"4\" fill=\"#ffa03a\"/><ellipse cx=\"62\" cy=\"90\" rx=\"8\" ry=\"4\" fill=\"#ffa03a\"/><ellipse cx=\"50\" cy=\"68\" rx=\"30\" ry=\"24\" fill=\"#ffe98a\"/><ellipse cx=\"32\" cy=\"68\" rx=\"9\" ry=\"14\" fill=\"#f5cf4a\" transform=\"rotate(12 32 68)\"/><circle cx=\"50\" cy=\"36\" r=\"22\" fill=\"#ffe98a\"/><ellipse cx=\"50\" cy=\"44\" rx=\"12\" ry=\"7\" fill=\"#ffa03a\"/><path d=\"M40 44 Q50 48 60 44\" stroke=\"#c2570c\" stroke-width=\"1.5\" fill=\"none\"/><circle cx=\"41\" cy=\"33\" r=\"3.4\" fill=\"#2a2233\"/><circle cx=\"59\" cy=\"33\" r=\"3.4\" fill=\"#2a2233\"/><circle cx=\"42.2\" cy=\"31.8\" r=\"1.1\" fill=\"#fff\"/><circle cx=\"60.2\" cy=\"31.8\" r=\"1.1\" fill=\"#fff\"/><circle cx=\"35\" cy=\"42\" r=\"3.5\" fill=\"#ff9c8a\" opacity=\".55\"/><circle cx=\"65\" cy=\"42\" r=\"3.5\" fill=\"#ff9c8a\" opacity=\".55\"/><path d=\"M50 14 Q44 2 52 6 Q54 -2 58 8\" stroke=\"#f5cf4a\" stroke-width=\"3\" fill=\"none\" stroke-linecap=\"round\"/></g></svg>",
    'duck': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 -12 100 112\"><ellipse cx=\"50\" cy=\"96\" rx=\"30\" ry=\"4\" fill=\"#000\" opacity=\".18\"/><ellipse cx=\"38\" cy=\"90\" rx=\"8\" ry=\"4\" fill=\"#ff8a1f\"/><ellipse cx=\"62\" cy=\"90\" rx=\"8\" ry=\"4\" fill=\"#ff8a1f\"/><ellipse cx=\"50\" cy=\"68\" rx=\"30\" ry=\"24\" fill=\"#ffd23f\"/><ellipse cx=\"32\" cy=\"68\" rx=\"9\" ry=\"14\" fill=\"#f2b705\" transform=\"rotate(12 32 68)\"/><circle cx=\"50\" cy=\"36\" r=\"22\" fill=\"#ffd23f\"/><ellipse cx=\"50\" cy=\"44\" rx=\"12\" ry=\"7\" fill=\"#ff8a1f\"/><path d=\"M40 44 Q50 48 60 44\" stroke=\"#c2570c\" stroke-width=\"1.5\" fill=\"none\"/><circle cx=\"41\" cy=\"33\" r=\"3.4\" fill=\"#2a2233\"/><circle cx=\"59\" cy=\"33\" r=\"3.4\" fill=\"#2a2233\"/><circle cx=\"42.2\" cy=\"31.8\" r=\"1.1\" fill=\"#fff\"/><circle cx=\"60.2\" cy=\"31.8\" r=\"1.1\" fill=\"#fff\"/><circle cx=\"35\" cy=\"42\" r=\"3.5\" fill=\"#ff9c8a\" opacity=\".55\"/><circle cx=\"65\" cy=\"42\" r=\"3.5\" fill=\"#ff9c8a\" opacity=\".55\"/></svg>",
    'egg': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 112\"><defs><radialGradient id=\"g\" cx=\".35\" cy=\".3\" r=\".9\"><stop offset=\"0\" stop-color=\"#ffc977\"/><stop offset=\".6\" stop-color=\"#f0a23b\"/><stop offset=\"1\" stop-color=\"#b9611c\"/></radialGradient></defs><path d=\"M50 6 C78 6 92 50 92 72 C92 92 74 106 50 106 C26 106 8 92 8 72 C8 50 22 6 50 6Z\" fill=\"url(#g)\"/><path d=\"M30 50 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M54 44 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M24 74 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M48 70 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M68 66 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M38 92 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M62 90 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><ellipse cx=\"34\" cy=\"34\" rx=\"7\" ry=\"12\" fill=\"#fff\" opacity=\".28\" transform=\"rotate(-20 34 34)\"/></svg>",
    'egg_crack': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 112\"><defs><radialGradient id=\"g\" cx=\".35\" cy=\".3\" r=\".9\"><stop offset=\"0\" stop-color=\"#ffc977\"/><stop offset=\".6\" stop-color=\"#f0a23b\"/><stop offset=\"1\" stop-color=\"#b9611c\"/></radialGradient></defs><path d=\"M50 6 C78 6 92 50 92 72 C92 92 74 106 50 106 C26 106 8 92 8 72 C8 50 22 6 50 6Z\" fill=\"url(#g)\"/><path d=\"M30 50 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M54 44 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M24 74 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M48 70 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M68 66 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M38 92 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M62 90 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><ellipse cx=\"34\" cy=\"34\" rx=\"7\" ry=\"12\" fill=\"#fff\" opacity=\".28\" transform=\"rotate(-20 34 34)\"/><polyline points=\"14,56 30,66 42,50 56,68 70,50 88,62\" fill=\"none\" stroke=\"#3b1d0a\" stroke-width=\"3.5\" stroke-linejoin=\"round\"/><polyline points=\"14,56 30,66 42,50 56,68 70,50 88,62\" fill=\"none\" stroke=\"#ffe9a8\" stroke-width=\"1.2\" stroke-linejoin=\"round\" opacity=\".8\"/></svg>",
    'fan': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 -12 100 112\"><ellipse cx=\"50\" cy=\"96\" rx=\"30\" ry=\"4\" fill=\"#000\" opacity=\".18\"/><ellipse cx=\"38\" cy=\"90\" rx=\"8\" ry=\"4\" fill=\"#ff8a1f\"/><ellipse cx=\"62\" cy=\"90\" rx=\"8\" ry=\"4\" fill=\"#ff8a1f\"/><ellipse cx=\"50\" cy=\"68\" rx=\"30\" ry=\"24\" fill=\"#ffd23f\"/><clipPath id=\"b\"><ellipse cx=\"50\" cy=\"68\" rx=\"30\" ry=\"24\"/></clipPath><g clip-path=\"url(#b)\"><rect x=\"24\" y=\"40\" width=\"10\" height=\"60\" fill=\"#6cc6f5\"/><rect x=\"44\" y=\"40\" width=\"10\" height=\"60\" fill=\"#6cc6f5\"/><rect x=\"64\" y=\"40\" width=\"10\" height=\"60\" fill=\"#6cc6f5\"/><rect x=\"84\" y=\"40\" width=\"10\" height=\"60\" fill=\"#6cc6f5\"/></g><ellipse cx=\"32\" cy=\"68\" rx=\"9\" ry=\"14\" fill=\"#f2b705\" transform=\"rotate(12 32 68)\"/><circle cx=\"50\" cy=\"36\" r=\"22\" fill=\"#ffd23f\"/><ellipse cx=\"50\" cy=\"44\" rx=\"12\" ry=\"7\" fill=\"#ff8a1f\"/><path d=\"M40 44 Q50 48 60 44\" stroke=\"#c2570c\" stroke-width=\"1.5\" fill=\"none\"/><circle cx=\"41\" cy=\"33\" r=\"3.4\" fill=\"#2a2233\"/><circle cx=\"59\" cy=\"33\" r=\"3.4\" fill=\"#2a2233\"/><circle cx=\"42.2\" cy=\"31.8\" r=\"1.1\" fill=\"#fff\"/><circle cx=\"60.2\" cy=\"31.8\" r=\"1.1\" fill=\"#fff\"/><circle cx=\"35\" cy=\"42\" r=\"3.5\" fill=\"#ff9c8a\" opacity=\".55\"/><circle cx=\"65\" cy=\"42\" r=\"3.5\" fill=\"#ff9c8a\" opacity=\".55\"/><rect x=\"30\" y=\"22\" width=\"40\" height=\"6\" rx=\"3\" fill=\"#6cc6f5\"/></svg>",
    'golden': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 -12 100 112\"><ellipse cx=\"50\" cy=\"96\" rx=\"30\" ry=\"4\" fill=\"#000\" opacity=\".18\"/><ellipse cx=\"38\" cy=\"90\" rx=\"8\" ry=\"4\" fill=\"#e8590c\"/><ellipse cx=\"62\" cy=\"90\" rx=\"8\" ry=\"4\" fill=\"#e8590c\"/><ellipse cx=\"50\" cy=\"68\" rx=\"30\" ry=\"24\" fill=\"#f7b500\"/><ellipse cx=\"32\" cy=\"68\" rx=\"9\" ry=\"14\" fill=\"#d98e00\" transform=\"rotate(12 32 68)\"/><circle cx=\"50\" cy=\"36\" r=\"22\" fill=\"#f7b500\"/><ellipse cx=\"50\" cy=\"44\" rx=\"12\" ry=\"7\" fill=\"#e8590c\"/><path d=\"M40 44 Q50 48 60 44\" stroke=\"#c2570c\" stroke-width=\"1.5\" fill=\"none\"/><circle cx=\"41\" cy=\"33\" r=\"3.4\" fill=\"#2a2233\"/><circle cx=\"59\" cy=\"33\" r=\"3.4\" fill=\"#2a2233\"/><circle cx=\"42.2\" cy=\"31.8\" r=\"1.1\" fill=\"#fff\"/><circle cx=\"60.2\" cy=\"31.8\" r=\"1.1\" fill=\"#fff\"/><circle cx=\"35\" cy=\"42\" r=\"3.5\" fill=\"#ff9c8a\" opacity=\".55\"/><circle cx=\"65\" cy=\"42\" r=\"3.5\" fill=\"#ff9c8a\" opacity=\".55\"/><path d=\"M78 14 L80.4 19.6 L86 22 L80.4 24.4 L78 30 L75.6 24.4 L70 22 L75.6 19.6Z\" fill=\"#fff6c9\"/><path d=\"M20 50 L21.8 54.2 L26 56 L21.8 57.8 L20 62 L18.2 57.8 L14 56 L18.2 54.2Z\" fill=\"#fff6c9\"/><path d=\"M84 65 L85.5 68.5 L89 70 L85.5 71.5 L84 75 L82.5 71.5 L79 70 L82.5 68.5Z\" fill=\"#fff6c9\"/></svg>",
    'royal': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 -12 100 112\"><ellipse cx=\"50\" cy=\"96\" rx=\"30\" ry=\"4\" fill=\"#000\" opacity=\".18\"/><ellipse cx=\"38\" cy=\"90\" rx=\"8\" ry=\"4\" fill=\"#ff8a1f\"/><ellipse cx=\"62\" cy=\"90\" rx=\"8\" ry=\"4\" fill=\"#ff8a1f\"/><ellipse cx=\"50\" cy=\"68\" rx=\"30\" ry=\"24\" fill=\"#f4f1ff\"/><ellipse cx=\"32\" cy=\"68\" rx=\"9\" ry=\"14\" fill=\"#cfc8ee\" transform=\"rotate(12 32 68)\"/><circle cx=\"50\" cy=\"36\" r=\"22\" fill=\"#f4f1ff\"/><ellipse cx=\"50\" cy=\"44\" rx=\"12\" ry=\"7\" fill=\"#ff8a1f\"/><path d=\"M40 44 Q50 48 60 44\" stroke=\"#c2570c\" stroke-width=\"1.5\" fill=\"none\"/><circle cx=\"41\" cy=\"33\" r=\"3.4\" fill=\"#2a2233\"/><circle cx=\"59\" cy=\"33\" r=\"3.4\" fill=\"#2a2233\"/><circle cx=\"42.2\" cy=\"31.8\" r=\"1.1\" fill=\"#fff\"/><circle cx=\"60.2\" cy=\"31.8\" r=\"1.1\" fill=\"#fff\"/><circle cx=\"35\" cy=\"42\" r=\"3.5\" fill=\"#ff9c8a\" opacity=\".55\"/><circle cx=\"65\" cy=\"42\" r=\"3.5\" fill=\"#ff9c8a\" opacity=\".55\"/><polygon points=\"36,18 38,2 45,11 50,0 55,11 62,2 64,18\" fill=\"#ffd23f\" stroke=\"#c98a00\" stroke-width=\"2\" stroke-linejoin=\"round\"/><circle cx=\"50\" cy=\"12\" r=\"2.2\" fill=\"#e5384f\"/></svg>",
    'wizard': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 -12 100 112\"><ellipse cx=\"50\" cy=\"96\" rx=\"30\" ry=\"4\" fill=\"#000\" opacity=\".18\"/><ellipse cx=\"38\" cy=\"90\" rx=\"8\" ry=\"4\" fill=\"#ff8a1f\"/><ellipse cx=\"62\" cy=\"90\" rx=\"8\" ry=\"4\" fill=\"#ff8a1f\"/><ellipse cx=\"50\" cy=\"68\" rx=\"30\" ry=\"24\" fill=\"#ffd23f\"/><ellipse cx=\"32\" cy=\"68\" rx=\"9\" ry=\"14\" fill=\"#f2b705\" transform=\"rotate(12 32 68)\"/><circle cx=\"50\" cy=\"36\" r=\"22\" fill=\"#ffd23f\"/><ellipse cx=\"50\" cy=\"44\" rx=\"12\" ry=\"7\" fill=\"#ff8a1f\"/><path d=\"M40 44 Q50 48 60 44\" stroke=\"#c2570c\" stroke-width=\"1.5\" fill=\"none\"/><circle cx=\"41\" cy=\"33\" r=\"3.4\" fill=\"#2a2233\"/><circle cx=\"59\" cy=\"33\" r=\"3.4\" fill=\"#2a2233\"/><circle cx=\"42.2\" cy=\"31.8\" r=\"1.1\" fill=\"#fff\"/><circle cx=\"60.2\" cy=\"31.8\" r=\"1.1\" fill=\"#fff\"/><circle cx=\"35\" cy=\"42\" r=\"3.5\" fill=\"#ff9c8a\" opacity=\".55\"/><circle cx=\"65\" cy=\"42\" r=\"3.5\" fill=\"#ff9c8a\" opacity=\".55\"/><polygon points=\"34,20 50,-10 66,20\" fill=\"#6a3fc4\"/><ellipse cx=\"50\" cy=\"20\" rx=\"24\" ry=\"5\" fill=\"#53309e\"/><path d=\"M50 1 L51.5 4.5 L55 6 L51.5 7.5 L50 11 L48.5 7.5 L45 6 L48.5 4.5Z\" fill=\"#ffe27a\"/></svg>",
}
