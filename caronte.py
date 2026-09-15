#!/usr/bin/env python3
"""
Caronte - analizador de apps vibecodeadas.

Le pasas una carpeta con el codigo, o la URL de la app publicada, o las dos,
y te devuelve el JSON con los problemas, en el mismo formato que ejemplo.json.

Uso:
    python caronte.py --carpeta ./turnos-belgrano
    python caronte.py --url https://turnos-belgrano.lovable.app
    python caronte.py --carpeta ./turnos-belgrano --url https://turnos-belgrano.lovable.app
    python caronte.py --repo https://github.com/santi/turnos-belgrano

Instalar:  pip install requests playwright && playwright install chromium
(Playwright es opcional: si no esta, se saltean los chequeos de navegador.)
"""
import argparse, base64, json, os, re, subprocess, sys, tempfile, time
from datetime import datetime, timezone, timedelta

import requests

AR = timezone(timedelta(hours=-3))
UA = {"User-Agent": "Mozilla/5.0 (Caronte)"}

# ---------------------------------------------------------------- utilidades

def ahora():
    return datetime.now(AR).isoformat(timespec="seconds")

# Lo que Caronte NO puede arreglar solo, por mas que sepa que esta mal: hace falta que
# la persona rote una clave, entre a una cuenta, decida quien es el dueno, o ponga una
# imagen suya. Prometer que lo arregla y despues no poder es peor que no prometerlo.
NO_SOLO = ("secreto-", "supabase-service-role", "env-commiteado", "admin-sin-clave",
           "sin-preview", "sin-favicon", "favicon-ajeno", "imagenes-rotas",
           "errores-consola", "pedidos-fallidos", "no-abre", "no-carga", "pantalla-blanco")

def problema(pid, eje, gravedad, titulo, donde, por_que, arregla=None):
    if arregla is None:
        arregla = not pid.startswith(NO_SOLO)
    return {"id": pid, "eje": eje, "gravedad": gravedad, "titulo": titulo,
            "donde": donde, "por_que_importa": por_que, "lo_arregla_caronte": arregla}

def leer(path, limite=400_000):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(limite)
    except Exception:
        return ""

def archivos(carpeta, exts=None, saltar=("node_modules", ".git", "dist", "build", ".next")):
    for raiz, dirs, files in os.walk(carpeta):
        dirs[:] = [d for d in dirs if d not in saltar]
        for f in files:
            if exts and not f.endswith(exts):
                continue
            p = os.path.join(raiz, f)
            try:
                if os.path.getsize(p) > 2_000_000:
                    continue
            except OSError:
                continue
            yield p

def rel(carpeta, path):
    return os.path.relpath(path, carpeta).replace("\\", "/")

# ------------------------------------------------------- chequeos del codigo

SECRETOS = {
    "clave de OpenAI":    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}"),
    "clave de Anthropic": re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{20,}"),
    "clave secreta de Stripe": re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{20,}"),
    "clave de AWS":       re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "clave de Resend":    re.compile(r"\bre_[A-Za-z0-9]{8,}_[A-Za-z0-9]{10,}"),
    "clave de SendGrid":  re.compile(r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"),
    "clave privada":      re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}")

def rol_del_jwt(texto):
    """Devuelve los roles que vienen adentro de los JWT de Supabase que encuentre."""
    roles = set()
    for m in JWT.finditer(texto):
        cuerpo = m.group(0).split(".")[1]
        cuerpo += "=" * (-len(cuerpo) % 4)
        try:
            payload = base64.urlsafe_b64decode(cuerpo).decode("utf-8", "ignore")
        except Exception:
            continue
        r = re.search(r'"role"\s*:\s*"([^"]+)"', payload)
        if r:
            roles.add(r.group(1))
    return roles

def revisar_secretos(carpeta):
    out = []
    for p in archivos(carpeta, exts=(".ts", ".tsx", ".js", ".jsx", ".json", ".env",
                                     ".html", ".sql", ".md", ".yml", ".yaml")):
        if "/.env.example" in rel(carpeta, p):
            continue
        txt = leer(p)
        for nombre, pat in SECRETOS.items():
            if pat.search(txt):
                out.append(problema(
                    f"secreto-{os.path.basename(p)}", "segura", "critico",
                    f"Hay una {nombre} escrita dentro del codigo",
                    rel(carpeta, p),
                    "Cualquiera que vea el repo o el codigo de la app publicada se la lleva y la usa a tu nombre. "
                    "Rotala y movela a una variable de entorno."))
                break
        if "service_role" in rol_del_jwt(txt):
            out.append(problema(
                "supabase-service-role", "segura", "critico",
                "La clave de administrador de Supabase esta en el codigo",
                rel(carpeta, p),
                "Esa clave se saltea todos los permisos de tu base. Quien la tenga puede leer y borrar todo."))
    return out

def revisar_env(carpeta):
    out = []
    gitignore = leer(os.path.join(carpeta, ".gitignore"))
    tiene_env = os.path.exists(os.path.join(carpeta, ".env"))
    ignora_env = bool(re.search(r"^\s*\.env\s*$", gitignore, re.M)) or ".env*" in gitignore
    if tiene_env and not ignora_env:
        out.append(problema(
            "env-commiteado", "segura", "critico",
            "El archivo .env con tus claves esta subido al repositorio",
            ".env",
            "Todo lo que pusiste ahi quedo publicado. Sacalo del repo, rota las claves y agregalo al .gitignore."))
    elif not ignora_env and gitignore:
        out.append(problema(
            "gitignore-sin-env", "segura", "alto",
            "El .gitignore no ignora el archivo .env",
            ".gitignore",
            "Todavia no pasa nada, pero el dia que crees un .env con claves lo vas a subir sin darte cuenta. "
            "Es el descuido mas comun en apps hechas con Vite."))
    return out

CREATE_TABLE = re.compile(r"create\s+table\s+(?:if\s+not\s+exists\s+)?[\"']?(?:public\.)?[\"']?(\w+)", re.I)
ENABLE_RLS   = re.compile(r"alter\s+table\s+[\"']?(?:public\.)?[\"']?(\w+)[\"']?\s+enable\s+row\s+level\s+security", re.I)
DISABLE_RLS  = re.compile(r"alter\s+table\s+[\"']?(?:public\.)?[\"']?(\w+)[\"']?\s+disable\s+row\s+level\s+security", re.I)
CREATE_POLICY= re.compile(r"create\s+policy[\s\S]{0,200}?\son\s+[\"']?(?:public\.)?[\"']?(\w+)", re.I)
DROP_POLICY  = re.compile(r"drop\s+policy[\s\S]{0,200}?\son\s+[\"']?(?:public\.)?[\"']?(\w+)", re.I)

def revisar_base(carpeta):
    """Busca tablas creadas que no tengan el candado puesto ni reglas de acceso.

    Las migraciones se leen EN ORDEN (por nombre de archivo, que en Supabase lleva
    timestamp). Vale el estado final: si una migracion prende el candado y otra
    posterior lo apaga, la tabla queda abierta. Sin esto, un disable posterior
    pasaba desapercibido."""
    mig = os.path.join(carpeta, "supabase", "migrations")
    if not os.path.isdir(mig):
        return []
    tablas, donde = set(), {}
    rls_final, policies = {}, {}
    for p in sorted(archivos(mig, exts=(".sql",))):
        sql = leer(p)
        for t in CREATE_TABLE.findall(sql):
            t = t.lower(); tablas.add(t); donde.setdefault(t, rel(carpeta, p))
            rls_final.setdefault(t, False); policies.setdefault(t, 0)
        for t in ENABLE_RLS.findall(sql):
            rls_final[t.lower()] = True
        for t in DISABLE_RLS.findall(sql):
            rls_final[t.lower()] = False
            donde[t.lower()] = rel(carpeta, p)
        for t in CREATE_POLICY.findall(sql):
            policies[t.lower()] = policies.get(t.lower(), 0) + 1
        for t in DROP_POLICY.findall(sql):
            policies[t.lower()] = max(0, policies.get(t.lower(), 0) - 1)
    con_rls = {t for t, v in rls_final.items() if v}
    con_policy = {t for t, n in policies.items() if n > 0}
    out = []
    for t in sorted(tablas):
        if t not in con_rls:
            out.append(problema(
                f"db-{t}-abierta", "segura", "critico",
                f"Cualquiera puede leer y borrar la tabla {t} sin estar logueado",
                donde.get(t, "supabase/migrations/"),
                f"La tabla {t} no tiene el candado puesto. Como la clave publica de tu app viene en el codigo "
                f"(y eso es normal), cualquiera que abra tu app puede bajarse el contenido entero de esa tabla, o borrarlo."))
        elif t not in con_policy:
            out.append(problema(
                f"db-{t}-sin-reglas", "segura", "alto",
                f"La tabla {t} tiene el candado puesto pero ninguna regla de acceso",
                donde.get(t, "supabase/migrations/"),
                f"Con el candado puesto y sin reglas, nadie puede leer ni escribir en {t}. "
                f"Probablemente tu app este fallando en silencio al usarla."))
    return out

GENERICO = re.compile(r"^(lovable([- ]?(app|generated[- ]project|project))?|vite_react_shadcn_ts|"
                      r"vite(\s*\+\s*react)?(\s*\+\s*ts)?|react app|create next app|next\.js app|"
                      r"my app|untitled|index|app|home|welcome)\s*$", re.I)

# Iconos que vienen con el andamio: si quedo alguno, la pestaña muestra el logo de la
# herramienta con la que se hizo la app, no el del negocio.
ICONO_AJENO = ("vite.svg", "next.svg", "vercel.svg", "react.svg", "logo192.png", "logo512.png")

def revisar_metadatos(html, donde):
    """Los chequeos de 'se puede compartir'. Sirve igual para el index.html del repo
    que para el HTML que devuelve la app publicada."""
    def tag(pat):
        m = re.search(pat, html, re.I | re.S)
        return m.group(1).strip() if m else None

    titulo = tag(r"<title[^>]*>(.*?)</title>") or ""
    desc   = tag(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)')
    ogimg  = tag(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']*)')
    autor  = tag(r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']*)')
    lang   = tag(r'<html[^>]+lang=["\']([^"\']*)')
    favicon= re.search(r'<link[^>]+rel=["\'][^"\']*icon[^>]*>', html, re.I)
    editor = re.search(r"gpteng\.co|gptengineer\.js|cdn\.gpteng", html, re.I)

    icono = ""
    if favicon:
        h = re.search(r'href=["\']([^"\']*)', favicon.group(0), re.I)
        icono = (h.group(1) if h else "").lower()

    marca_ajena = (ogimg and ("lovable.dev" in ogimg or "opengraph-image-p98pqg" in ogimg)) \
                  or (autor and "lovable" in autor.lower())
    titulo_generico = bool(GENERICO.match(titulo or ""))

    out = []
    if marca_ajena:
        out.append(problema(
            "metadatos-sin-tocar", "compartible", "medio",
            "Al compartir el link aparece la marca de Lovable, no la tuya",
            donde,
            f"El titulo dice \"{titulo or 'nada'}\" y la imagen de preview sigue siendo la de Lovable. "
            "Cuando pasas el link por WhatsApp parece el link de otra empresa."))
    else:
        if not titulo:
            out.append(problema("sin-titulo", "compartible", "medio",
                "La app no tiene titulo", donde,
                "En la pestaña del navegador y en los buscadores no aparece ningun nombre."))
        elif titulo_generico:
            out.append(problema("titulo-por-defecto", "compartible", "medio",
                "La pestaña del navegador sigue diciendo el nombre de fabrica", donde,
                f"Dice \"{titulo}\". Ese es el titulo que viene puesto de antes, no el de tu negocio. "
                "Es lo que se guarda cuando alguien te agrega a favoritos y lo que aparece al compartir el link."))
        if not ogimg:
            out.append(problema("sin-preview", "compartible", "medio",
                "Al compartir el link no aparece ninguna imagen", donde,
                "Cuando alguien pasa tu link por WhatsApp sale un recuadro vacio. "
                "Es lo primero que ve alguien que todavia no te conoce."))
    if not desc:
        out.append(problema("sin-descripcion", "compartible", "bajo",
            "La app no tiene descripcion", donde,
            "Es el renglon que aparece abajo del titulo en Google y al compartir el link."))
    if not favicon:
        out.append(problema("sin-favicon", "compartible", "bajo",
            "La app no tiene icono propio en la pestaña", donde,
            "Queda el icono gris por defecto, al lado de las otras pestañas de la persona."))
    elif any(a in icono for a in ICONO_AJENO):
        out.append(problema("favicon-ajeno", "compartible", "bajo",
            "El icono de la pestaña no es tuyo", donde,
            f"Quedo puesto {icono}, que es el logo de la herramienta con la que se armo la app. "
            "En la pestaña del navegador tu negocio aparece con el logo de otro."))
    if not lang:
        out.append(problema("sin-idioma", "compartible", "bajo",
            "La pagina no declara en que idioma esta", donde,
            "Los lectores de pantalla y el traductor del navegador no saben que hacer."))
    if editor:
        out.append(problema("editor-en-produccion", "compartible", "bajo",
            "La app sigue cargando el script del editor de Lovable", donde,
            "Tus usuarios se descargan un script que solo sirve para editar la app. Pesa de mas y no les hace nada."))
    return out

PALABRA_ADMIN = r"(?:admin|panel|backoffice|dashboard)"
RUTA_ADMIN  = re.compile(r'path\s*=\s*["\'](/[\w\-/]*' + PALABRA_ADMIN + r'[\w\-/]*)["\']', re.I)
NOMBRE_ADMIN = re.compile(PALABRA_ADMIN, re.I)
# Cualquier señal de que en algun lado se pide identificarse. Es a proposito amplia:
# ante la duda preferimos no avisar, antes que avisar de algo que si esta protegido.
AUTH = re.compile(r"supabase\.auth|auth\.getUser|getSession|signInWith|onAuthStateChange|"
                  r"next-auth|@clerk|useUser\s*\(|requireAuth|withAuth|ProtectedRoute|"
                  r"RequireAuth|isAdmin|password|contrase", re.I)

def revisar_rutas_admin(carpeta):
    """Busca una pagina de administracion a la que se entre sin identificarse.
    Solo avisa si en TODO el codigo no hay ni una señal de login."""
    rutas, archivo_admin, hay_auth, donde_ruta = set(), None, False, None
    for p in archivos(carpeta, exts=(".ts", ".tsx", ".js", ".jsx")):
        r = rel(carpeta, p)
        if "/dist/" in r or r.endswith(".d.ts"):
            continue
        txt = leer(p)
        if AUTH.search(txt):
            hay_auth = True
        encontradas = RUTA_ADMIN.findall(txt)
        if encontradas:
            rutas.update(x.lower() for x in encontradas)
            donde_ruta = donde_ruta or r
        base = os.path.basename(r)
        # Admin.tsx suelto, o app/admin/page.tsx y pages/admin.tsx de Next
        if NOMBRE_ADMIN.match(os.path.splitext(base)[0]) or \
           (base in ("page.tsx", "page.jsx", "route.ts") and NOMBRE_ADMIN.search(os.path.dirname(r))):
            archivo_admin = archivo_admin or r
            if base.startswith("page"):
                rutas.add("/" + os.path.dirname(r).split("app/")[-1].split("pages/")[-1])

    if not rutas or hay_auth:
        return []
    ruta = sorted(rutas)[0]
    return [problema(
        "admin-sin-clave", "segura", "critico",
        f"La pagina {ruta} entra cualquiera que sepa la direccion",
        archivo_admin or donde_ruta or "src/",
        f"No hay ningun control: escribiendo {ruta} en la barra del navegador se ve todo lo que hay ahi "
        "adentro y se puede cambiar. En el codigo no aparece ni una pantalla de ingreso.")]

ESCRITURA = re.compile(r"\.from\(\s*['\"](\w+)['\"]\s*\)\s*\.\s*(insert|update|upsert|delete)\b")
HAY_FORM  = re.compile(r"<form[\s>]|onSubmit\s*=", re.I)

def revisar_formularios(carpeta):
    """Un formulario que guarda algo y nunca mira si salio bien. La persona toca
    Confirmar, ve "listo", y del otro lado no llego nada. Es el peor de todos porque
    no se nota: la app parece que anda.

    Somos conservadores a proposito: solo avisamos si en TODO el archivo no aparece
    la palabra error. Con que la miren una vez, asumimos que algo hacen con ella."""
    out = []
    for p in archivos(carpeta, exts=(".tsx", ".jsx", ".ts", ".js")):
        r = rel(carpeta, p)
        if "/dist/" in r or r.endswith(".d.ts"):
            continue
        txt = leer(p)
        escrituras = ESCRITURA.findall(txt)
        if not escrituras or not HAY_FORM.search(txt):
            continue
        if re.search(r"\berror\b|\bcatch\b", txt, re.I):
            continue
        tabla = escrituras[0][0]
        out.append(problema(
            "form-sin-aviso-error", "funciona", "alto",
            "Si falla al guardar, el formulario dice que salio todo bien igual",
            r,
            f"El formulario guarda en {tabla} y nunca chequea si se guardo. Si falla "
            "(se corto internet, la base rechazo el dato), a la persona igual le aparece el "
            "mensaje de listo y se va tranquila. Vos nunca te enteras de que ese dato no llego."))
        break   # con marcarlo una vez alcanza, no hace falta uno por archivo
    return out

def rutas_de_la_app(carpeta):
    """Que otras pantallas, ademas de la home, vale la pena abrir en el navegador.
    Hoy: la de administracion, que es donde suelen estar las tablas que no entran en el celular."""
    rutas = set()
    if not carpeta:
        return ["/"]
    for p in archivos(carpeta, exts=(".ts", ".tsx", ".js", ".jsx")):
        r = rel(carpeta, p)
        if "/dist/" in r or r.endswith(".d.ts"):
            continue
        rutas.update(x.lower() for x in RUTA_ADMIN.findall(leer(p)))
    return ["/"] + sorted(x for x in rutas if x != "/")[:2]

def revisar_codigo(carpeta):
    out = []
    out += revisar_secretos(carpeta)
    out += revisar_env(carpeta)
    out += revisar_base(carpeta)
    out += revisar_rutas_admin(carpeta)
    out += revisar_formularios(carpeta)
    index = os.path.join(carpeta, "index.html")
    if os.path.exists(index):
        out += revisar_metadatos(leer(index), "index.html")
    return out

def detectar_stack(carpeta):
    pkg = leer(os.path.join(carpeta, "package.json"))
    stack = []
    if '"vite"' in pkg: stack.append("vite")
    if '"next"' in pkg: stack.append("next")
    if '"react"' in pkg: stack.append("react")
    if "supabase" in pkg: stack.append("supabase")
    hecha_con = "desconocido"
    if os.path.exists(os.path.join(carpeta, "src/integrations/supabase/client.ts")) \
       or "vite_react_shadcn_ts" in pkg:
        hecha_con = "lovable"
    elif os.path.isdir(os.path.join(carpeta, ".bolt")):
        hecha_con = "bolt"
    return " + ".join(stack) or "desconocido", hecha_con

# ------------------------------------------------ chequeos de la app publicada

def revisar_publicada(url):
    """Lo que se puede ver pidiendo el HTML. No necesita navegador."""
    try:
        r = requests.get(url, headers=UA, timeout=25)
    except Exception as e:
        return [problema("no-abre", "funciona", "critico",
                "La app publicada no responde", url,
                f"No se pudo abrir la direccion ({type(e).__name__}). Si a vos te abre, puede ser algo temporal.")]
    if r.status_code != 200:
        return [problema("no-abre", "funciona", "critico",
                f"La app publicada devuelve error {r.status_code}", url,
                "Quien entre al link no ve nada.")]
    return revisar_metadatos(r.text, "la app publicada")

def revisar_navegador(url, rutas=("/",), carpeta_capturas="capturas", avisar=None, tope=160):
    """Abre la app de verdad en celular y en compu. Necesita Playwright.
    `rutas` son las pantallas a visitar: la home siempre, mas las que salgan del codigo
    (la de administracion, por ejemplo, que es donde suelen estar las tablas anchas).

    `avisar` es una funcion que recibe un texto y lo publica para que la persona vea que
    seguimos trabajando. Esta parte tarda uno o dos minutos y sin avisos parece colgada."""
    def contar(txt):
        if avisar:
            try:
                avisar(txt)
            except Exception as e:
                # No cortamos el analisis por no poder avisar, pero que quede dicho.
                print(f"    (no pude avisar: {e})", file=sys.stderr)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  (Playwright no esta instalado, salteo los chequeos de navegador)", file=sys.stderr)
        return [], []

    os.makedirs(carpeta_capturas, exist_ok=True)
    problemas, capturas = [], []
    pantallas = [("celular", 390, 844), ("compu", 1440, 900)]
    rutas = list(dict.fromkeys(["/"] + [r for r in rutas if r]))

    # Fecha limite para todo este tramo. Cada pagina tiene su tope, pero con varias rutas
    # y dos pantallas la suma se va a las nubes, asi que le ponemos un techo al conjunto.
    vence = time.monotonic() + tope
    saltadas = []

    with sync_playwright() as p:
        nav = p.chromium.launch()
        for nombre, ancho, alto in pantallas:
          for ruta in rutas:
            slug = "home" if ruta == "/" else ruta.strip("/").replace("/", "-")
            suf = "" if ruta == "/" else f"-{slug}"
            destino = url.rstrip("/") + ruta

            if time.monotonic() >= vence:
                saltadas.append(f"{ruta} en {nombre}")
                contar(f"Se nos hizo tarde, no alcanzamos a abrir {ruta} en {nombre}")
                continue

            contar(f"Abriendo {ruta} en {nombre}")
            ctx = nav.new_context(viewport={"width": ancho, "height": alto})
            pag = ctx.new_page()
            errores, fallidos = [], []
            pag.on("console", lambda m: errores.append(m.text[:200]) if m.type == "error" else None)
            pag.on("pageerror", lambda e: errores.append(str(e)[:200]))
            pag.on("response", lambda r: fallidos.append(f"{r.status} {r.url[:80]}") if r.status >= 400 else None)
            try:
                pag.goto(destino, wait_until="networkidle",
                         timeout=int(max(8, min(40, vence - time.monotonic())) * 1000))
                pag.wait_for_timeout(1500)
            except Exception as e:
                problemas.append(problema(f"no-carga{suf}-{nombre}", "funciona", "critico",
                    f"La pantalla {ruta} no termina de cargar", ruta,
                    f"Se corto la carga ({type(e).__name__})."))
                ctx.close(); continue

            m = pag.evaluate("""() => ({
                anchoReal: document.documentElement.scrollWidth,
                anchoPantalla: document.documentElement.clientWidth,
                largoTexto: (document.body ? document.body.innerText : '').trim().length,
                imgsRotas: Array.from(document.images).filter(i => i.complete && i.naturalWidth === 0).length
            })""")

            contar(f"Sacando la foto de {ruta} en {nombre}")
            archivo = os.path.join(carpeta_capturas, f"{slug}-{nombre}.png").replace("\\", "/")
            # En celular la sacamos del tamaño de la pantalla, no de la pagina entera: si la
            # sacaramos entera se veria todo el contenido acomodado y no se notaria que en el
            # telefono queda cortado, que es justamente lo que hay que mostrar.
            pag.screenshot(path=archivo, full_page=(nombre != "celular"))
            capturas.append({"ruta": ruta, "pantalla": nombre, "momento": "antes", "url": archivo})

            if nombre == "celular":
                sobra = m["anchoReal"] - m["anchoPantalla"]
                if sobra > 8:
                    problemas.append(problema(f"desborda{suf}-celular", "usable", "alto",
                        f"El contenido de {ruta} se sale de la pantalla en el celular", ruta,
                        f"A {ancho}px de ancho el contenido mide {m['anchoReal']}px. Hay que scrollear de costado "
                        "para leer, y la mayoria de la gente entra desde el celular."))
            if m["largoTexto"] < 60:
                problemas.append(problema(f"pantalla-blanco{suf}-{nombre}", "funciona", "critico",
                    f"La pantalla {ruta} se ve en blanco en {nombre}", ruta,
                    "Carga pero no muestra nada. Suele ser un error de javascript que rompe todo."))
            if m["imgsRotas"]:
                problemas.append(problema(f"imagenes-rotas{suf}-{nombre}", "usable", "medio",
                    f"Hay {m['imgsRotas']} imagenes que no cargan en {ruta}", ruta,
                    "Quedan recuadros vacios en el lugar de la imagen."))
            if errores:
                problemas.append(problema(f"errores-consola{suf}-{nombre}", "funciona", "alto",
                    f"La pantalla {ruta} tira {len(errores)} errores al abrirse", ruta,
                    f"El primero dice: {errores[0][:120]}. Los errores al cargar suelen romper alguna parte de la app."))
            if fallidos:
                problemas.append(problema(f"pedidos-fallidos{suf}-{nombre}", "funciona", "alto",
                    f"Hay {len(fallidos)} pedidos que fallan al abrir {ruta}", ruta,
                    f"El primero: {fallidos[0][:120]}. Algo que la app necesita no esta llegando."))
            ctx.close()
        nav.close()

    if saltadas:
        # Nunca dar por bueno lo que no miramos.
        problemas.append(problema(
            "no-alcanzamos-a-mirar", "funciona", "medio",
            f"No alcanzamos a probar {len(saltadas)} de tus pantallas", ", ".join(saltadas[:3]),
            "Tu app tardo mucho en abrir y cortamos para no dejarte esperando. Lo que no "
            "miramos no lo estamos dando por bueno: proba de nuevo mas tarde.",
            arregla=False))

    # dedup: si el mismo problema salio en las dos pantallas, dejamos uno
    vistos, unicos = set(), []
    for pr in problemas:
        clave = pr["id"].rsplit("-", 1)[0] if pr["id"].endswith(("-celular", "-compu")) else pr["id"]
        if clave in vistos: continue
        vistos.add(clave); unicos.append(pr)
    return unicos, capturas

# ------------------------------------------------------------------- puntaje

RESTA = {"critico": 12, "alto": 8, "medio": 5, "bajo": 2}
EJES = ["segura", "funciona", "usable", "compartible"]

def calcular_score(problemas, ejes_medidos):
    """Un eje que no se midio NO puede valer 25. Vale null y no suma al total.
    Asi el puntaje nunca dice que algo esta bien cuando en realidad no se miro."""
    score = {}
    for eje in EJES:
        if eje not in ejes_medidos:
            score[eje] = None
            continue
        puntos = 25
        for pr in problemas:
            if pr["eje"] == eje:
                puntos -= RESTA.get(pr["gravedad"], 5)
        score[eje] = max(0, puntos)
    score["total"] = sum(v for v in score.values() if isinstance(v, int))
    score["sobre"] = 25 * len(ejes_medidos)
    score["ejes_medidos"] = sorted(ejes_medidos)
    return score

# ---------------------------------------------------------------------- main

def clonar(repo, tope=120):
    """Baja el repo. Con tope de tiempo y sin que git se quede esperando que alguien le
    escriba usuario y contrasena: si el repo es privado queremos el error ahora, no un
    proceso colgado para siempre."""
    destino = tempfile.mkdtemp(prefix="caronte-")
    print(f"  bajando {repo} ...")
    entorno = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
               "GIT_ASKPASS": "echo", "SSH_ASKPASS": "echo"}
    try:
        subprocess.run(["git", "clone", "--depth", "1", repo, destino],
                       check=True, capture_output=True, timeout=tope, env=entorno)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git clone tardo mas de {tope} segundos y lo cortamos. "
                           "Puede ser un repo muy grande o una conexion lenta.")
    except subprocess.CalledProcessError as e:
        detalle = (e.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
        raise RuntimeError("git clone fallo: " + (detalle[-1] if detalle else "sin detalle"))
    return destino

def main():
    ap = argparse.ArgumentParser(description="Analiza una app vibecodeada")
    ap.add_argument("--repo",    help="URL del repositorio de GitHub (lo baja solo)")
    ap.add_argument("--carpeta", help="Carpeta local con el codigo ya bajado")
    ap.add_argument("--url",     help="URL de la app publicada")
    ap.add_argument("--nombre",  default="", help="Nombre de la app para el reporte")
    ap.add_argument("--salida",  default="resultado.json")
    ap.add_argument("--sin-navegador", action="store_true", help="No abrir el navegador")
    a = ap.parse_args()

    if not (a.repo or a.carpeta or a.url):
        ap.error("hace falta --repo, --carpeta o --url")

    carpeta = a.carpeta
    if a.repo and not carpeta:
        carpeta = clonar(a.repo)

    problemas, capturas = [], []
    ejes_medidos = set()
    stack, hecha_con = "desconocido", "desconocido"
    rutas = ["/"]

    if carpeta:
        print("  revisando el codigo ...")
        stack, hecha_con = detectar_stack(carpeta)
        problemas += revisar_codigo(carpeta)
        rutas = rutas_de_la_app(carpeta)
        ejes_medidos |= {"segura", "compartible"}

    if a.url:
        print("  revisando la app publicada ...")
        problemas += revisar_publicada(a.url)
        ejes_medidos.add("compartible")
        if not a.sin_navegador:
            print(f"  abriendo la app en celular y en compu ({', '.join(rutas)}) ...")
            pr, cap = revisar_navegador(a.url, rutas)
            if pr or cap:
                ejes_medidos |= {"funciona", "usable"}
            problemas += pr
            capturas += cap

    # sacar duplicados por id (el mismo problema puede salir del repo y de la app publicada)
    vistos, unicos = set(), []
    for pr in problemas:
        if pr["id"] in vistos: continue
        vistos.add(pr["id"]); unicos.append(pr)
    orden = {"critico": 0, "alto": 1, "medio": 2, "bajo": 3}
    unicos.sort(key=lambda p: orden.get(p["gravedad"], 9))

    resultado = {
        "version": 1,
        "estado": "listo",
        "app": {
            "nombre": a.nombre or (a.url or a.repo or carpeta or "").rstrip("/").split("/")[-1],
            "repo": a.repo or "", "url": a.url or "",
            "hecha_con": hecha_con, "stack": stack, "analizada_el": ahora(),
        },
        "score": calcular_score(unicos, ejes_medidos),
        "problemas": unicos,
        "plan": [],          # lo llena el paso de Claude (ver prompt_plan.md)
        "capturas": capturas,
    }

    with open(a.salida, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)

    s = resultado["score"]
    detalle = " · ".join(f"{e} {s[e]}" if s[e] is not None else f"{e} sin medir" for e in EJES)
    print(f"\n  Puntaje: {s['total']}/{s['sobre']}   ({detalle})")
    if s["sobre"] < 100:
        faltan = [e for e in EJES if s[e] is None]
        print(f"  OJO: no se midieron {', '.join(faltan)}. Corre con --url para medir todo.")
    print(f"  Problemas encontrados: {len(unicos)}")
    for pr in unicos:
        print(f"    [{pr['gravedad']:8}] {pr['titulo']}")
    print(f"\n  Guardado en {a.salida}")

if __name__ == "__main__":
    main()
