import re, json, sys
from concurrent.futures import ThreadPoolExecutor
import requests
S=requests.Session(); S.headers.update({"User-Agent":"Mozilla/5.0 (readiness-research)"})
GEN=re.compile(r"^(lovable([- ]?(app|generated[- ]project|project))?|vite_react_shadcn_ts|vite \+ react|react app|my app|untitled|index|app|home|vite|welcome)\s*$", re.I)
def tag(h,pat):
    m=re.search(pat,h,re.I|re.S); return m.group(1).strip() if m else None
def one(host):
    r={"host":host,"ok":False}
    try:
        resp=S.get(f"https://{host}/",timeout=20)
        if resp.status_code!=200: r["err"]=resp.status_code; return r
        h=resp.text; r["ok"]=True
        title=tag(h,r"<title[^>]*>(.*?)</title>") or ""
        desc=tag(h,r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)')
        ogimg=tag(h,r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']*)')
        ogtitle=tag(h,r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)')
        author=tag(h,r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']*)')
        lang=tag(h,r'<html[^>]+lang=["\']([^"\']*)')
        r["title"]=title[:70]
        r["title_generico"]= (not title) or bool(GEN.match(title))
        r["sin_description"]= not (desc and desc.strip())
        r["og_default_lovable"]= bool(ogimg and ("lovable.dev" in ogimg or "opengraph-image-p98pqg" in ogimg))
        r["og_title_generico"]= bool(ogtitle and GEN.match(ogtitle.strip()))
        r["sin_og_image"]= not ogimg
        r["author_lovable"]= bool(author and "lovable" in author.lower())
        r["sin_favicon"]= not re.search(r'<link[^>]+rel=["\'][^"\']*icon',h,re.I)
        r["sin_lang"]= not (lang and lang.strip())
        # script del editor de Lovable servido a usuarios finales en produccion
        r["script_editor_en_prod"]= bool(re.search(r"gpteng\.co|gptengineer\.js|cdn\.gpteng",h,re.I))
        r["twitter_lovable"]= bool(re.search(r'content=["\']@?lovable_dev["\']',h,re.I))
    except Exception as e:
        r["err"]=type(e).__name__
    return r
hosts=[x.strip() for x in open(sys.argv[1]) if x.strip()]
with ThreadPoolExecutor(max_workers=12) as ex: res=list(ex.map(one,hosts))
json.dump(res,open(sys.argv[2],"w"),indent=1)
live=[r for r in res if r.get("ok")]
FLAGS=["title_generico","sin_description","og_default_lovable","sin_og_image","author_lovable",
       "sin_favicon","sin_lang","script_editor_en_prod","twitter_lovable","og_title_generico"]
print(f"apps analizadas: {len(live)}")
for f in FLAGS:
    n=sum(1 for r in live if r.get(f))
    print(f"  {f:26s} {n:3d}/{len(live)}  {100*n/len(live):5.1f}%")
def nfail(r): return sum(1 for f in ["title_generico","sin_description","og_default_lovable","sin_favicon","sin_lang","script_editor_en_prod"] if r.get(f))
import statistics
fails=[nfail(r) for r in live]
print(f"\napps con AL MENOS 1 senal de 'nunca lo termine': {sum(1 for f in fails if f>=1)}/{len(live)} ({100*sum(1 for f in fails if f>=1)/len(live):.0f}%)")
print(f"apps con 3 o mas: {sum(1 for f in fails if f>=3)}/{len(live)}")
print(f"promedio de senales por app: {statistics.mean(fails):.1f}")
