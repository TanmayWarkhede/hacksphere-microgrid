"""Pure-stdlib engine: cleaning, features, forecast (QuickML or local ridge weights), transition controller, SoC dispatch."""
import csv, io, os, json, math, urllib.request, datetime as dt
RAW = ["ambient_temp_c", "humidity_pct", "solar_irradiance_wm2"]
W = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "local_model.json")))
OVERLOAD_KW, SWITCH_KW = 200.0, 160.0

def _ts(v):
    v = str(v).strip()
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try: return dt.datetime.strptime(v, f)
        except ValueError: pass
    try: return dt.datetime.fromisoformat(v)
    except ValueError: raise ValueError(f"Unparseable timestamp: {v!r}")

def _num(v):
    try:
        x = float(v); return x if math.isfinite(x) else None
    except (TypeError, ValueError): return None

def clean(rows):
    if not rows: raise ValueError("No rows supplied")
    miss = [c for c in ["timestamp"] + RAW if c not in rows[0]]
    if miss: raise ValueError(f"Missing required columns: {miss}")
    out = sorted(({"timestamp": _ts(r["timestamp"]), **{c: _num(r.get(c)) for c in RAW}} for r in rows), key=lambda r: r["timestamp"])
    for r in out:
        if r["humidity_pct"] is not None and r["humidity_pct"] > 100: r["humidity_pct"] = None
        if r["solar_irradiance_wm2"] is not None and r["solar_irradiance_wm2"] < 0: r["solar_irradiance_wm2"] = None
    for c in RAW:
        known = [i for i, r in enumerate(out) if r[c] is not None]
        if not known:
            for r in out: r[c] = 0.0
            continue
        for i, r in enumerate(out):
            if r[c] is None:
                lo = max((k for k in known if k < i), default=None); hi = min((k for k in known if k > i), default=None)
                if lo is None: r[c] = out[hi][c]
                elif hi is None: r[c] = out[lo][c]
                else: r[c] = out[lo][c] + (out[hi][c] - out[lo][c]) * (i - lo) / (hi - lo)
        for r in out: r[c] = min(100.0, max(0.0, r[c])) if c == "humidity_pct" else (min(1500.0, max(0.0, r[c])) if c == "solar_irradiance_wm2" else r[c])
    return out

def features(r):
    h = r["timestamp"].hour; f = {f"h{i}": 1.0 if h == i else 0.0 for i in range(24)}
    f["wk"] = 1.0 if r["timestamp"].weekday() >= 5 else 0.0
    cool = max(r["ambient_temp_c"] - 24, 0.0)
    for s, m in {"night": h < 6 or h == 23, "day": 6 <= h < 18, "eve": 18 <= h < 23}.items():
        for c in RAW: f[f"{c}_{s}"] = r[c] if m else 0.0
        f[f"cool_{s}"] = cool if m else 0.0
    return f

def _local(r):
    f = features(r)
    return W["intercept"] + sum((f[c] - m) / s * w for c, m, s, w in zip(W["cols"], W["mean"], W["scale"], W["coef"]))

def _quickml(rows):
    """Env: QUICKML_ENDPOINT, QUICKML_ENDPOINT_KEY, QUICKML_ORG_ID, QUICKML_ENV, QUICKML_TOKEN or refresh-token vars."""
    e = os.environ
    tok = e.get("QUICKML_TOKEN", "")
    if e.get("QUICKML_REFRESH_TOKEN"):
        body = "&".join(f"{k}={v}" for k, v in {"grant_type": "refresh_token", "client_id": e["QUICKML_CLIENT_ID"], "client_secret": e["QUICKML_CLIENT_SECRET"], "refresh_token": e["QUICKML_REFRESH_TOKEN"]}.items()).encode()
        tok = json.load(urllib.request.urlopen(urllib.request.Request(e.get("QUICKML_ACCOUNTS_URL", "https://accounts.zoho.in") + "/oauth/v2/token", body), timeout=30))["access_token"]
    h = {"Content-Type": "application/json", "Authorization": f"Zoho-oauthtoken {tok}", "Environment": e.get("QUICKML_ENV", "Development")}
    if e.get("QUICKML_ENDPOINT_KEY"): h["X-QUICKML-ENDPOINT-KEY"] = e["QUICKML_ENDPOINT_KEY"]
    if e.get("QUICKML_ORG_ID"): h["CATALYST-ORG"] = e["QUICKML_ORG_ID"]
    data = json.dumps({"data": [{k: round(v, 4) for k, v in features(r).items()} for r in rows]}).encode()
    j = json.load(urllib.request.urlopen(urllib.request.Request(e["QUICKML_ENDPOINT"], data, h), timeout=60))
    out = j.get("result", j.get("predictions", j.get("data", j))) if isinstance(j, dict) else j
    vals = [float(v["target_demand_kw"] if isinstance(v, dict) and "target_demand_kw" in v else (list(v.values())[0] if isinstance(v, dict) else v)) for v in out]
    if len(vals) != len(rows): raise ValueError("QuickML returned wrong number of predictions")
    return vals

def forecast(rows):
    if os.environ.get("QUICKML_ENDPOINT"):
        try: return _quickml(rows), "quickml"
        except Exception as ex:
            if os.environ.get("FORCE_QUICKML") == "1": raise
            print("QuickML failed, using local fallback:", ex)
    return [_local(r) for r in rows], "local-fallback"

def transition(ts, kw):
    mode = "Renewable Storage" if (kw > SWITCH_KW or 18 <= ts.hour <= 22) else "Main Grid"
    return mode, kw >= OVERLOAD_KW

def dispatch(rows, preds, soc0=80.0, cap=6000.0, charge=300.0, floor=10.0):
    soc, out = soc0, []
    for r, p in zip(rows, preds):
        mode, over = transition(r["timestamp"], p)
        soc += -p / cap * 100 if mode == "Renewable Storage" else charge / cap * 100
        soc = min(100.0, max(0.0, soc))
        out.append({"timestamp": r["timestamp"].strftime("%Y-%m-%d %H:%M:%S"), "hour": r["timestamp"].hour, "predicted_kw": round(p, 2),
                    "mode": mode, "critical_overload": over, "soc_pct": round(soc, 1), "low_battery": soc < floor})
    return out
