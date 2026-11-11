#!/usr/bin/env python3
# ============================================================
#   MRPTech Auto Square Posting Bot  ·  v2.4 (official endpoint)
#   Official Binance Square OpenAPI · X-Square-OpenAPI-Key
#   Official endpoint · official response codes · zero fake data
# ============================================================
import os, re, sys, time, json, random, hashlib, logging
from datetime import datetime, timezone, timedelta

import requests
from apscheduler.schedulers.blocking import BlockingScheduler

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------- CONFIG ----------------
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://api.mwapi.dev/v1")
AI_API_KEY  = os.environ.get("AI_API_KEY", "")
AI_MODEL    = os.environ.get("AI_MODEL", "claude-opus-4-8")

SQ_API_KEY = os.environ.get("SQ_API_KEY", "")      # X-Square-OpenAPI-Key
SQ_BASE    = "https://www.binance.com"

LANG       = os.environ.get("LANG", "zh")
MIN_GAP    = 30
COOLDOWN_H = 4
POOL_SIZE  = 40
STALE_HRS  = 6
STATE_FILE = "mrp_state.json"
FAPI       = "https://fapi.binance.com"
BLACKLIST  = {"USDCUSDT","FDUSDUSDT","TUSDUSDT","BUSDUSDT","DAIUSDT"}
PUBLISH_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    handlers=[logging.FileHandler("mrptech.log", encoding="utf-8"),
              logging.StreamHandler()])
log = logging.getLogger("MRPTech")

# ---------------- BANNER ----------------
def banner():
    rows = [
        "█▀▄▀█ █▀█ █▀█ ▀█▀ █▀▀ █▀▀ █░█",
        "█░▀░█ █▀▄ █▀▀ ░█░ ██▄ █▄▄ █▀█",
    ]
    print("\033[96m" + "\n".join(rows) + "\033[0m")
    print("      ⚡ MRPTech · Auto Square Posting Bot v1.0 ⚡")
    print("   Official API · Official Endpoint · Deduped · Guarded\n")

# ---------------- TARGET MENU ----------------
def ask_daily_target():
    print(" Choose daily post target:")
    print("   [1] Quality   5/day    <- safest")
    print("   [2] Light    12/day")
    print("   [3] Standard 24/day")
    print("   [4] Active   48/day")
    print("   [5] Max      60/day")
    print("   [6] Custom number")
    choice = input(" Selection: ").strip() or "3"
    mapping = {"1":5,"2":12,"3":24,"4":48,"5":60}
    if choice == "6":
        try: n = int(input(" Enter number (5-96): "))
        except ValueError: n = 24
    else:
        n = mapping.get(choice, 24)
    return max(5, min(n, 96))

# ---------------- STATE ----------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return {}

def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)

state = load_state()
state.setdefault("day_ts", 0); state.setdefault("posted_today", [])
state.setdefault("last_post_ts", 0); state.setdefault("per_symbol", {})
state.setdefault("recent_hashes", []); state.setdefault("paused_until", 0)
state.setdefault("pool", {"symbols": [], "ts": 0})

def day_start(now=None):
    now = now or time.time()
    cn = datetime.fromtimestamp(now, timezone(timedelta(hours=8)))
    d = cn.replace(hour=8, minute=0, second=0, microsecond=0)
    if cn < d: d -= timedelta(days=1)
    return d.timestamp()

def prune():
    cut = day_start()
    if state["day_ts"] != int(cut):
        state["day_ts"], state["posted_today"] = int(cut), []
    state["posted_today"] = [t for t in state["posted_today"] if t >= cut]
    for s in list(state["per_symbol"]):
        k = [t for t in state["per_symbol"][s] if t >= cut]
        if not k: del state["per_symbol"][s]
    state["recent_hashes"] = state["recent_hashes"][-150:]

# ---------------- HTTP ----------------
def http_get(url, params=None, timeout=15):
    r = requests.get(url, params=params, timeout=timeout); r.raise_for_status(); return r.json()

# ---------------- DATA LAYER ----------------
def all_symbols():
    info = http_get(f"{FAPI}/fapi/v1/exchangeInfo")
    return [s["symbol"] for s in info.get("symbols", [])
            if s.get("status")=="TRADING" and s.get("quoteAsset")=="USDT"
            and s.get("contractType")=="PERPETUAL" and s["symbol"] not in BLACKLIST]

def refresh_pool():
    p = state["pool"]
    if not p["symbols"] or time.time()-p["ts"] > STALE_HRS*3600:
        try:
            p["symbols"], p["ts"] = all_symbols(), time.time()
            log.info("Universe refreshed: %d pairs", len(p["symbols"]))
        except Exception as e:
            log.warning("Pool refresh failed: %s", e)
    return p["symbols"]

def top_movers(n):
    live = set(refresh_pool())
    tk = http_get(f"{FAPI}/fapi/v1/ticker/24hr")
    scored=[]
    for t in tk:
        s=t.get("symbol","")
        if s not in live: continue
        try: v=float(t["quoteVolume"]); c=float(t["priceChangePercent"])
        except (ValueError,KeyError): continue
        scored.append((v*(1+abs(c)/10), s))
    scored.sort(reverse=True)
    return [x[1] for x in scored[:n]]

def snapshot(sym):
    t  = http_get(f"{FAPI}/fapi/v1/ticker/24hr", {"symbol": sym})
    oi = http_get(f"{FAPI}/fapi/v1/openInterest", {"symbol": sym})
    fr = None
    try: fr = float(http_get(f"{FAPI}/fapi/v1/premiumIndex",{"symbol":sym})["lastFundingRate"])
    except Exception: pass
    kl = http_get(f"{FAPI}/fapi/v1/klines", {"symbol": sym, "interval":"5m", "limit":13})
    cl = [float(k[4]) for k in kl]
    h1 = (cl[-1]-cl[0])/cl[0]*100 if cl[0] else 0.0
    return {"coin": sym.replace("USDT",""), "price": float(t["lastPrice"]),
            "chg": float(t["priceChangePercent"]), "high": float(t["highPrice"]),
            "low": float(t["lowPrice"]), "vol": float(t["quoteVolume"]),
            "oi": float(oi.get("openInterest",0)), "fund": fr,
            "h1": h1, "prev_c": cl[0], "last_c": cl[-1]}

# ---------------- STYLE ENGINE ----------------
STYLES = {
 "plain":  "Plain talk: casual short sentences, honest hedge at the end like 我说的也可能错，别照着做。",
 "tech":   "Technical: calm analyst, mention structure/MA/volume.",
 "news":   "Newsflash style: start with 刚刚：or 📢 then the fact, no opinion.",
 "data":   "Data card: 📊 emoji, lead with funding rate or OI numbers, annualized %, ranking.",
 "flow":   "Money flow: 🐋 whale/fund-flow framing, 15分钟/30分钟 window.",
 "hype":   "Hype: 兄弟们！energy but factual, still include one hedge line.",
 "joke":   "Jokester: light humor, self-deprecating ending.",
}

SYSTEM_PROMPT = """You write Binance Square posts. HARD RULES:
1. Use ONLY the exact numbers provided. NEVER invent prices, percentages, events, news or quotes. If data missing, skip mentioning it.
2. No promises, no 'guaranteed', no buy/sell calls, no insider claims.
3. Include $COIN tag for the coin. Add exactly 2 relevant hashtags at the end.
4. 2-4 sentences total. Human trader voice, not robotic marketing.
5. End with an honest disclaimer line when opinion-ish (e.g. 数据仅供参考，不构成建议。 / Data for reference only.).
Output ONLY the post text."""

def build_material(s, style_key):
    fund = f"{s['fund']*100:.3f}%" if s["fund"] is not None else None
    ann  = f"{s['fund']*3*365*100:.1f}%" if s["fund"] is not None else None
    lines = [f"Coin ${s['coin']}", f"Price {s['price']:g}",
             f"24h change {s['chg']:+.2f}%", f"1h change {s['h1']:+.2f}% ({s['prev_c']:g}->{s['last_c']:g})",
             f"24h range {s['low']:g}-{s['high']:g}", f"24h volume {s['vol']/1e6:,.0f}M USDT",
             f"Funding {fund}" + (f" (annualized {ann})" if ann else ""),
             f"Open interest {s['oi']:,.0f}"]
    material = "\n".join(l for l in lines if "None" not in l)
    return f"STYLE: {STYLES[style_key]}\nVERIFIED DATA (only source of truth):\n{material}"

def fingerprint(txt):
    core = re.sub(r"[^a-z\u4e00-\u9fff]+","",txt.lower())
    return hashlib.md5(core.encode()).hexdigest()

BANNED = ["稳赚","必涨","必跌","内幕消息","带单","保证收益","guarantee profit",
          "insider","risk-free","no loss","躺赚","跟单暴富","rugged","to the moon guaranteed"]

def clean(txt):
    txt = re.sub(r"\s+"," ",txt).strip()
    low = txt.lower()
    if any(b.lower() in low for b in BANNED): return None
    if len(txt) < 20 or len(txt) > 400: return None
    return txt

def ask_ai(material):
    payload = {"model": AI_MODEL,
               "messages":[{"role":"system","content":SYSTEM_PROMPT},
                           {"role":"user","content":f"language={LANG}\n\n{material}"}],
               "temperature":0.9,"max_tokens":220}
    r = requests.post(AI_BASE_URL.rstrip("/")+"/chat/completions", json=payload,
                      headers={"Authorization":f"Bearer {AI_API_KEY}","Content-Type":"application/json"},
                      timeout=40)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def make_candidate():
    syms = top_movers(POOL_SIZE); random.shuffle(syms)
    styles = list(STYLES.keys()); now = time.time()
    for sym in syms:
        recent = state["per_symbol"].get(sym, [])
        if len(recent) >= 2: continue
        try: s = snapshot(sym)
        except Exception as e:
            log.debug("skip %s (%s)", sym, e); continue
        moved = abs(s["chg"])>=2.0 or abs(s["h1"])>=1.0
        too_soon = any(now-t < COOLDOWN_H*3600 for t in recent)
        if too_soon and not moved: continue
        if not moved and not recent and random.random()>0.35: continue
        draft = None
        for _ in range(2):
            try: raw = ask_ai(build_material(s, random.choice(styles)))
            except Exception as e:
                log.warning("AI error: %s", e); break
            draft = clean(raw)
            if draft and fingerprint(draft) not in state["recent_hashes"]: break
            draft = None
        if draft: return draft, sym, s
    return None

# ---------------- PUBLISH (OFFICIAL ENDPOINT) ----------------
def publish(ctx, text):
    body = {"bodyTextOnly": text}
    headers = {
        "X-Square-OpenAPI-Key": SQ_API_KEY,
        "Content-Type": "application/json",
        "clienttype": "binanceSkill",
    }
    try:
        r = requests.post(PUBLISH_URL, json=body, headers=headers, timeout=20)
    except requests.RequestException as e:
        log.error("Network error publishing: %s", e); return False
    try: data = r.json()
    except ValueError:
        log.error("Bad response %s: %s", r.status_code, r.text[:150]); return False
    ok = data.get("code") == "000000"
    if not ok:
        log.error("Publish rejected: code=%s msg=%s",
                  data.get("code"), str(data.get("msg"))[:150])
        return False
    post_id = data.get("data", {}).get("id")
    live = f"https://www.binance.com/square/post/{post_id}" if post_id else ""
    log.info("✅ Published #%d/%d · next %s · %s",
             len(state["posted_today"]), ctx["target"],
             datetime.fromtimestamp(time.time()+MIN_GAP*60).strftime("%H:%M"),
             live[:120] if live else "")
    return True

# ---------------- GUARDS ----------------
def posting_window_ok(mode):
    hour = datetime.now(timezone(timedelta(hours=8))).hour
    if mode=="allday": return True
    if mode=="recommended": return hour>=6 or hour<1
    if mode=="evenings": return hour>=18 or hour<1
    return True

def can_post(target, mode):
    prune()
    now=time.time()
    if now < state.get("paused_until",0):
        log.info("Paused until %s (restriction guard).",
                 datetime.fromtimestamp(state["paused_until"]).strftime("%d %H:%M")); return False
    if len(state["posted_today"]) >= target: return False
    if now - state["last_post_ts"] < MIN_GAP*60: return False
    if not posting_window_ok(mode): return False
    return True

# ---------------- MAIN LOOP ----------------
def tick(ctx):
    if ctx.get("paused_by_user"):
        log.info("User-paused. Type 'resume' to continue."); return
    if not SQ_API_KEY:
        log.error("Missing SQ_API_KEY in .env"); return
    if not can_post(ctx["target"], ctx["mode"]):
        log.info("Guard idle · today=%d/%d", len(state["posted_today"]), ctx["target"]); return
    res = make_candidate()
    if not res:
        log.info("No eligible candidate this tick."); return
    text, sym, s = res
    log.info("Drafted [%s %+0.2f%%]: %s", sym, s["chg"], text[:80].replace("\n"," "))
    if publish(ctx, text):
        now=time.time()
        state["posted_today"].append(now); state["last_post_ts"]=now
        state["per_symbol"].setdefault(sym,[]).append(now)
        state["recent_hashes"].append(fingerprint(text)); save_state(state)

# ---------------- LIVE COMMANDS ----------------
def control_console(ctx):
    import threading
    def loop():
        print("\nLive commands -> status | pause | resume | set <5-96> (current: %d/day)" % ctx["target"])
        while True:
            try: cmd = input("").strip().lower()
            except EOFError: break
            if cmd.startswith("set "):
                try:
                    n=max(5,min(int(cmd.split()[1]),96)); ctx["target"]=n
                    log.info("🎯 Daily target changed to %d (applies immediately)", n)
                except (ValueError, IndexError): log.warning("Usage: set 40")
            elif cmd=="pause": ctx["paused_by_user"]=True; log.info("⏸ User paused")
            elif cmd=="resume": ctx["paused_by_user"]=False; log.info("▶ Resumed")
            elif cmd=="status":
                log.info("today=%d/%d · last=%s · pool=%d",
                         len(state["posted_today"]), ctx["target"],
                         datetime.fromtimestamp(state["last_post_ts"]).strftime("%H:%M") if state["last_post_ts"] else "-",
                         len(state["pool"]["symbols"]))
            elif cmd=="help":
                print("commands: set 40 | pause | resume | status | help")
    threading.Thread(target=loop, daemon=True).start()

if __name__ == "__main__":
    banner()
    if not AI_API_KEY or not SQ_API_KEY:
        print("❌ Fill .env first: AI_API_KEY and SQ_API_KEY"); sys.exit(1)
    target = ask_daily_target()
    print(" Auto-post hours [1]All-day [2]Recommended 06:00-01:00 [3]Evenings: ", end="")
    mode = {"1":"allday","2":"recommended","3":"evenings"}.get(input().strip() or "2","recommended")
    ctx = {"target":target, "mode":mode, "paused_by_user":False}
    log.info("START · target=%d/day · gap=%dmin · lang=%s · model=%s", target, MIN_GAP, LANG, AI_MODEL)
    sched = BlockingScheduler(timezone="Asia/Shanghai")
    sched.add_job(tick, "interval", minutes=5, jitter=60, max_instances=1, kwargs={"ctx":ctx})
    control_console(ctx)
    try: sched.start()
    except (KeyboardInterrupt, SystemExit): log.info("Stopped.")