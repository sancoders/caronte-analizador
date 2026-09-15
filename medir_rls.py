import re, json, base64, time
from concurrent.futures import ThreadPoolExecutor
import requests
S=requests.Session(); S.headers.update({"User-Agent":"Mozilla/5.0 (security-research; read-schema-only)"})
T=18; MAX=6_000_000
SRC=re.compile(r'(?:src|href)\s*=\s*["\']([^"\']+\.js[^"\']*)["\']',re.I)
SUPAURL=re.compile(r'https://([a-z0-9]{20})\.supabase\.co')
ANON=re.compile(r'(eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,})')
from urllib.parse import urljoin
def b64d(s):
    s+='='*(-len(s)%4)
    try:return base64.urlsafe_b64decode(s).decode('utf-8','ignore')
    except:return''
def get(u):
    r=S.get(u,timeout=T,stream=True); buf=b''
    for c in r.iter_content(65536):
        buf+=c
        if len(buf)>MAX:break
    r.close(); return r.status_code,buf.decode('utf-8','ignore'),r.url
def anon_key(blob):
    for m in ANON.finditer(blob):
        p=b64d(m.group(1).split('.')[1])
        if '"role":"anon"' in p.replace(' ',''): return m.group(1)
    return None
def probe(host):
    o={"host":host,"supa":None,"has_anon":False,"schema_exposed":None,"tables":0,"err":None}
    try:
        c,html,final=get(f"https://{host}/")
        if c!=200: o["err"]=f"root:{c}"; return o
        blob=html
        for m in list(SRC.finditer(html))[:8]:
            u=urljoin(final,m.group(1))
            try:
                c2,t2,_=get(u)
                if c2==200: blob+="\n"+t2
            except:pass
        mu=SUPAURL.search(blob)
        if not mu: o["err"]="no-supabase-url"; return o
        ref=mu.group(1); o["supa"]=ref
        ak=anon_key(blob)
        if not ak: o["err"]="no-anon-key"; return o
        o["has_anon"]=True
        # UN solo request benigno: raiz REST. Devuelve el spec OpenAPI (definiciones de tablas)
        # si el rol anon tiene acceso al schema. NO leemos filas de ninguna tabla.
        h={"apikey":ak,"Authorization":f"Bearer {ak}","Accept":"application/openapi+json"}
        rr=S.get(f"https://{ref}.supabase.co/rest/v1/",headers=h,timeout=T)
        if rr.status_code==200:
            try:
                spec=rr.json(); defs=spec.get("definitions",spec.get("components",{}).get("schemas",{}))
                # excluir el pseudo-path raiz
                tbls=[k for k in defs.keys()]
                o["schema_exposed"]=True; o["tables"]=len(tbls); o["table_names"]=tbls[:12]
            except: o["schema_exposed"]=True; o["tables"]=-1
        else:
            o["schema_exposed"]=False; o["rest_status"]=rr.status_code
    except Exception as e:
        o["err"]=type(e).__name__
    return o
hosts=[r["host"] for r in json.load(open("out_all.json")) if r["ok"] and r["uses_supabase"]]
res=[]; 
with ThreadPoolExecutor(max_workers=10) as ex:
    for r in ex.map(probe,hosts): res.append(r)
json.dump(res,open("rls_out.json","w"),indent=1)
n=len(res); anon=[r for r in res if r["has_anon"]]
schema=[r for r in anon if r["schema_exposed"]]
print("supabase apps:",n)
print("con anon key extraible del bundle:",len(anon))
print("con SCHEMA de la DB accesible con solo la anon key (sin login):",len(schema))
for r in schema: print("  ",r["host"],"tablas:",r["tables"],r.get("table_names"))
