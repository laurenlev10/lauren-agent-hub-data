# =================================================================
# Tradovate Auto-Trader — Step 4 ב-Zap SMA60 (v5 — 2026-05-27)
# Paste this whole file into the "Code by Zapier" Python step.
# Changes vs v4:
#   • Every SKIP/ERROR is logged back to journal-data.json via reject() helper
#     so the dashboard's "רשומות גולמיות" tab shows EVERY rejected alert + reason.
#   • Set GH_TOKEN_FOR_RAW_LOG = "<classic PAT scope=repo>" to enable the logging.
#     Leave empty to keep the bot trading but skip the rejection-logging.
# =================================================================
import json, time, base64
from datetime import datetime, timezone
import requests

TRADOVATE = {
    "url": "https://demo.tradovateapi.com/v1",
    "name": "Laurenlev318",
    "password": "039845425Lauren!",
    "appId": "SMA60AutoTraderDemo",
    "appVersion": "1.0",
    "cid": 13601,
    "sec": "ab91f7d0-a36d-43b8-88fc-192b93134e05",
}
KILL_SWITCH_URL = "https://dashboard.themakeupblowout.com/trading/autotrade_enabled.json"
JOURNAL_URL = "https://dashboard.themakeupblowout.com/trading/journal-data.json"
ST_TOKEN = "26daba15ca118647f932f4b9bca5a7e9"
LAUREN_PHONE = "+14243547625"

# 🛑 2026-05-28 — Cloudflare Access Service Token (required since dashboard.themakeupblowout.com
# is now gated by Access). Without these headers, the bot can't read autotrade_enabled.json or
# the journal. Token "Bot v2" — non-expiring. Maps to Access policy "Bot v2 Service Token"
# (action: Service Auth) on app "Lauren Agent Hub".
CF_ACCESS_CLIENT_ID = "e2687da8c05935922276da65140c7802.access"
CF_ACCESS_CLIENT_SECRET = "46536b0b62f596506979048604ddd63fc43338219d2d0a428bfbfdc17dcc3ec8"
CF_ACCESS_HEADERS = {
    "CF-Access-Client-Id": CF_ACCESS_CLIENT_ID,
    "CF-Access-Client-Secret": CF_ACCESS_CLIENT_SECRET,
}
# 🛑 2026-05-27 — fallbacks only. Actual values come from autotrade_enabled.json
# (default_tp_ticks / default_sl_ticks). Editable from the dashboard 📊 TP/SL modal.
DEFAULT_TP_TICKS = 250
DEFAULT_SL_TICKS = 550

GH_TOKEN_FOR_RAW_LOG = ""   # ← paste classic PAT (scope=repo) to enable rejection-logging
GH_REPO = "laurenlev10/lauren-agent-hub-data"
GH_RAW_LOG_FILE = "docs/trading/journal-data.json"

def sms(body):
    try:
        requests.post(
            "https://api-app2.simpletexting.com/v2/api/messages",
            headers={"Authorization": "Bearer " + ST_TOKEN, "Content-Type": "application/json"},
            json={"contactPhone": LAUREN_PHONE, "mode": "AUTO", "text": body},
            timeout=10,
        )
    except Exception:
        pass

def log_rejection_to_journal(signal_type, ticker_, status, reason, details=None):
    if not GH_TOKEN_FOR_RAW_LOG: return
    try:
        API = "https://api.github.com/repos/" + GH_REPO + "/contents/" + GH_RAW_LOG_FILE
        H = {"Authorization": "token " + GH_TOKEN_FOR_RAW_LOG,
             "Accept": "application/vnd.github+json"}
        g = requests.get(API + "?ref=main", headers=H, timeout=8)
        if g.status_code != 200: return
        j = g.json(); sha = j["sha"]
        data = json.loads(base64.b64decode(j["content"]).decode("utf-8"))
        if "trades" not in data or not isinstance(data["trades"], list):
            data = {"_doc": "TradingView journal", "_schema_version": 1, "trades": []}
        row = {
            "ticker": ticker_ or "",
            "type": signal_type or "",
            "action": "rejected",
            "price": None,
            "_received_at": datetime.now(timezone.utc).isoformat(),
            "_status": status,
            "_skip_reason": reason or "",
        }
        if details: row["_details"] = details
        data["trades"].append(row)
        data["_updated_at"] = row["_received_at"]
        body_ = {
            "message": "journal: " + status + " " + (signal_type or "?") + " — " + (reason or "?"),
            "content": base64.b64encode(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")).decode(),
            "sha": sha, "branch": "main",
        }
        requests.put(API, headers=H, json=body_, timeout=10)
    except Exception:
        pass

def reject(base_, status_, reason_, **extra_):
    log_rejection_to_journal(base_.get("signal_type"), base_.get("ticker"),
                              status_, reason_, extra_ if extra_ else None)
    return {**base_, "status": status_, "reason": reason_, **extra_}

def f2(v):
    if v in (None, "", "null"): return None
    try: return float(v)
    except: return None
def i2(v):
    f = f2(v); return int(f) if f is not None else 0

ticker = (input_data.get("ticker") or "").strip()
type_ = (input_data.get("type") or "").strip().upper()
danger = i2(input_data.get("danger"))
received = datetime.now(timezone.utc).isoformat()
is_exit = type_.startswith("EXIT")
base = {"received_at": received, "ticker": ticker, "signal_type": type_, "danger": danger, "is_exit": is_exit}

try:
    cfg = requests.get(KILL_SWITCH_URL + "?_=" + str(int(time.time())),
                       headers=CF_ACCESS_HEADERS, timeout=10).json()
except Exception as e:
    return reject(base, "ERROR", "config_fetch_failed", error=str(e))

if not cfg.get("enabled", False) and not is_exit:
    return reject(base, "SKIP", "bot_disabled")
whitelist = cfg.get("symbol_whitelist", [])
if not any(ticker.startswith(w) for w in whitelist):
    return reject(base, "SKIP", "symbol_not_whitelisted", whitelist=whitelist)
if cfg.get("skip_on_danger", True) and danger == 1 and not is_exit:
    return reject(base, "SKIP", "danger_hour")
active = cfg.get("active_contract") or {}
if not ticker.startswith(active.get("ticker_root", "")):
    return reject(base, "SKIP", "no_active_contract_mapping")

contract_id = active.get("contract_id")
contract_name = active.get("contract_name")
account_id = cfg.get("account_id")
max_qty = int(cfg.get("max_qty", 1))
daily_cap = int(cfg.get("daily_cap", 999))

# 🛑 2026-05-27 — TP/SL ticks come from config (editable via dashboard 📊 TP/SL modal)
cfg_tp_ticks = int(cfg.get("default_tp_ticks") or DEFAULT_TP_TICKS)
cfg_sl_ticks = int(cfg.get("default_sl_ticks") or DEFAULT_SL_TICKS)

if not is_exit:
    try:
        journal = requests.get(JOURNAL_URL + "?_=" + str(int(time.time())),
                               headers=CF_ACCESS_HEADERS, timeout=10).json()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_count = sum(1 for t in journal.get("trades", []) if (t.get("_received_at","").startswith(today)))
        if today_count >= daily_cap:
            sms("⚠ BOT עצר: עברנו את daily_cap (" + str(today_count) + "/" + str(daily_cap) + "). " + type_ + " " + ticker + " לא בוצע.")
            return reject(base, "SKIP", "daily_cap_exceeded", today_count=today_count, daily_cap=daily_cap)
    except Exception:
        pass

try:
    auth_body = {k: TRADOVATE[k] for k in ("name","password","appId","appVersion","cid","sec")}
    auth_r = requests.post(TRADOVATE["url"] + "/auth/accesstokenrequest", json=auth_body, timeout=15)
    auth_data = auth_r.json()
    token = auth_data.get("accessToken")
    if not token:
        err = auth_data.get("errorText") or auth_r.text[:200]
        sms("🚨 Tradovate AUTH נכשל: " + str(err))
        return reject(base, "ERROR", "auth_failed", error=err)
except Exception as e:
    sms("🚨 Tradovate AUTH exception: " + str(e))
    return reject(base, "ERROR", "auth_exception", error=str(e))

HEAD = {"Authorization": "Bearer " + token}

try:
    pos_r = requests.get(TRADOVATE["url"] + "/position/list", headers=HEAD, timeout=15)
    positions = pos_r.json()
    current_net = 0
    for p in positions:
        if p.get("contractId") == contract_id and p.get("accountId") == account_id:
            current_net = int(p.get("netPos") or 0)
            break
except Exception as e:
    return reject(base, "ERROR", "position_fetch_failed", error=str(e))

# 🔼 SCALE IN — add a contract to the open position WITH its own bracket (TP/SL).
# Pyramiding: not capped by max_qty. EXIT of this contract is handled server-side
# by the OSO bracket below, so the indicator's "EXIT SCALE" alert is intentionally
# a no-op (see below).
if type_ in ("MOMENTUM IN", "SCALE IN"):
    if not cfg.get("enabled", False):
        return reject(base, "SKIP", "bot_disabled_scalein")
    add_action = "Buy" if (input_data.get("action", "").lower() == "buy") else "Sell"
    add_qty = max(1, i2(input_data.get("quantity")) or 1)
    px = f2(input_data.get("price"))
    if px is None or px <= 0:
        sms("🚨 SCALE IN ללא price")
        return reject(base, "ERROR", "scalein_missing_price")
    tickS = float(active.get("tick_size") or 0.25)
    si_tp_ticks = i2(input_data.get("tp_ticks")) or 175
    si_sl_ticks = i2(input_data.get("sl_ticks")) or 80
    opp = "Sell" if add_action == "Buy" else "Buy"
    if add_action == "Buy":
        si_sl = px - si_sl_ticks * tickS
        si_tp = px + si_tp_ticks * tickS
    else:
        si_sl = px + si_sl_ticks * tickS
        si_tp = px - si_tp_ticks * tickS
    si_sl = round(round(si_sl / tickS) * tickS, 4)
    si_tp = round(round(si_tp / tickS) * tickS, 4)
    oso_si = {
        "accountSpec": TRADOVATE["name"], "accountId": account_id, "action": add_action,
        "symbol": contract_name, "orderQty": add_qty, "orderType": "Market", "isAutomated": True,
        "bracket1": {"action": opp, "orderType": "Stop",  "stopPrice": si_sl, "timeInForce": "GTC"},
        "bracket2": {"action": opp, "orderType": "Limit", "price": si_tp,    "timeInForce": "GTC"},
    }
    try:
        r = requests.post(TRADOVATE["url"] + "/order/placeOSO", headers=HEAD, json=oso_si, timeout=20)
        dd = r.json(); oid = dd.get("orderId")
        if not oid:
            err = dd.get("failureText") or r.text[:200]
            sms("🚨 SCALE IN נכשל: " + str(err))
            return reject(base, "ERROR", "scalein_failed", error=err)
    except Exception as e:
        sms("🚨 SCALE IN exception: " + str(e))
        return reject(base, "ERROR", "scalein_exception", error=str(e))
    sms("🔼 SCALE IN " + add_action + " x" + str(add_qty) + " @market | TP " + str(si_tp) + " | SL " + str(si_sl))
    return {**base, "status": "OK", "reason": "scale_in_placed", "order_id": oid,
            "action": add_action, "qty": add_qty, "symbol": contract_name, "sl": si_sl, "tp": si_tp}

# EXIT MOMENTUM / EXIT SCALE — the momentum contract is closed by its OSO bracket on
# Tradovate. No bot action needed (must NOT fall into the generic EXIT flatten logic).
if type_.startswith("EXIT MOMENTUM") or type_.startswith("EXIT SCALE"):
    return reject(base, "SKIP", "momentum_exit_handled_by_bracket")

u = type_
if u in ("LONG", "SWAP LONG"):
    delta = max_qty - current_net
elif u in ("SHORT", "SWAP SHORT"):
    delta = -max_qty - current_net
elif u.startswith("EXIT LONG"):
    delta = -current_net if current_net > 0 else 0
elif u.startswith("EXIT SHORT"):
    delta = -current_net if current_net < 0 else 0
else:
    return reject(base, "SKIP", "unknown_signal_type")

if delta == 0:
    return reject(base, "SKIP", "no_position_change", current_pos=current_net)

def cancel_working_orders():
    cancelled = []
    try:
        r = requests.get(TRADOVATE["url"] + "/order/list", headers=HEAD, timeout=15)
        for o in (r.json() if r.ok else []):
            if (o.get("contractId") == contract_id
                and o.get("accountId") == account_id
                and o.get("ordStatus") in ("Working", "PendingNew", "Accepted")):
                try:
                    requests.post(TRADOVATE["url"] + "/order/cancelorder",
                                  headers=HEAD, json={"orderId": o.get("id")}, timeout=10)
                    cancelled.append(o.get("id"))
                except Exception:
                    pass
    except Exception:
        pass
    return cancelled

if is_exit:
    cancelled_working = cancel_working_orders()
    action = "Buy" if delta > 0 else "Sell"
    qty = abs(delta)
    flat_body = {
        "accountSpec": TRADOVATE["name"], "accountId": account_id, "action": action,
        "symbol": contract_name, "orderQty": qty, "orderType": "Market", "isAutomated": True,
    }
    try:
        r = requests.post(TRADOVATE["url"] + "/order/placeorder", headers=HEAD, json=flat_body, timeout=20)
        d = r.json(); order_id = d.get("orderId")
        if not order_id:
            err = d.get("failureText") or r.text[:200]
            sms("🚨 EXIT נכשל: " + str(err))
            return reject(base, "ERROR", "exit_failed", error=err)
    except Exception as e:
        sms("🚨 EXIT exception: " + str(e))
        return reject(base, "ERROR", "exit_exception", error=str(e))
    return {**base, "status": "OK", "reason": "exit_placed", "order_id": order_id,
            "action": action, "qty": qty, "symbol": contract_name,
            "cancelled_working": cancelled_working,
            "prev_position": current_net, "new_position_target": 0}

action = "Buy" if delta > 0 else "Sell"
opposite_action = "Sell" if delta > 0 else "Buy"
tick = float(active.get("tick_size") or 0.25)
sl_price = f2(input_data.get("sl"))
tp_price = f2(input_data.get("tp"))
if sl_price is None or tp_price is None:
    close_price = f2(input_data.get("price"))
    if close_price is None or close_price <= 0:
        sms("🚨 BOT דחה כניסה — חסר price (וגם חסר sl/tp) | " + type_ + " " + ticker)
        return reject(base, "SKIP", "missing_price_and_brackets")
    # payload sl/tp_ticks override → fallback to config (default_sl_ticks / default_tp_ticks)
    sl_ticks_in = i2(input_data.get("sl_ticks")) or cfg_sl_ticks
    tp_ticks_in = i2(input_data.get("tp_ticks")) or cfg_tp_ticks
    if action == "Buy":
        sl_price = close_price - sl_ticks_in * tick
        tp_price = close_price + tp_ticks_in * tick
    else:
        sl_price = close_price + sl_ticks_in * tick
        tp_price = close_price - tp_ticks_in * tick
sl_price = round(round(sl_price / tick) * tick, 4)
tp_price = round(round(tp_price / tick) * tick, 4)
if action == "Buy":
    if not (sl_price < tp_price):
        sms("🚨 BOT דחה LONG — SL " + str(sl_price) + " חייב להיות מתחת ל-TP " + str(tp_price))
        return reject(base, "SKIP", "invalid_brackets_long", sl=sl_price, tp=tp_price)
else:
    if not (sl_price > tp_price):
        sms("🚨 BOT דחה SHORT — SL " + str(sl_price) + " חייב להיות מעל ל-TP " + str(tp_price))
        return reject(base, "SKIP", "invalid_brackets_short", sl=sl_price, tp=tp_price)

flatten_order_id = None
cancelled_pre_swap = []
if current_net != 0 and ((action == "Buy" and current_net < 0) or (action == "Sell" and current_net > 0)):
    cancelled_pre_swap = cancel_working_orders()
    flat_qty = abs(current_net)
    flat_action = "Buy" if current_net < 0 else "Sell"
    flat_body = {
        "accountSpec": TRADOVATE["name"], "accountId": account_id, "action": flat_action,
        "symbol": contract_name, "orderQty": flat_qty, "orderType": "Market", "isAutomated": True,
    }
    try:
        fr = requests.post(TRADOVATE["url"] + "/order/placeorder", headers=HEAD, json=flat_body, timeout=20)
        fd = fr.json(); flatten_order_id = fd.get("orderId")
        if not flatten_order_id:
            err = fd.get("failureText") or fr.text[:200]
            sms("🚨 SWAP flatten נכשל: " + str(err))
            return reject(base, "ERROR", "swap_flatten_failed", error=err)
        time.sleep(1)
    except Exception as e:
        sms("🚨 SWAP flatten exception: " + str(e))
        return reject(base, "ERROR", "swap_flatten_exception", error=str(e))
    qty = max_qty
else:
    qty = abs(delta)

oso_body = {
    "accountSpec": TRADOVATE["name"], "accountId": account_id, "action": action,
    "symbol": contract_name, "orderQty": qty, "orderType": "Market", "isAutomated": True,
    "bracket1": {"action": opposite_action, "orderType": "Stop", "stopPrice": sl_price, "timeInForce": "GTC"},
    "bracket2": {"action": opposite_action, "orderType": "Limit", "price": tp_price, "timeInForce": "GTC"},
}
try:
    order_r = requests.post(TRADOVATE["url"] + "/order/placeOSO", headers=HEAD, json=oso_body, timeout=20)
    order_data = order_r.json(); order_id = order_data.get("orderId")
    if not order_id:
        err = order_data.get("failureText") or order_r.text[:200]
        sms("🚨 OSO נכשל: " + str(err) + " | " + type_ + " " + ticker + " " + action + " " + str(qty)
            + " | sl=" + str(sl_price) + " tp=" + str(tp_price))
        return reject(base, "ERROR", "oso_failed", error=err, sl=sl_price, tp=tp_price)
except Exception as e:
    sms("🚨 OSO exception: " + str(e))
    return reject(base, "ERROR", "oso_exception", error=str(e))

sms("✓ " + ("LONG" if action == "Buy" else "SHORT") + " " + contract_name
    + " x" + str(qty) + " @market | SL " + str(sl_price) + " | TP " + str(tp_price))

return {**base, "status": "OK", "reason": "oso_placed", "order_id": order_id,
        "action": action, "qty": qty, "symbol": contract_name,
        "sl": sl_price, "tp": tp_price,
        "flatten_order_id": flatten_order_id, "cancelled_pre_swap": cancelled_pre_swap,
        "prev_position": current_net, "new_position_target": (max_qty if action == "Buy" else -max_qty)}
