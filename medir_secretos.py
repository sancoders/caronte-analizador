import re, json, base64, sys, time
from concurrent.futures import ThreadPoolExecutor
import requests
from urllib.parse import urljoin

S = requests.Session()
S.headers.update({"User-Agent":"Mozilla/5.0 (research; passive-bundle-scan)"})
TIMEOUT = 20
MAXBYTES = 6_000_000

# patrones de secretos que NUNCA deberian estar en el cliente
PATS = {
 "openai_key":       re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}"),
 "anthropic_key":    re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{20,}"),
 "stripe_secret":    re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{20,}"),
 "aws_access_key":   re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
 "resend_key":       re.compile(r"\bre_[A-Za-z0-9]{8,}_[A-Za-z0-9]{10,}"),
 "sendgrid_key":     re.compile(r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"),
 "mapbox_secret":    re.compile(r"\bsk\.eyJ[A-Za-z0-9_\-]{20,}"),
 "twilio_sid_auth":  re.compile(r"\bAC[0-9a-f]{32}\b"),
 "github_token":     re.compile(r"\bgh[pous]_[A-Za-z0-9]{30,}"),
 "google_api_key":   re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
 "private_key_pem":  re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
 "gemini_mention":   re.compile(r"GEMINI_API_KEY|OPENAI_API_KEY|SERVICE_ROLE_KEY", re.I),
}
JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}")
SRC = re.compile(r'(?:src|href)\s*=\s*["\']([^"\']+\.js[^"\']*)["\']', re.I)

def b64d(s):
    s += "=" * (-len(s) % 4)
    try: return base64.urlsafe_b64decode(s).decode("utf-8","ignore")
    except Exception: return ""

def jwt_roles(text):
    roles = set()
    for m in JWT.finditer(text):
        payload = b64d(m.group(0).split(".")[1])
        if '"role"' in payload or "'role'" in payload:
            r = re.search(r'"role"\s*:\s*"([^"]+)"', payload)
            if r: roles.add(r.group(1))
        if '"iss"' in payload and "supabase" in payload:
            r = re.search(r'"role"\s*:\s*"([^"]+)"', payload)
            if r: roles.add(r.group(1))
    return roles

def get(url):
    r = S.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True)
    buf = b""
    for chunk in r.iter_content(65536):
        buf += chunk
        if len(buf) > MAXBYTES: break
    r.close()
    return r.status_code, r.headers.get("content-type",""), buf.decode("utf-8","ignore"), r.url

def scan_host(host):
    out = {"host": host, "ok": False, "findings": {}, "roles": [], "js_bytes": 0, "err": None,
           "uses_supabase": False, "n_js": 0}
    try:
        code, ctype, html, final = get(f"https://{host}/")
        out["status"] = code
        out["final_url"] = final
        if code != 200 or "html" not in ctype.lower():
            out["err"] = f"no-html:{code}"
            return out
        if len(html) < 200:
            out["err"] = "empty"
            return out
        out["ok"] = True
        js = []
        for m in SRC.finditer(html):
            u = urljoin(final, m.group(1))
            if u.startswith("http") and u not in js: js.append(u)
        js = js[:8]
        out["n_js"] = len(js)
        blob = html
        for u in js:
            try:
                c2, ct2, txt, _ = get(u)
                if c2 == 200:
                    blob += "\n" + txt
                    out["js_bytes"] += len(txt)
            except Exception:
                pass
        if "supabase" in blob.lower(): out["uses_supabase"] = True
        for name, pat in PATS.items():
            hits = pat.findall(blob)
            if hits:
                out["findings"][name] = len(set(hits))
        out["roles"] = sorted(jwt_roles(blob))
    except Exception as e:
        out["err"] = type(e).__name__
    return out

hosts = [h.strip() for h in open(sys.argv[1]) if h.strip()]
res = []
t0 = time.time()
with ThreadPoolExecutor(max_workers=14) as ex:
    for i, r in enumerate(ex.map(scan_host, hosts), 1):
        res.append(r)
        if i % 25 == 0:
            print(f"{i}/{len(hosts)}  {time.time()-t0:.0f}s", flush=True)
json.dump(res, open(sys.argv[2], "w"), indent=1)
print("listo", len(res), f"{time.time()-t0:.0f}s")
