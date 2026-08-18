"""
MEXC Futures Long/Short Setup Bot
---------------------------------
Scans MEXC USDT-perpetual futures for LONG and SHORT setups using
confluence scoring — an alert only fires when multiple independent
conditions line up at once.

LONG conditions:
  L1. Buy-side volume spike  (5m volume >= SPIKE_MULTIPLIER x 2h median, green candle)
  L2. Breakout               (close above the highest high of the 2h window)
  L3. Negative funding       (shorts are crowded and paying -> squeeze fuel)
  L4. Open interest rising with price rising (new money entering long)

SHORT conditions:
  S1. Sell-side volume spike (big volume, red candle) OR rejection wick
      (long upper wick, close near lows -> big money sold the pump)
  S2. Breakdown              (close below the lowest low of the 2h window)
  S3. Overheated funding     (longs crowded and paying heavily)
  S4. Open interest rising with price falling (new shorts in control)

An alert fires when a volume event occurs AND total score >= ALERT_SCORE_MIN.
3-4 conditions = STRONG setup. 2 = standard setup.

No API key needed. Alerts go to Telegram with your referral link.

Usage:
    pip install requests
    python mexc_whale_futures.py
"""

import time
import statistics
import os
import json
import requests

# ============================= CONFIG =============================

FUTURES_BASE = "https://contract.mexc.com"

# Universe: USDT perpetuals with 24h turnover in this range get watched.
MIN_24H_TURNOVER = 500_000       # skip dead contracts (high-conviction floor)
MAX_24H_TURNOVER = 60_000_000    # skip true majors, but keep mid-move runners in the universe
UNIVERSE_REFRESH_SEC = 30 * 60

# Volume spike (5m candles):
BASELINE_CANDLES = 24            # 2h window
SPIKE_MULTIPLIER = 12
MIN_SPIKE_TURNOVER = 50_000      # spike candle must move >= this much USDT

# Confluence:
ALERT_SCORE_MIN = 3              # conditions needed to alert (3=high conviction)
FUNDING_NEG_THRESHOLD = -0.0001  # funding <= this counts as "shorts paying" (L3)
FUNDING_HOT_THRESHOLD = 0.0005   # funding >= this counts as "longs overheated" (S3)
OI_CHANGE_PCT = 3.0              # open interest must move >= this % over ~1h (L4/S4)
WICK_BODY_RATIO = 2.0            # upper wick >= 2x body counts as rejection (S1)

SCAN_INTERVAL_SEC = 120
ALERT_COOLDOWN_SEC = 60 * 60     # per symbol per side
REQUEST_PAUSE_SEC = 0.08

# Position sizing (shown in every alert):
UNIT_STAKE = 1000                # your stake per trade, USDT
RISK_PCT = 1.0                   # % of stake risked if the stop hits
MAX_LEVERAGE = 10                # never suggest above this
STOP_BUFFER_PCT = 0.15           # stop placed this % beyond the spike candle's low/high

# Telegram:
TELEGRAM_BOT_TOKEN = "8844104818:AAGQyrXGs0fMWEXW9Cec5_nZjmU70ivmKC0"

# Every alert is sent to each destination below.
# message_thread_id targets a specific topic in a forum-style group.
TELEGRAM_DESTINATIONS = [
    {"chat_id": "7088997548"},        # you (private)
    {"chat_id": "@jwsalerts"},        # JWS Trade Alerts — public channel (also feeds the website)
]

# =====================================================================
# X (TWITTER) AUTO-POSTING — optional
#
# Setup:
#   1. developer.x.com → sign in with the account that should post →
#      create a Project + App (Free tier is fine).
#   2. App settings → User authentication set up → App permissions:
#      READ AND WRITE → save.
#   3. Keys & Tokens tab → generate all four and paste below:
#      API Key + Secret (a.k.a. consumer keys) and
#      Access Token + Secret (for YOUR account).
#   4. On this PC:  pip install tweepy
#   5. X rules: mark the account as automated (X profile settings →
#      Your account → Automation) — required for bot accounts.
#
# Leave the keys blank to disable X posting entirely.
# Free tier allows ~500 posts/month — hence STRONG-only + daily cap.
# =====================================================================
X_API_KEY = ""
X_API_SECRET = ""
X_ACCESS_TOKEN = ""
X_ACCESS_SECRET = ""
X_MIN_SCORE = 3          # only STRONG setups (score >= this) go to X
X_MAX_PER_DAY = 12       # hard daily cap, protects the monthly limit

# ---- RUNNER tier: catch power moves already underway (ticker-only, free) ----
RUNNER_MIN_RISE = 50.0        # 24h % gain to qualify as a runner (LONG)
RUNNER_MIN_FALL = -40.0       # 24h % drop to qualify (SHORT)
RUNNER_NEAR_EXTREME_PCT = 3.0 # must be within this % of the 24h high/low (still pressing)
RUNNER_MIN_TURNOVER = 1_000_000     # 24h turnover floor — real money only
RUNNER_MAX_TURNOVER = 150_000_000   # ceiling — beyond this it's a major, not a runner
RUNNER_COOLDOWN_SEC = 4 * 3600      # re-alert cadence while the move keeps running
RUNNER_X_MIN_RISE = 100.0           # runners this big also post to X

# ---- VWAP tier: hourly reclaim/loss of the rolling 24h VWAP ----
VWAP_MIN_24H_TURNOVER = 2_000_000   # liquid enough for VWAP to mean something
VWAP_WINDOW = 24                    # hours in the rolling VWAP
VWAP_VOL_MULT = 1.5                 # signal candle turnover vs window average
VWAP_MAX_SIGMA = 1.5                # skip if already stretched beyond this many σ from VWAP
VWAP_COOLDOWN_SEC = 4 * 3600        # per symbol per side
VWAP_SCAN_MINUTE = 3                # run just after each hourly close

# ---- Daily Setup of the Day (higher-liquidity, daily timeframe) ----
DAILY_SETUP_HOUR = 7             # local hour to post (24h clock)
DAILY_MIN_24H_TURNOVER = 5_000_000   # only liquid pairs qualify for the daily
DAILY_VOL_MULT = 1.5             # yesterday's volume vs 20-day average
DAILY_LOOKBACK = 20              # days for highs/lows/SMA/volume baseline
DAILY_MIN_SCORE = 3              # post only A-grade daily charts
DAILY_POST_WHEN_EMPTY = True     # post the "no A-grade chart today" note

REFERRAL_LINK = "https://www.mexc.com/acquisition/custom-sign-up?shareCode=mexc-JWS"
REFERRAL_EVERY_N_ALERTS = 1      # referral link on EVERY alert (rotating hook copy)
REFERRAL_HOOKS = [
    "🎁 New here? MEXC has a sign-up bonus — takes 2 min:\n{link}",
    "⚡ These setups fire on MEXC futures. Trade them where they happen:\n{link}",
    "📊 Following along? You'll need a MEXC account for these pairs:\n{link}",
    "🔻 Lower fees on the exchange this bot scans:\n{link}",
]

# ==================================================================

session = requests.Session()
oi_history = {}      # symbol -> list of (ts, open_interest), pruned to ~1h
last_alert = {}      # (symbol, side) -> unix time


# =====================================================================
# WEBSITE LIVE FEED (trade.journeywithshannon.com) — all-in-one, no extra file
#
# One-time setup:
#   1. GitHub → New repository → jws-alerts-feed → Public → create.
#      Add one file named alerts.json containing exactly:  {"alerts": []}
#   2. GitHub → Settings → Developer settings → Personal access tokens →
#      Fine-grained token → repo access ONLY jws-alerts-feed →
#      Contents = Read and write → generate + copy.
#   3. On this PC (PowerShell):  setx JWS_FEED_TOKEN "github_pat_XXXX"
#      then restart this bot's terminal.
# No token set = feed silently skipped; Telegram is never affected.
# =====================================================================
import base64 as _b64

try:
    import tweepy  # pip install tweepy
except ImportError:
    tweepy = None

FEED_REPO = "Parro95/jws-alerts-feed"
FEED_API = f"https://api.github.com/repos/{FEED_REPO}/contents/alerts.json"
FEED_KEEP = 20  # newest alerts kept on the website
TRACK_HOURS = 24          # how long each alert is tracked
TRACK_TARGET_R = 2.0      # "TARGET" when favorable move reaches 2x the stop distance
TRACKER_COMMIT_MIN_SEC = 240   # min seconds between tracker commits
TRACKER_MIN_DELTA = 0.5        # or commit sooner if any chg moved this many % points


def _feed_headers():
    token = os.environ.get("JWS_FEED_TOKEN")
    if not token:
        return None
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json"}


def _feed_read(headers):
    r = requests.get(FEED_API, headers=headers, timeout=10)
    if r.status_code == 200:
        cur = r.json()
        return json.loads(_b64.b64decode(cur["content"]).decode("utf-8")), cur["sha"]
    return {"alerts": []}, None


def _feed_write(headers, data, sha, message):
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = {"message": message,
            "content": _b64.b64encode(
                json.dumps(data, separators=(",", ":")).encode()).decode()}
    if sha:
        body["sha"] = sha
    p = requests.put(FEED_API, headers=headers, json=body, timeout=10)
    return p.status_code in (200, 201)


def push_alert(pair, type, msg, entry=None, stop=None, side=None):
    """Publish a public-safe alert to the website feed. Never raises."""
    headers = _feed_headers()
    if not headers:
        return False
    try:
        data, sha = _feed_read(headers)
        alert = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "pair": pair, "type": type, "msg": msg}
        if entry:
            alert.update({"entry": round(float(entry), 10),
                          "stop": round(float(stop), 10) if stop else None,
                          "side": side or ("SHORT" if "SHORT" in type else "LONG"),
                          "st": "LIVE", "chg": 0.0, "peak": 0.0})
        data["alerts"] = ([alert] + data.get("alerts", []))[:FEED_KEEP]
        return _feed_write(headers, data, sha, f"alert: {pair}")
    except Exception:
        return False


_tracker_last_commit = 0.0


def update_tracker(tickers):
    """Re-mark every LIVE alert against current price; commit when it matters."""
    global _tracker_last_commit
    headers = _feed_headers()
    if not headers:
        return
    try:
        data, sha = _feed_read(headers)
        alerts = data.get("alerts", [])
        changed, delta = False, 0.0
        now = time.time()
        for a in alerts:
            if a.get("st") != "LIVE" or not a.get("entry"):
                continue
            try:
                age_h = (now - time.mktime(time.strptime(a["t"], "%Y-%m-%dT%H:%M:%SZ")) + time.timezone) / 3600
            except Exception:
                age_h = 0
            sym = a["pair"].replace("/", "_")
            tick = tickers.get(sym)
            if not tick or tick["price"] <= 0:
                continue
            p, entry, stop = tick["price"], a["entry"], a.get("stop")
            fav = (p - entry) / entry * 100
            if a.get("side") == "SHORT":
                fav = -fav
            fav = round(fav, 2)
            delta = max(delta, abs(fav - a.get("chg", 0.0)))
            a["chg"] = fav
            a["cur"] = round(p, 10)
            a["peak"] = round(max(a.get("peak", 0.0), fav), 2)
            r_pct = abs(entry - stop) / entry * 100 if stop else None
            stopped = stop and ((a.get("side") != "SHORT" and p <= stop) or
                                (a.get("side") == "SHORT" and p >= stop))
            if stopped:
                a["st"] = "STOPPED"
            elif r_pct and fav >= TRACK_TARGET_R * r_pct:
                a["st"] = "TARGET"
            elif age_h >= TRACK_HOURS:
                a["st"] = "CLOSED"
            if a["st"] != "LIVE":
                changed = True
        due = (now - _tracker_last_commit) >= TRACKER_COMMIT_MIN_SEC
        if changed or (due and delta >= TRACKER_MIN_DELTA):
            if _feed_write(headers, data, sha, "tracker update"):
                _tracker_last_commit = now
    except Exception as e:
        print(f"[warn] tracker update failed: {e}")


_x_client = None
_x_day = ""
_x_count = 0


def post_to_x(text):
    """Post to X. Silent no-op if keys blank / tweepy missing / cap hit."""
    global _x_client, _x_day, _x_count
    if not (tweepy and X_API_KEY and X_API_SECRET and X_ACCESS_TOKEN and X_ACCESS_SECRET):
        return False
    today = time.strftime("%Y-%m-%d")
    if today != _x_day:
        _x_day, _x_count = today, 0
    if _x_count >= X_MAX_PER_DAY:
        return False
    try:
        if _x_client is None:
            _x_client = tweepy.Client(
                consumer_key=X_API_KEY, consumer_secret=X_API_SECRET,
                access_token=X_ACCESS_TOKEN, access_token_secret=X_ACCESS_SECRET)
        _x_client.create_tweet(text=text[:280])
        _x_count += 1
        return True
    except Exception as e:
        print(f"[warn] X post failed: {e}")
        return False


def get_json(url, params=None):
    r = session.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN:
        return
    for dest in TELEGRAM_DESTINATIONS:
        payload = {"chat_id": dest["chat_id"], "text": text}
        if "message_thread_id" in dest:
            payload["message_thread_id"] = dest["message_thread_id"]
        try:
            r = session.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json=payload,
                timeout=10,
            )
            if r.status_code != 200:
                print(f"[warn] Telegram rejected message for {dest['chat_id']} "
                      f"({r.status_code}): {r.text}")
        except requests.RequestException as e:
            print(f"[warn] Telegram send failed for {dest['chat_id']}: {e}")


def fetch_all_tickers():
    """One request: price, funding rate, open interest, 24h turnover for every contract."""
    data = get_json(FUTURES_BASE + "/api/v1/contract/ticker")
    tickers = data.get("data", data) if isinstance(data, dict) else data
    out = {}
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("_USDT"):
            continue
        try:
            out[sym] = {
                "price": float(t.get("lastPrice", 0) or 0),
                "funding": float(t.get("fundingRate", 0) or 0),
                "oi": float(t.get("holdVol", 0) or 0),
                "turnover24": float(t.get("amount24", 0) or 0),
                "rise24": float(t.get("riseFallRate", 0) or 0) * 100,
                "hi24": float(t.get("high24Price", 0) or 0),
                "lo24": float(t.get("lower24Price", 0) or 0),
            }
        except (TypeError, ValueError):
            continue
    return out


def fetch_klines(symbol):
    """5m candles as list of dicts, oldest -> newest."""
    data = get_json(
        FUTURES_BASE + f"/api/v1/contract/kline/{symbol}",
        params={"interval": "Min5"},
    )
    d = data.get("data", {})
    times = d.get("time", [])
    n = len(times)
    if n == 0:
        return []
    candles = []
    for i in range(n):
        candles.append({
            "open": float(d["open"][i]),
            "close": float(d["close"][i]),
            "high": float(d["high"][i]),
            "low": float(d["low"][i]),
            "turnover": float(d["amount"][i]),
        })
    return candles[-(BASELINE_CANDLES + 1):]


def update_oi(symbol, oi):
    now = time.time()
    hist = [x for x in oi_history.get(symbol, []) if now - x[0] < 70 * 60]
    hist.append((now, oi))
    oi_history[symbol] = hist


def oi_change_pct(symbol):
    hist = oi_history.get(symbol, [])
    if len(hist) < 2:
        return 0.0
    oldest, newest = hist[0][1], hist[-1][1]
    if oldest <= 0:
        return 0.0
    return (newest - oldest) / oldest * 100


def evaluate(symbol, candles, tick):
    """Score long and short conditions. Returns (side, score, reasons, info) or None."""
    if len(candles) < BASELINE_CANDLES + 1:
        return None

    base = candles[:-1]
    cur = candles[-1]

    base_turnovers = [c["turnover"] for c in base]
    median_turnover = max(statistics.median(base_turnovers), 1.0)
    spike_mult = cur["turnover"] / median_turnover
    is_spike = spike_mult >= SPIKE_MULTIPLIER and cur["turnover"] >= MIN_SPIKE_TURNOVER
    if not is_spike:
        return None  # every setup starts with a volume event

    green = cur["close"] > cur["open"]
    red = cur["close"] < cur["open"]
    body = abs(cur["close"] - cur["open"])
    upper_wick = cur["high"] - max(cur["close"], cur["open"])
    rejection = body > 0 and (upper_wick / body) >= WICK_BODY_RATIO and not green

    window_high = max(c["high"] for c in base)
    window_low = min(c["low"] for c in base)
    pct_move = ((cur["close"] - cur["open"]) / cur["open"] * 100) if cur["open"] else 0.0

    funding = tick["funding"]
    oi_pct = oi_change_pct(symbol)

    # ---- LONG score ----
    long_score, long_reasons = 0, []
    if green:
        long_score += 1
        long_reasons.append(f"Buy spike {spike_mult:.1f}x baseline")
    if cur["close"] > window_high:
        long_score += 1
        long_reasons.append("2h breakout")
    if funding <= FUNDING_NEG_THRESHOLD:
        long_score += 1
        long_reasons.append(f"Negative funding {funding * 100:.4f}% (short squeeze fuel)")
    if oi_pct >= OI_CHANGE_PCT and green:
        long_score += 1
        long_reasons.append(f"OI +{oi_pct:.1f}% ~1h (new longs)")

    # ---- SHORT score ----
    short_score, short_reasons = 0, []
    if red or rejection:
        short_score += 1
        short_reasons.append("Rejection wick — pump sold into" if rejection
                             else f"Sell spike {spike_mult:.1f}x baseline")
    if cur["close"] < window_low:
        short_score += 1
        short_reasons.append("2h breakdown")
    if funding >= FUNDING_HOT_THRESHOLD:
        short_score += 1
        short_reasons.append(f"Overheated funding +{funding * 100:.4f}% (longs crowded)")
    if oi_pct >= OI_CHANGE_PCT and red:
        short_score += 1
        short_reasons.append(f"OI +{oi_pct:.1f}% ~1h (new shorts in control)")

    side, score, reasons = ("LONG", long_score, long_reasons) \
        if long_score >= short_score else ("SHORT", short_score, short_reasons)
    if score < ALERT_SCORE_MIN:
        return None

    info = {
        "price": cur["close"], "pct_move": pct_move,
        "turnover": cur["turnover"], "mult": spike_mult,
        "funding": funding, "oi_pct": oi_pct,
        "candle_low": cur["low"], "candle_high": cur["high"],
    }
    return side, score, reasons, info


def position_plan(side, info):
    """Risk-based sizing: risk RISK_PCT of UNIT_STAKE, stop beyond the spike candle."""
    entry = info["price"]
    if entry <= 0:
        return ""
    if side == "LONG":
        stop = info["candle_low"] * (1 - STOP_BUFFER_PCT / 100)
        stop_dist = (entry - stop) / entry
    else:
        stop = info["candle_high"] * (1 + STOP_BUFFER_PCT / 100)
        stop_dist = (stop - entry) / entry
    if stop_dist <= 0:
        return ""

    risk_usd = UNIT_STAKE * RISK_PCT / 100
    notional = risk_usd / stop_dist          # position size that loses risk_usd at the stop
    leverage = notional / UNIT_STAKE

    capped = ""
    if leverage > MAX_LEVERAGE:
        leverage = MAX_LEVERAGE
        notional = UNIT_STAKE * MAX_LEVERAGE
        actual_risk = notional * stop_dist
        capped = f" (capped — risk becomes ${actual_risk:,.0f})"
        risk_usd = actual_risk
    elif leverage < 1:
        leverage = 1
        # sizing below 1x just means using part of the stake, notional stays as computed

    qty = notional / entry
    return (
        f"📐 Plan for ${UNIT_STAKE:,} stake (risk {RISK_PCT:.0f}% = ${UNIT_STAKE * RISK_PCT / 100:,.0f}):\n"
        f"  Entry ~{entry:.8g} | Stop {stop:.8g} ({stop_dist * 100:.2f}% away)\n"
        f"  Size: ${notional:,.0f} ≈ {qty:,.2f} units\n"
        f"  Leverage: ~{leverage:.1f}x{capped}\n"
    )


def fetch_daily_klines(symbol):
    data = get_json(
        FUTURES_BASE + f"/api/v1/contract/kline/{symbol}",
        params={"interval": "Day1"},
    )
    d = data.get("data", {})
    n = len(d.get("time", []))
    if n == 0:
        return []
    candles = []
    for i in range(n):
        candles.append({
            "open": float(d["open"][i]),
            "close": float(d["close"][i]),
            "high": float(d["high"][i]),
            "low": float(d["low"][i]),
            "turnover": float(d["amount"][i]),
        })
    return candles[-(DAILY_LOOKBACK + 2):]


def evaluate_daily(symbol, days):
    """Score yesterday's completed daily candle. Returns (side, score, reasons, info) or None."""
    if len(days) < DAILY_LOOKBACK + 2:
        return None
    base = days[-(DAILY_LOOKBACK + 1):-1]   # 20 completed days before yesterday
    y = days[-2]                            # yesterday = last COMPLETED day
    closes = [c["close"] for c in base]
    sma = sum(closes) / len(closes)
    avg_vol = max(sum(c["turnover"] for c in base) / len(base), 1.0)
    hi20 = max(c["high"] for c in base)
    lo20 = min(c["low"] for c in base)
    vol_mult = y["turnover"] / avg_vol
    move = (y["close"] - y["open"]) / y["open"] * 100 if y["open"] else 0.0

    for side in ("LONG", "SHORT"):
        score, reasons = 0, []
        if vol_mult >= DAILY_VOL_MULT:
            score += 1; reasons.append(f"Volume {vol_mult:.1f}x the {DAILY_LOOKBACK}-day average")
        if side == "LONG":
            if y["close"] > hi20:
                score += 1; reasons.append(f"Closed above the {DAILY_LOOKBACK}-day high")
            if y["close"] > sma:
                score += 1; reasons.append(f"Trading above the {DAILY_LOOKBACK}-day average (uptrend)")
            if move > 0 and days[-3]["close"] > days[-3]["open"]:
                score += 1; reasons.append("Back-to-back green daily closes")
        else:
            if y["close"] < lo20:
                score += 1; reasons.append(f"Closed below the {DAILY_LOOKBACK}-day low")
            if y["close"] < sma:
                score += 1; reasons.append(f"Trading below the {DAILY_LOOKBACK}-day average (downtrend)")
            if move < 0 and days[-3]["close"] < days[-3]["open"]:
                score += 1; reasons.append("Back-to-back red daily closes")
        if score >= DAILY_MIN_SCORE:
            info = {
                "price": y["close"], "pct_move": move,
                "turnover": y["turnover"], "mult": vol_mult,
                "candle_low": y["low"], "candle_high": y["high"],
            }
            return side, score, reasons, info
    return None


def run_daily_setup(tickers):
    """Scan liquid pairs on the daily chart; post the single best setup."""
    # restart guard: if today's DAILY is already in the feed, don't re-fire
    headers = _feed_headers()
    if headers:
        try:
            data, _ = _feed_read(headers)
            today_iso = time.strftime("%Y-%m-%d", time.gmtime())
            for a in data.get("alerts", []):
                if str(a.get("type", "")).startswith("DAILY") and str(a.get("t", "")).startswith(today_iso):
                    print("[info] Daily setup already published today — skipping.")
                    return
        except Exception:
            pass
    candidates = [s for s, t in tickers.items() if t["turnover24"] >= DAILY_MIN_24H_TURNOVER]
    best = None
    for sym in candidates:
        try:
            res = evaluate_daily(sym, fetch_daily_klines(sym))
        except Exception:
            continue
        if res:
            side, score, reasons, info = res
            key = (score, info["mult"])
            if best is None or key > best[0]:
                best = (key, sym, side, score, reasons, info)
        time.sleep(0.12)  # gentle on the API
    today = time.strftime("%d %b %Y")
    if best:
        _, sym, side, score, reasons, info = best
        emoji = "🟢" if side == "LONG" else "🔴"
        reason_lines = "\n".join(f"  ✔ {r}" for r in reasons)
        msg = (
            f"☀️ DAILY SETUP — {today}\n"
            f"{emoji} {side}: {sym} (score {score}/4, daily chart)\n"
            f"{reason_lines}\n"
            f"Yesterday: {info['pct_move']:+.2f}% on ${info['turnover']:,.0f} "
            f"({info['mult']:.1f}x avg volume)\n"
            f"{position_plan(side, info)}"
            f"https://futures.mexc.com/exchange/{sym}\n"
            f"{referral_line()}"
            f"⚠️ Daily-timeframe idea, not certainty — demo it first, size small."
        )
        print("\n" + msg + "\n")
        send_telegram(msg)
        d_stop = info["candle_low"] * (1 - STOP_BUFFER_PCT / 100) if side == "LONG" \
            else info["candle_high"] * (1 + STOP_BUFFER_PCT / 100)
        push_alert(
            pair=sym.replace("_", "/"),
            type=f"DAILY {side} SETUP",
            msg=(f"Daily chart · {info['pct_move']:+.2f}% yesterday · "
                 f"vol {info['mult']:.1f}x 20d avg · score {score}/4"),
            entry=info["price"], stop=d_stop, side=side,
        )
        post_to_x(
            f"☀️ DAILY SETUP: ${sym.replace('_USDT','')}\n"
            f"{emoji} {side} · daily chart · score {score}/4\n"
            f"{info['pct_move']:+.2f}% yesterday on {info['mult']:.1f}x average volume\n\n"
            f"Full setup + live feed (free): t.me/jwsalerts\n"
            f"Not financial advice. #crypto #trading"
        )
    elif DAILY_POST_WHEN_EMPTY:
        msg = (f"☀️ DAILY SETUP — {today}\n"
               f"No A-grade daily chart today. No setup is a position too — "
               f"capital preserved is capital compounding. Back tomorrow.")
        print("\n" + msg + "\n")
        send_telegram(msg)


def fetch_hourly_klines(symbol, need=27):
    data = get_json(
        FUTURES_BASE + f"/api/v1/contract/kline/{symbol}",
        params={"interval": "Min60"},
    )
    d = data.get("data", {})
    n = len(d.get("time", []))
    if n < need:
        return []
    out = []
    for i in range(n - need, n):
        out.append({
            "high": float(d["high"][i]), "low": float(d["low"][i]),
            "close": float(d["close"][i]), "turnover": float(d["amount"][i]),
        })
    return out


def _vwap_sigma(candles):
    """Turnover-weighted VWAP and σ over a candle window."""
    wsum = sum(c["turnover"] for c in candles)
    if wsum <= 0:
        return None, None
    tps = [((c["high"] + c["low"] + c["close"]) / 3, c["turnover"]) for c in candles]
    vwap = sum(tp * w for tp, w in tps) / wsum
    var = sum(w * (tp - vwap) ** 2 for tp, w in tps) / wsum
    return vwap, var ** 0.5


def run_vwap_scan(tickers):
    """Hourly: flag closes that reclaim (long) or lose (short) the rolling 24h VWAP."""
    candidates = [s for s, t in tickers.items()
                  if VWAP_MIN_24H_TURNOVER <= t["turnover24"] <= MAX_24H_TURNOVER]
    print(f"[{time.strftime('%H:%M:%S')}] VWAP scan over {len(candidates)} pairs…")
    fired = 0
    for sym in candidates:
        try:
            k = fetch_hourly_klines(sym)
            if len(k) < VWAP_WINDOW + 3:
                continue
            last = k[-2]                      # last CLOSED hourly candle
            prev = k[-3]
            win_now = k[-(VWAP_WINDOW + 1):-1]
            win_prev = k[-(VWAP_WINDOW + 2):-2]
            vwap_now, sigma = _vwap_sigma(win_now)
            vwap_prev, _ = _vwap_sigma(win_prev)
            if not vwap_now or not vwap_prev or not sigma:
                continue
            avg_vol = sum(c["turnover"] for c in win_now) / len(win_now)
            if last["turnover"] < VWAP_VOL_MULT * max(avg_vol, 1.0):
                continue
            dist_sigma = (last["close"] - vwap_now) / sigma
            side = None
            if prev["close"] < vwap_prev and last["close"] > vwap_now and abs(dist_sigma) <= VWAP_MAX_SIGMA:
                side, verb = "LONG", "RECLAIMED"
            elif prev["close"] > vwap_prev and last["close"] < vwap_now and abs(dist_sigma) <= VWAP_MAX_SIGMA:
                side, verb = "SHORT", "LOST"
            if not side:
                continue
            key = (sym, "VWAP-" + side)
            if time.time() - last_alert.get(key, 0) <= VWAP_COOLDOWN_SEC:
                continue
            last_alert[key] = time.time()
            fired += 1
            vol_mult = last["turnover"] / max(avg_vol, 1.0)
            info = {
                "price": last["close"], "pct_move": (last["close"] - prev["close"]) / prev["close"] * 100,
                "turnover": last["turnover"], "mult": vol_mult,
                "candle_low": last["low"], "candle_high": last["high"],
            }
            emoji = "🟢" if side == "LONG" else "🔴"
            msg = (
                f"{emoji} VWAP {verb} — {side}: {sym} (1h)\n"
                f"Closed {'above' if side == 'LONG' else 'below'} the rolling 24h VWAP "
                f"({dist_sigma:+.2f}σ) on {vol_mult:.1f}x volume\n"
                f"Price: {last['close']:.8g} | VWAP: {vwap_now:.8g}\n"
                f"{position_plan(side, info)}"
                f"https://futures.mexc.com/exchange/{sym}\n"
                f"{referral_line()}"
                f"⚠️ VWAP signals chop in ranging markets — demo first, size small."
            )
            print("\n" + msg + "\n")
            send_telegram(msg)
            if side == "LONG":
                v_stop = last["low"] * (1 - STOP_BUFFER_PCT / 100)
            else:
                v_stop = last["high"] * (1 + STOP_BUFFER_PCT / 100)
            push_alert(
                pair=sym.replace("_", "/"),
                type=f"VWAP {side}",
                msg=(f"{verb.lower()} 24h VWAP on 1h close · {dist_sigma:+.2f}σ · "
                     f"vol {vol_mult:.1f}x avg"),
                entry=last["close"], stop=v_stop, side=side,
            )
        except Exception:
            continue
        time.sleep(0.12)
    print(f"[{time.strftime('%H:%M:%S')}] VWAP scan done — {fired} signal(s).")


def run_runner_scan(tickers):
    """Flag pairs in a sustained 24h power move, near their extreme, with real turnover."""
    for sym, t in tickers.items():
        p, rise = t["price"], t.get("rise24", 0.0)
        if p <= 0 or not (RUNNER_MIN_TURNOVER <= t["turnover24"] <= RUNNER_MAX_TURNOVER):
            continue
        side = None
        if rise >= RUNNER_MIN_RISE and t.get("hi24", 0) > 0:
            if (t["hi24"] - p) / t["hi24"] * 100 <= RUNNER_NEAR_EXTREME_PCT:
                side = "LONG"
        elif rise <= RUNNER_MIN_FALL and t.get("lo24", 0) > 0:
            if (p - t["lo24"]) / p * 100 <= RUNNER_NEAR_EXTREME_PCT:
                side = "SHORT"
        if not side:
            continue
        key = (sym, "RUNNER-" + side)
        if time.time() - last_alert.get(key, 0) <= RUNNER_COOLDOWN_SEC:
            continue
        last_alert[key] = time.time()
        emoji = "🚀" if side == "LONG" else "🩸"
        msg = (
            f"{emoji} RUNNER {side}: {sym} — {rise:+.1f}% in 24h and still pressing "
            f"{'highs' if side == 'LONG' else 'lows'}\n"
            f"Price: {p:.8g} | 24h turnover: ${t['turnover24']:,.0f}\n"
            f"Low-float power move — these run further AND reverse harder than anything else.\n"
            f"https://futures.mexc.com/exchange/{sym}\n"
            f"{referral_line()}"
            f"⚠️ Parabolic. Late entries eat brutal drawdowns — if you touch it, size tiny, demo first."
        )
        print("\n" + msg + "\n")
        send_telegram(msg)
        push_alert(
            pair=sym.replace("_", "/"),
            type=f"RUNNER {side}",
            msg=f"{rise:+.1f}% in 24h · pressing 24h {'high' if side == 'LONG' else 'low'} · ${t['turnover24']/1e6:.1f}M turnover",
            entry=p, stop=None, side=side,
        )
        if abs(rise) >= RUNNER_X_MIN_RISE:
            post_to_x(
                f"{emoji} RUNNER: ${sym.replace('_USDT','')}\n"
                f"{rise:+.1f}% in 24h and still pressing {'highs' if side == 'LONG' else 'lows'}\n\n"
                f"Live feed + alerts (free): t.me/jwsalerts\n"
                f"Not financial advice. #crypto #trading"
            )


alert_counter = 0


def referral_line():
    """Returns a rotating referral hook every Nth alert, else empty."""
    global alert_counter
    alert_counter += 1
    if alert_counter % REFERRAL_EVERY_N_ALERTS != 0:
        return ""
    hook = REFERRAL_HOOKS[(alert_counter // REFERRAL_EVERY_N_ALERTS - 1) % len(REFERRAL_HOOKS)]
    return hook.format(link=REFERRAL_LINK) + "\n"


def send_setup_alert(symbol, side, score, reasons, info):
    emoji = "🟢" if side == "LONG" else "🔴"
    strength = "STRONG " if score >= 3 else ""
    reason_lines = "\n".join(f"  ✔ {r}" for r in reasons)
    msg = (
        f"{emoji} {strength}{side} SETUP: {symbol} (score {score}/4)\n"
        f"{reason_lines}\n"
        f"Price: {info['price']:.8g} ({info['pct_move']:+.2f}% this candle)\n"
        f"5m turnover: ${info['turnover']:,.0f} ({info['mult']:.1f}x baseline)\n"
        f"{position_plan(side, info)}"
        f"https://futures.mexc.com/exchange/{symbol}\n"
        f"{referral_line()}"
        f"⚠️ Setup, not certainty — size small, cut fast."
    )
    print("\n" + msg + "\n")
    send_telegram(msg)
    # website feed: data-only summary (no position plan, no referral)
    if side == "LONG":
        feed_stop = info["candle_low"] * (1 - STOP_BUFFER_PCT / 100)
    else:
        feed_stop = info["candle_high"] * (1 + STOP_BUFFER_PCT / 100)
    push_alert(
        pair=symbol.replace("_", "/"),
        type=f"{strength}{side} SETUP",
        msg=(f"{info['pct_move']:+.2f}% this candle · "
             f"vol {info['mult']:.1f}x baseline · score {score}/4"),
        entry=info["price"], stop=feed_stop, side=side,
    )
    # X: STRONG setups only, capped daily — data + funnel, no financial advice
    if score >= X_MIN_SCORE:
        post_to_x(
            f"{emoji} {strength}{side} SETUP: ${symbol.replace('_USDT','').replace('_','/')}\n"
            f"{info['pct_move']:+.2f}% · vol {info['mult']:.1f}x baseline · score {score}/4\n\n"
            f"Full alert + live feed (free): t.me/jwsalerts\n"
            f"Not financial advice. #crypto #trading"
        )


def main():
    print("MEXC Futures Long/Short Setup Bot")
    print(f"Universe: USDT perps, 24h turnover ${MIN_24H_TURNOVER:,} - ${MAX_24H_TURNOVER:,}")
    print(f"Trigger: volume spike >= {SPIKE_MULTIPLIER}x 2h median (>= ${MIN_SPIKE_TURNOVER:,}) "
          f"+ confluence score >= {ALERT_SCORE_MIN}/4\n")

    send_telegram("✅ JWS Trade Bot online — scanning MEXC futures for long/short setups.")

    universe = []
    universe_built_at = 0
    last_daily_date = ""
    last_vwap_hour = ""

    while True:
        try:
            tickers = fetch_all_tickers()
        except requests.RequestException as e:
            print(f"[warn] Ticker fetch failed: {e}")
            time.sleep(30)
            continue

        now = time.time()

        # outcome tracker: re-mark LIVE alerts against current prices
        update_tracker(tickers)

        # VWAP tier: once per hour, just after the hourly close
        this_hour = time.strftime("%Y-%m-%d-%H", time.localtime())
        if time.localtime().tm_min >= VWAP_SCAN_MINUTE and last_vwap_hour != this_hour:
            last_vwap_hour = this_hour
            try:
                run_vwap_scan(tickers)
            except Exception as e:
                print(f"[warn] VWAP scan failed: {e}")

        # runner tier: power moves already underway (ticker data only)
        try:
            run_runner_scan(tickers)
        except Exception as e:
            print(f"[warn] Runner scan failed: {e}")

        # Daily Setup of the Day — once, at/after the configured hour
        lt = time.localtime()
        today_str = time.strftime("%Y-%m-%d", lt)
        if lt.tm_hour >= DAILY_SETUP_HOUR and last_daily_date != today_str:
            last_daily_date = today_str
            print(f"[{time.strftime('%H:%M:%S')}] Running Daily Setup scan…")
            try:
                run_daily_setup(tickers)
            except Exception as e:
                print(f"[warn] Daily setup scan failed: {e}")

        if now - universe_built_at > UNIVERSE_REFRESH_SEC or not universe:
            universe = [s for s, t in tickers.items()
                        if MIN_24H_TURNOVER <= t["turnover24"] <= MAX_24H_TURNOVER]
            universe_built_at = now
            print(f"[{time.strftime('%H:%M:%S')}] Watchlist refreshed: "
                  f"{len(universe)} futures contracts")

        # Track open interest for every watched symbol (from the single ticker call)
        for s in universe:
            if s in tickers:
                update_oi(s, tickers[s]["oi"])

        scanned = 0
        for symbol in universe:
            if symbol not in tickers:
                continue
            try:
                candles = fetch_klines(symbol)
                scanned += 1
            except requests.RequestException:
                continue
            result = evaluate(symbol, candles, tickers[symbol])
            if result:
                side, score, reasons, info = result
                key = (symbol, side)
                if time.time() - last_alert.get(key, 0) > ALERT_COOLDOWN_SEC:
                    send_setup_alert(symbol, side, score, reasons, info)
                    last_alert[key] = time.time()
            time.sleep(REQUEST_PAUSE_SEC)

        print(f"[{time.strftime('%H:%M:%S')}] Scan complete ({scanned} contracts). "
              f"Sleeping {SCAN_INTERVAL_SEC}s...")
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
