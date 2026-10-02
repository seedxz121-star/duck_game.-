import hashlib, hmac, json, os, random, sqlite3, time
from urllib.parse import parse_qsl
from urllib.request import Request as UrlRequest, urlopen

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")      # بدون @
CHANNEL = os.environ.get("CHANNEL", "")                # اختياري: @channel
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
DB_PATH = os.environ.get("DB_PATH", "game.db")
DEV = os.environ.get("DEV") == "1"

N, KINDS, MIN_GROUP = 7, 5, 3                           # اللوحة 7×7، 5 ألوان، أقل مجموعة للكسر
ENERGY_MAX, ENERGY_SEC = 50, 20                         # الطاقة القصوى، ثواني تجدد طاقة واحدة
HATCH_AT, XP_PER_LEVEL = 40, 150                        # بيضات لفقس بطة، نقاط خبرة لكل مستوى
CAP_HOURS, MAX_LV = 3, 10
REF_BONUS_NEW, REF_BONUS_INVITER = 200, 300
LOCKS = {(0, 0): 3, (0, N - 1): 5, (N - 1, 0): 7, (N - 1, N - 1): 9}   # خانة مقفلة: المستوى المطلوب

TYPES = {  # المعرّف: (الاسم، الندرة)
    "duck": ("بطة عادية", "common"), "chick": ("كتكوت", "common"),
    "fan": ("بطة المشجع", "uncommon"), "wizard": ("بطة الساحر", "rare"),
    "astro": ("بطة الفضاء", "epic"), "golden": ("بطة ذهبية", "legendary"), "royal": ("بطة ملكية", "legendary")}
RARITIES = [("common", 60), ("uncommon", 25), ("rare", 10), ("epic", 4), ("legendary", 1)]   # الندرة، نسبة الفقس %
BASE = {"common": 2, "uncommon": 8, "rare": 18, "epic": 40, "legendary": 90}               # عملات/ساعة للمستوى 1
SHOP = {"fan": 400, "wizard": 1200, "astro": 3000}
ITEMS = {  # المعرّف: (الاسم، الندرة، زيادة الدخل %، السعر)
    "cap": ("قبعة رياضية", "common", 5, 150), "bow": ("فيونكة", "common", 5, 150),
    "shades": ("نظارة شمسية", "uncommon", 10, 400), "scarf": ("وشاح", "uncommon", 10, 400),
    "phones": ("سماعات", "rare", 20, 1000), "tiara": ("التاج الذهبي", "epic", 35, 2500)}
DROP_W = [("common", 55), ("uncommon", 30), ("rare", 12), ("epic", 3)]
AUTO_MAX = 10                                              # أقصى عدد كسرات في ضغطة الكسر التلقائي
SLOT_SEC = 6 * 3600                                        # تتجدد عروض السوق كل 6 ساعات
EV_SEC = 24 * 3600                                         # مدة السباق
EV_NAMES = ["سباق الفجر", "سباق البركة", "سباق القمر", "سباق الغيم"]
EV_MILES = [(100, 60), (300, 200), (800, 500), (1500, 1000)]   # بيضات مكسورة -> مكافأة
LEGACY = {"🦆": "duck", "🐤": "chick", "🐥": "golden", "🦢": "royal"}

TASKS = {  # id: (العنوان، المكافأة، الشرط)
    "pop_100": ("اكسر 100 بيضة", 80, lambda p: p["pops"] >= 100),
    "pop_500": ("اكسر 500 بيضة", 300, lambda p: p["pops"] >= 500),
    "hatch_3": ("اجمع 3 بطات", 50, lambda p: len(p["ducks"]) >= 3),
    "hatch_10": ("اجمع 10 بطات", 150, lambda p: len(p["ducks"]) >= 10),
    "level_5": ("وصول المستوى 5", 200, lambda p: level(p) >= 5),
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


def level(p): return 1 + p["xp"] // XP_PER_LEVEL
def upg_cost(lv): return 60 * lv * lv
def fresh(): return random.randrange(KINDS)
def duck_income(d):
    bonus = ITEMS.get(d.get("it"), ("", "", 0, 0))[2]
    return round(BASE[TYPES[d["t"]][1]] * d["lv"] * (100 + bonus) / 100)


def rate(p): return sum(duck_income(d) for d in p["ducks"])


def event_id(now): return int(now // EV_SEC)


def touch_event(p, now):
    if p["ev_id"] != event_id(now):
        p["ev_id"], p["ev_score"], p["ev_claim"] = event_id(now), 0, 0


def offers(slot):
    rng = random.Random(slot * 7919)
    out = []
    for t in rng.sample(list(SHOP), 2):
        d = rng.choice([0, 10, 20, 30]); out.append({"kind": "duck", "ref": t, "price": SHOP[t] * (100 - d) // 100, "disc": d})
    for it in rng.sample(list(ITEMS), 2):
        d = rng.choice([0, 10, 20, 30]); out.append({"kind": "item", "ref": it, "price": ITEMS[it][3] * (100 - d) // 100, "disc": d})
    return out


def pending(p, now):
    r = rate(p)
    return min(p["stash"] + max(0, now - p["last_collect"]) * r / 3600, r * CAP_HOURS)


def settle(p, now):
    """يثبّت الأرباح قبل أي تغيير في البط حتى لا تُحسب بأثر رجعي."""
    p["stash"], p["last_collect"] = pending(p, now), now


def regen(p, now):
    p["energy"] = min(ENERGY_MAX, p["energy"] + max(0, now - p["energy_t"]) / ENERGY_SEC) if p["energy_t"] else ENERGY_MAX
    p["energy_t"] = now


def ensure_board(p):
    b = p["board"]
    if not b:
        b = [[fresh() for _ in range(N)] for _ in range(N)]
        for (r, c) in LOCKS:
            b[r][c] = -1
    for (r, c), need in LOCKS.items():
        if b[r][c] == -1 and level(p) >= need:
            b[r][c] = fresh()
    p["board"] = b


def group(board, r, c):
    """كل الخانات المتجاورة (أعلى/أسفل/يمين/يسار) من اللون نفسه."""
    k = board[r][c]
    if k < 0:
        return []
    seen, stack = {(r, c)}, [(r, c)]
    while stack:
        y, x = stack.pop()
        for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
            if 0 <= ny < N and 0 <= nx < N and (ny, nx) not in seen and board[ny][nx] == k:
                seen.add((ny, nx)); stack.append((ny, nx))
    return list(seen)


db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS players(
  id INTEGER PRIMARY KEY, name TEXT, coins INTEGER DEFAULT 0, ducks TEXT DEFAULT '[]',
  last_claim REAL DEFAULT 0, refs INTEGER DEFAULT 0, referred_by INTEGER DEFAULT 0, tasks TEXT DEFAULT '[]',
  last_collect REAL DEFAULT 0, stash REAL DEFAULT 0, board TEXT DEFAULT '', energy REAL DEFAULT 50,
  energy_t REAL DEFAULT 0, xp INTEGER DEFAULT 0, pops INTEGER DEFAULT 0, meter INTEGER DEFAULT 0,
  items TEXT DEFAULT '{}', shop_slot INTEGER DEFAULT 0, shop_bought TEXT DEFAULT '[]',
  ev_id INTEGER DEFAULT 0, ev_score INTEGER DEFAULT 0, ev_claim INTEGER DEFAULT 0)""")
for col in ("last_collect REAL DEFAULT 0", "stash REAL DEFAULT 0", "board TEXT DEFAULT ''", "energy REAL DEFAULT 50",
            "energy_t REAL DEFAULT 0", "xp INTEGER DEFAULT 0", "pops INTEGER DEFAULT 0", "meter INTEGER DEFAULT 0",
            "items TEXT DEFAULT '{}'", "shop_slot INTEGER DEFAULT 0", "shop_bought TEXT DEFAULT '[]'",
            "ev_id INTEGER DEFAULT 0", "ev_score INTEGER DEFAULT 0", "ev_claim INTEGER DEFAULT 0"):
    try:
        db.execute("ALTER TABLE players ADD COLUMN " + col)     # ترقية قواعد البيانات القديمة
    except sqlite3.OperationalError:
        pass
app = FastAPI()
COLS = ["id", "name", "coins", "ducks", "last_claim", "refs", "referred_by", "tasks", "last_collect", "stash",
        "board", "energy", "energy_t", "xp", "pops", "meter",
        "items", "shop_slot", "shop_bought", "ev_id", "ev_score", "ev_claim"]


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


def load(uid):
    r = db.execute(f"SELECT {','.join(COLS)} FROM players WHERE id=?", (uid,)).fetchone()
    if not r:
        return None
    p = dict(zip(COLS, r))
    p["ducks"] = [d if isinstance(d, dict) else {"t": LEGACY.get(d, d), "lv": 1} for d in json.loads(p["ducks"] or "[]")]
    p["tasks"] = json.loads(p["tasks"] or "[]")
    p["board"] = json.loads(p["board"]) if p["board"] else []
    p["items"] = json.loads(p["items"] or "{}")
    p["shop_bought"] = json.loads(p["shop_bought"] or "[]")
    return p


def save(p):
    db.execute("""UPDATE players SET coins=?,ducks=?,last_claim=?,refs=?,tasks=?,last_collect=?,stash=?,board=?,
                  energy=?,energy_t=?,xp=?,pops=?,meter=?,items=?,shop_slot=?,shop_bought=?,
                  ev_id=?,ev_score=?,ev_claim=? WHERE id=?""",
               (p["coins"], json.dumps(p["ducks"], ensure_ascii=False), p["last_claim"], p["refs"],
                json.dumps(p["tasks"]), p["last_collect"], p["stash"], json.dumps(p["board"]),
                p["energy"], p["energy_t"], p["xp"], p["pops"], p["meter"], json.dumps(p["items"]), p["shop_slot"],
                json.dumps(p["shop_bought"]), p["ev_id"], p["ev_score"], p["ev_claim"], p["id"]))
    db.commit()


def player(auth):
    user = verify(auth.removeprefix("tma ").strip())
    p = load(user["id"])
    if not p:
        sp, ref = user.get("start_param", ""), 0
        if sp.startswith("ref_") and sp[4:].isdigit() and int(sp[4:]) != user["id"]:
            ref = int(sp[4:])
        inviter = load(ref) if ref else None
        db.execute("INSERT INTO players(id,name,coins,referred_by,energy,energy_t) VALUES(?,?,?,?,?,?)",
                   (user["id"], user.get("first_name", ""), REF_BONUS_NEW if inviter else 0,
                    ref if inviter else 0, ENERGY_MAX, time.time()))
        if inviter:
            inviter["refs"] += 1; inviter["coins"] += REF_BONUS_INVITER
            save(inviter)
        db.commit()
        p = load(user["id"])
    before = json.dumps(p["board"])
    ensure_board(p)
    if json.dumps(p["board"]) != before:
        save(p)
    return p


def view(p, msg=""):
    now = time.time()
    energy = min(ENERGY_MAX, p["energy"] + max(0, now - p["energy_t"]) / ENERGY_SEC) if p["energy_t"] else ENERGY_MAX
    ducks = [{"t": d["t"], "lv": d["lv"], "it": d.get("it", ""), "income": duck_income(d),
              "cost": upg_cost(d["lv"]) if d["lv"] < MAX_LV else 0} for d in p["ducks"]]
    link = f"https://t.me/{BOT_USERNAME}?startapp=ref_{p['id']}" if BOT_USERNAME else ""
    slot, eid = int(now // SLOT_SEC), event_id(now)
    bought = p["shop_bought"] if p["shop_slot"] == slot else []
    offs = []
    for i, o in enumerate(offers(slot)):
        name = TYPES[o["ref"]][0] if o["kind"] == "duck" else ITEMS[o["ref"]][0]
        rar = TYPES[o["ref"]][1] if o["kind"] == "duck" else ITEMS[o["ref"]][1]
        offs.append(o | {"i": i, "name": name, "rarity": rar, "bought": i in bought})
    live = p["ev_id"] == eid
    sc, cl = (p["ev_score"], p["ev_claim"]) if live else (0, 0)
    return {"name": p["name"], "coins": p["coins"], "board": p["board"], "energy": energy,
            "energy_max": ENERGY_MAX, "energy_sec": ENERGY_SEC, "meter": p["meter"], "hatch_at": HATCH_AT,
            "level": level(p), "xp_in": p["xp"] % XP_PER_LEVEL, "xp_need": XP_PER_LEVEL,
            "ducks": ducks, "types": {k: {"name": v[0], "rarity": v[1]} for k, v in TYPES.items()},
            "items": {k: v for k, v in p["items"].items() if v > 0},
            "item_info": {k: {"name": v[0], "rarity": v[1], "bonus": v[2]} for k, v in ITEMS.items()},
            "income": rate(p), "pending": pending(p, now), "cap_hours": CAP_HOURS, "max_lv": MAX_LV,
            "offers": offs, "offers_ends_in": (slot + 1) * SLOT_SEC - now,
            "event": {"name": EV_NAMES[eid % len(EV_NAMES)], "ends_in": (eid + 1) * EV_SEC - now, "score": sc,
                      "milestones": [{"need": n, "reward": r, "claimed": i < cl} for i, (n, r) in enumerate(EV_MILES)]},
            "tasks": [{"id": k, "title": t, "reward": r, "done": k in p["tasks"]} for k, (t, r, _) in TASKS.items()],
            "next_claim": p["last_claim"] + 86400, "refs": p["refs"], "ref_link": link,
            "ref_bonus": REF_BONUS_INVITER, "msg": msg}


@app.get("/api/state")
def state(authorization: str = Header("")):
    return view(player(authorization))


def blast(p, cells, now):
    """يكسر مجموعة ويعيد (العملات المكتسبة، قائمة الأحداث الخاصة)."""
    n, old_level, sp = len(cells), level(p), []
    for y, x in cells:
        p["board"][y][x] = fresh()
    gain = n * n // 2
    p["coins"] += gain; p["xp"] += n; p["pops"] += n; p["meter"] += n
    touch_event(p, now); p["ev_score"] += n
    while p["meter"] >= HATCH_AT:
        p["meter"] -= HATCH_AT
        settle(p, now)
        rarity = random.choices([x[0] for x in RARITIES], weights=[x[1] for x in RARITIES])[0]
        t = random.choice([k for k, v in TYPES.items() if v[1] == rarity])
        p["ducks"].append({"t": t, "lv": 1})
        sp.append(f"فقست {TYPES[t][0]}")
    if random.random() < 0.10 + n * 0.01:
        rar = random.choices([x[0] for x in DROP_W], weights=[x[1] for x in DROP_W])[0]
        it = random.choice([k for k, v in ITEMS.items() if v[1] == rar])
        p["items"][it] = p["items"].get(it, 0) + 1
        sp.append(f"وجدت {ITEMS[it][0]}")
    if level(p) > old_level:
        bonus = 50 * level(p)
        p["coins"] += bonus
        ensure_board(p)
        sp.append(f"المستوى {level(p)} (+{bonus})")
    return gain, sp


@app.post("/api/pop/{r}/{c}")
def pop(r: int, c: int, authorization: str = Header("")):
    p = player(authorization)
    if not (0 <= r < N and 0 <= c < N):
        raise HTTPException(400, "bad cell")
    now = time.time()
    regen(p, now)
    cells = group(p["board"], r, c)
    if len(cells) < MIN_GROUP:
        save(p)
        return view(p, f"اختر {MIN_GROUP} بيضات متجاورة من اللون نفسه")
    if p["energy"] < 1:
        save(p)
        return view(p, "نفدت الطاقة، انتظر قليلاً")
    p["energy"] -= 1
    gain, sp = blast(p, cells, now)
    save(p)
    return view(p, "، ".join([f"+{gain} عملة"] + sp))


def largest_group(board):
    seen, best = set(), []
    for r in range(N):
        for c in range(N):
            if (r, c) not in seen and board[r][c] >= 0:
                g = group(board, r, c)
                seen.update(g)
                if len(g) > len(best):
                    best = g
    return best


@app.post("/api/auto")
def auto(authorization: str = Header("")):
    p = player(authorization)
    now = time.time()
    regen(p, now)
    done = total = 0
    sp = []
    while done < AUTO_MAX and p["energy"] >= 1:
        best = largest_group(p["board"])
        if len(best) < MIN_GROUP:
            break
        p["energy"] -= 1
        g, s2 = blast(p, best, now)
        done += 1; total += g; sp += s2
    save(p)
    if not done:
        return view(p, "لا طاقة كافية أو لا توجد مجموعات")
    return view(p, "، ".join([f"كسر تلقائي {done} مرات: +{total} عملة"] + sp))


@app.post("/api/upgrade/{i}")
def upgrade(i: int, authorization: str = Header("")):
    p = player(authorization)
    if not (0 <= i < len(p["ducks"])):
        raise HTTPException(404, "no such duck")
    d = p["ducks"][i]
    if d["lv"] >= MAX_LV:
        return view(p, "وصلت البطة لأعلى مستوى")
    cost = upg_cost(d["lv"])
    if p["coins"] < cost:
        return view(p, f"تحتاج {cost - p['coins']} عملة إضافية")
    settle(p, time.time())
    p["coins"] -= cost; d["lv"] += 1
    save(p)
    return view(p, f"ترقّت {TYPES[d['t']][0]} إلى المستوى {d['lv']}")


@app.post("/api/offer/{i}")
def offer(i: int, authorization: str = Header("")):
    p = player(authorization)
    now = time.time()
    slot = int(now // SLOT_SEC)
    offs = offers(slot)
    if not (0 <= i < len(offs)):
        raise HTTPException(404, "no such offer")
    if p["shop_slot"] != slot:
        p["shop_slot"], p["shop_bought"] = slot, []
    o = offs[i]
    if i in p["shop_bought"]:
        return view(p, "اشتريت هذا العرض من قبل")
    if p["coins"] < o["price"]:
        return view(p, f"تحتاج {o['price'] - p['coins']} عملة إضافية")
    settle(p, now)
    p["coins"] -= o["price"]
    p["shop_bought"].append(i)
    if o["kind"] == "duck":
        p["ducks"].append({"t": o["ref"], "lv": 1}); name = TYPES[o["ref"]][0]
    else:
        p["items"][o["ref"]] = p["items"].get(o["ref"], 0) + 1; name = ITEMS[o["ref"]][0]
    save(p)
    return view(p, f"اشتريت {name}")


@app.post("/api/equip/{i}/{item}")
def equip(i: int, item: str, authorization: str = Header("")):
    p = player(authorization)
    if not (0 <= i < len(p["ducks"])):
        raise HTTPException(404, "no such duck")
    d, cur = p["ducks"][i], p["ducks"][i].get("it")
    if item != "none" and (item not in ITEMS or p["items"].get(item, 0) < 1):
        return view(p, "لا تملك هذا العنصر")
    settle(p, time.time())
    if cur:
        p["items"][cur] = p["items"].get(cur, 0) + 1
        d.pop("it", None)
    if item != "none":
        p["items"][item] -= 1; d["it"] = item
    save(p)
    return view(p, "تم التزيين" if item != "none" else "أُزيل العنصر")


@app.post("/api/ev_claim")
def ev_claim(authorization: str = Header("")):
    p = player(authorization)
    touch_event(p, time.time())
    got = 0
    while p["ev_claim"] < len(EV_MILES) and p["ev_score"] >= EV_MILES[p["ev_claim"]][0]:
        got += EV_MILES[p["ev_claim"]][1]; p["ev_claim"] += 1
    p["coins"] += got
    save(p)
    return view(p, f"استلمت {got} عملة من السباق" if got else "لا مكافآت جاهزة بعد")


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


@app.post("/api/claim")
def claim(authorization: str = Header("")):
    p = player(authorization)
    if time.time() < p["last_claim"] + 86400:
        return view(p, "المكافأة اليومية غير جاهزة بعد")
    p["coins"] += 100; p["last_claim"] = time.time()
    save(p)
    return view(p, "حصلت على 100 عملة")


@app.post("/api/task/{task_id}")
def task(task_id: str, authorization: str = Header("")):
    p = player(authorization)
    if task_id not in TASKS:
        raise HTTPException(404, "unknown task")
    _, reward, check = TASKS[task_id]
    if task_id in p["tasks"]:
        return view(p, "أخذت هذه المكافأة من قبل")
    if not check(p):
        return view(p, "لم تكمل المهمة بعد")
    p["tasks"].append(task_id); p["coins"] += reward
    save(p)
    return view(p, f"أُنجزت المهمة: +{reward} عملة")


@app.get("/api/leaderboard")
def leaderboard(by: str = "coins", authorization: str = Header("")):
    p = player(authorization)
    eid = event_id(time.time())
    if by == "event":
        mine = p["ev_score"] if p["ev_id"] == eid else 0
        rows = db.execute("SELECT id, name, ev_score FROM players WHERE ev_id=? ORDER BY ev_score DESC, id LIMIT 20", (eid,)).fetchall()
        rank = db.execute("SELECT COUNT(*) + 1 FROM players WHERE ev_id=? AND ev_score > ?", (eid, mine)).fetchone()[0]
    else:
        col = "json_array_length(ducks)" if by == "ducks" else "coins"   # قيمتان ثابتتان فقط
        mine = len(p["ducks"]) if by == "ducks" else p["coins"]
        rows = db.execute(f"SELECT id, name, {col} FROM players ORDER BY {col} DESC, id LIMIT 20").fetchall()
        rank = db.execute(f"SELECT COUNT(*) + 1 FROM players WHERE {col} > ?", (mine,)).fetchone()[0]
    return {"by": by, "me": {"rank": rank, "value": mine},
            "top": [{"rank": i + 1, "name": (n or "لاعب")[:16], "value": v, "me": uid == p["id"]}
                    for i, (uid, n, v) in enumerate(rows)]}


@app.post("/webhook/{secret}")
def webhook(secret: str, update: dict = Body(...)):
    if not WEBHOOK_SECRET or not hmac.compare_digest(secret, WEBHOOK_SECRET):
        raise HTTPException(404)
    msg = update.get("message") or {}
    if (msg.get("text") or "").startswith("/start") and WEBAPP_URL:
        body = {"chat_id": msg["chat"]["id"], "text": "أهلاً بك في مزرعة البط! اكسر البيض وجمّع البط.",
                "reply_markup": {"inline_keyboard": [[{"text": "العب الآن", "web_app": {"url": WEBAPP_URL}}]]}}
        try:
            urlopen(UrlRequest(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json.dumps(body).encode(),
                               {"Content-Type": "application/json"}), timeout=5)
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


# صور البط والبيض والعناصر (SVG). لاستبدال أي صورة غيّر نصها هنا.
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
    'e0': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 112\"><defs><radialGradient id=\"g\" cx=\".35\" cy=\".3\" r=\".9\"><stop offset=\"0\" stop-color=\"#ffc977\"/><stop offset=\".6\" stop-color=\"#f0a23b\"/><stop offset=\"1\" stop-color=\"#b9611c\"/></radialGradient></defs><path d=\"M50 6 C78 6 92 50 92 72 C92 92 74 106 50 106 C26 106 8 92 8 72 C8 50 22 6 50 6Z\" fill=\"url(#g)\"/><path d=\"M30 50 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M54 44 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M24 74 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M48 70 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M68 66 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M38 92 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M62 90 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><ellipse cx=\"34\" cy=\"34\" rx=\"7\" ry=\"12\" fill=\"#fff\" opacity=\".28\" transform=\"rotate(-20 34 34)\"/></svg>",
    'e1': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 112\"><defs><radialGradient id=\"g\" cx=\".35\" cy=\".3\" r=\".9\"><stop offset=\"0\" stop-color=\"#a8e6ff\"/><stop offset=\".6\" stop-color=\"#2fa8e0\"/><stop offset=\"1\" stop-color=\"#13537e\"/></radialGradient></defs><path d=\"M50 6 C78 6 92 50 92 72 C92 92 74 106 50 106 C26 106 8 92 8 72 C8 50 22 6 50 6Z\" fill=\"url(#g)\"/><path d=\"M30 50 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M54 44 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M24 74 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M48 70 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M68 66 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M38 92 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M62 90 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><ellipse cx=\"34\" cy=\"34\" rx=\"7\" ry=\"12\" fill=\"#fff\" opacity=\".28\" transform=\"rotate(-20 34 34)\"/></svg>",
    'e2': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 112\"><defs><radialGradient id=\"g\" cx=\".35\" cy=\".3\" r=\".9\"><stop offset=\"0\" stop-color=\"#c2f5a8\"/><stop offset=\".6\" stop-color=\"#4fbf5a\"/><stop offset=\"1\" stop-color=\"#1f6b34\"/></radialGradient></defs><path d=\"M50 6 C78 6 92 50 92 72 C92 92 74 106 50 106 C26 106 8 92 8 72 C8 50 22 6 50 6Z\" fill=\"url(#g)\"/><path d=\"M30 50 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M54 44 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M24 74 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M48 70 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M68 66 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M38 92 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M62 90 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><ellipse cx=\"34\" cy=\"34\" rx=\"7\" ry=\"12\" fill=\"#fff\" opacity=\".28\" transform=\"rotate(-20 34 34)\"/></svg>",
    'e3': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 112\"><defs><radialGradient id=\"g\" cx=\".35\" cy=\".3\" r=\".9\"><stop offset=\"0\" stop-color=\"#e6c2ff\"/><stop offset=\".6\" stop-color=\"#9b5de5\"/><stop offset=\"1\" stop-color=\"#4a2380\"/></radialGradient></defs><path d=\"M50 6 C78 6 92 50 92 72 C92 92 74 106 50 106 C26 106 8 92 8 72 C8 50 22 6 50 6Z\" fill=\"url(#g)\"/><path d=\"M30 50 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M54 44 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M24 74 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M48 70 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M68 66 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M38 92 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M62 90 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><ellipse cx=\"34\" cy=\"34\" rx=\"7\" ry=\"12\" fill=\"#fff\" opacity=\".28\" transform=\"rotate(-20 34 34)\"/></svg>",
    'e4': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 112\"><defs><radialGradient id=\"g\" cx=\".35\" cy=\".3\" r=\".9\"><stop offset=\"0\" stop-color=\"#ffc0d2\"/><stop offset=\".6\" stop-color=\"#ee5f8a\"/><stop offset=\"1\" stop-color=\"#8a2547\"/></radialGradient></defs><path d=\"M50 6 C78 6 92 50 92 72 C92 92 74 106 50 106 C26 106 8 92 8 72 C8 50 22 6 50 6Z\" fill=\"url(#g)\"/><path d=\"M30 50 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M54 44 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M24 74 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M48 70 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M68 66 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M38 92 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><path d=\"M62 90 q8 -8 16 0 q-8 8 -16 0Z\" fill=\"#a8541a\" opacity=\".35\"/><ellipse cx=\"34\" cy=\"34\" rx=\"7\" ry=\"12\" fill=\"#fff\" opacity=\".28\" transform=\"rotate(-20 34 34)\"/></svg>",
    'lock': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 112\"><path d=\"M50 6C78 6 92 50 92 72C92 92 74 106 50 106C26 106 8 92 8 72C8 50 22 6 50 6Z\" fill=\"#3a4658\"/><rect x=\"34\" y=\"52\" width=\"32\" height=\"26\" rx=\"5\" fill=\"#9fb0c6\"/><path d=\"M40 52V44a10 10 0 0 1 20 0V52\" fill=\"none\" stroke=\"#9fb0c6\" stroke-width=\"6\"/></svg>",
    'i_cap': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 -12 100 112\"><path d=\"M30 30A20 20 0 0 1 70 30Z\" fill=\"#e5384f\"/><rect x=\"26\" y=\"28\" width=\"48\" height=\"6\" rx=\"3\" fill=\"#b32436\"/><path d=\"M62 31h16q4 0 4 4H62Z\" fill=\"#b32436\"/></svg>",
    'i_shades': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 -12 100 112\"><rect x=\"33\" y=\"27\" width=\"16\" height=\"11\" rx=\"4\" fill=\"#1d1b2a\"/><rect x=\"51\" y=\"27\" width=\"16\" height=\"11\" rx=\"4\" fill=\"#1d1b2a\"/><rect x=\"47\" y=\"30\" width=\"6\" height=\"3\" fill=\"#1d1b2a\"/><path d=\"M36 30l5-1\" stroke=\"#fff\" stroke-width=\"1.5\" opacity=\".6\"/></svg>",
    'i_bow': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 -12 100 112\"><path d=\"M50 58L35 50V66ZM50 58L65 50V66Z\" fill=\"#ff5fa2\"/><circle cx=\"50\" cy=\"58\" r=\"4.5\" fill=\"#d6347c\"/></svg>",
    'i_scarf': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 -12 100 112\"><path d=\"M29 56Q50 66 71 56V64Q50 74 29 64Z\" fill=\"#2bd4b5\"/><rect x=\"60\" y=\"63\" width=\"9\" height=\"17\" rx=\"3\" fill=\"#1aa890\"/></svg>",
    'i_phones': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 -12 100 112\"><path d=\"M27 36A23 23 0 0 1 73 36\" fill=\"none\" stroke=\"#3a3f4f\" stroke-width=\"5\"/><rect x=\"21\" y=\"32\" width=\"10\" height=\"17\" rx=\"4\" fill=\"#4aa8ff\"/><rect x=\"69\" y=\"32\" width=\"10\" height=\"17\" rx=\"4\" fill=\"#4aa8ff\"/></svg>",
    'i_tiara': "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 -12 100 112\"><polygon points=\"33,19 35,3 43,12 50,0 57,12 65,3 67,19\" fill=\"#ffd23f\" stroke=\"#c98a00\" stroke-width=\"2\" stroke-linejoin=\"round\"/><circle cx=\"50\" cy=\"12\" r=\"2.5\" fill=\"#4aa8ff\"/><circle cx=\"40\" cy=\"14\" r=\"1.8\" fill=\"#ff5fa2\"/><circle cx=\"60\" cy=\"14\" r=\"1.8\" fill=\"#5fd38d\"/></svg>",
}
