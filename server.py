"""Stdlib HTTP server for Catalyst AppSail (no third-party packages). Start: python3 -u server.py"""
import os, json, sqlite3, csv, io, datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import engine

HERE = os.path.dirname(os.path.abspath(__file__))
try: DB = os.path.join(HERE, "data", "log.db"); open(os.path.join(HERE, "data", ".w"), "w").close()
except OSError: DB = "/tmp/log.db"
with sqlite3.connect(DB) as c: c.execute("create table if not exists runs(ts text, source text, rows int, peak real, overloads int)")
SAMPLE = list(csv.DictReader(open(os.path.join(HERE, "sample_test_input.csv"))))

def run(rows, soc0=80.0, heat=0.0):
    rows = engine.clean(rows)[:24]
    if heat:
        for r in rows: r["ambient_temp_c"] += heat
    preds, src = engine.forecast(rows); f = engine.dispatch(rows, preds, soc0)
    s = {"source": src, "hours": len(f), "peak_kw": max(r["predicted_kw"] for r in f), "avg_kw": round(sum(r["predicted_kw"] for r in f) / len(f), 1),
         "overload_hours": sum(r["critical_overload"] for r in f), "renewable_hours": sum(r["mode"] == "Renewable Storage" for r in f), "min_soc": min(r["soc_pct"] for r in f)}
    try:
        with sqlite3.connect(DB) as c: c.execute("insert into runs values(?,?,?,?,?)", (dt.datetime.now().isoformat(timespec="seconds"), src, len(f), s["peak_kw"], s["overload_hours"]))
    except Exception as e: print("log failed:", e)
    return {"summary": s, "forecast": f}

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _body(self): return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
    def do_GET(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/": return self._send(200, open(os.path.join(HERE, "static", "index.html"), "rb").read(), "text/html; charset=utf-8")
            if u.path == "/api/health": return self._send(200, {"status": "ok", "quickml": bool(os.environ.get("QUICKML_ENDPOINT"))})
            if u.path == "/api/sample":
                s = int(q.get("start", 0)) * 24
                return self._send(200, run(SAMPLE[s:s + 24], float(q.get("soc0", 80)), float(q.get("heat", 0))))
            if u.path == "/api/history":
                with sqlite3.connect(DB) as c: r = c.execute("select ts,source,rows,peak,overloads from runs order by rowid desc limit 10").fetchall()
                return self._send(200, [dict(ts=a, source=b, rows=n, peak_kw=p, overload_hours=o) for a, b, n, p, o in r])
            self._send(404, {"error": "not found"})
        except ValueError as e: self._send(400, {"error": str(e)})
        except Exception as e: self._send(500, {"error": str(e)})
    def do_POST(self):
        try:
            j = self._body(); soc0, heat = float(j.get("soc0", 80)), float(j.get("heat", 0))
            if self.path == "/api/forecast": return self._send(200, run(j.get("rows", []), soc0, heat))
            if self.path == "/api/upload": return self._send(200, run(list(csv.DictReader(io.StringIO(j.get("csv", "")))), soc0, heat))
            self._send(404, {"error": "not found"})
        except ValueError as e: self._send(400, {"error": str(e)})
        except Exception as e: self._send(500, {"error": str(e)})
    def log_message(self, *a): pass

if __name__ == "__main__":
    port = int(os.environ.get("X_ZOHO_CATALYST_LISTEN_PORT", os.environ.get("PORT", 9000)))
    print("listening on", port, flush=True); ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
