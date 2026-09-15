#!/usr/bin/env python3
"""
El worker. Mira la tabla runs de Supabase, agarra los analisis que estan en cola,
los corre y escribe el resultado. Es lo que conecta la web con el analizador.

El contrato esta en docs/contrato.md: el worker SOLO escribe estado, paso,
score_total y resultado. La web solo lee.

    python worker.py            corre para siempre, mirando la cola cada 2 segundos
    python worker.py --una-vez  agarra uno solo y termina (para probar)

La clave de Supabase y la de Anthropic salen de caronte-web/.env.local, asi hay un
solo lugar donde ponerlas.
"""
import argparse, json, os, re, shutil, sys, tempfile, time, traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import caronte
from plan import armar_plan
from entender import entender, rejuzgar

RAIZ = Path(__file__).resolve().parent.parent

def cargar_env():
    """Lee caronte-web/.env.local sin pisar lo que ya este en el entorno."""
    archivo = RAIZ / "caronte-web" / ".env.local"
    if not archivo.exists():
        return
    for linea in archivo.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        k, v = linea.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

cargar_env()

URL  = os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "").rstrip("/")
ANON = os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY", "")
REST = f"{URL}/rest/v1/runs"
CAB  = {"apikey": ANON, "Authorization": f"Bearer {ANON}", "Content-Type": "application/json"}

# Una sesion con reintentos. Internet se corta: un "connection reset" nos mato un analisis
# entero que ya habia costado plata, solo porque escribir el resultado fallo una vez.
# Reintentar es seguro: todas nuestras escrituras ponen valores fijos, no incrementan nada.
SESION = requests.Session()
SESION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=4, backoff_factor=0.7,
    status_forcelist=(408, 429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST", "PATCH", "DELETE"]),
)))

# Todos los topes de tiempo en un solo lugar. Sin esto, cualquier cosa que se cuelgue
# deja el analisis dando vueltas para siempre y la persona mirando una rueda.
TOPE_CLONAR    = 120    # bajar el repo
TOPE_MODELO    = 90     # cada llamada a Claude
TOPE_NAVEGADOR = 160    # abrir la app en celular y en compu, todas las rutas juntas
# Generoso a proposito. Cada tramo ya tiene su tope, asi que este es solo la red de
# seguridad para algo raro. Una corrida que viene avanzando NO se corta.
TOPE_TOTAL     = 900    # el analisis entero, de punta a punta
# Cuanto silencio hace falta para dar por muerto un analisis que figura "trabajando".
# El worker avisa cada 10 o 15 segundos, asi que cuatro minutos callado es que se murio.
SILENCIO_MUERTO = 240
TERMINALES     = ("listo", "error")
# Estados en los que alguien tendria que estar trabajando la fila. Si una queda ahi
# parada, es que el worker se murio. Ojo: "en_cola" NO va aca. Una fila esperando no
# esta colgada, esta esperando, y puede llevar horas si nadie levanto el worker.
TRABAJANDO     = ("bajando", "revisando", "levantando", "probando", "armando_plan")

class SeColgo(Exception):
    """Nos pasamos del tope de tiempo. Se corta y se avisa, no se deja colgado."""

def proximo():
    r = SESION.get(REST, headers=CAB, timeout=20, params={
        "estado": "eq.en_cola", "order": "creado.asc", "limit": "1", "select": "*"})
    r.raise_for_status()
    filas = r.json()
    return filas[0] if filas else None

def reclamar(run_id):
    """Agarra la fila para nosotros, de forma atomica. El filtro pide que TODAVIA este
    en cola: si otro worker la agarro primero, no matchea nada y devolvemos False.
    Sin esto, dos workers corriendo a la vez hacen el mismo analisis dos veces y se
    paga el modelo dos veces."""
    r = SESION.patch(REST, headers={**CAB, "Prefer": "return=representation"},
                     params={"id": f"eq.{run_id}", "estado": "eq.en_cola", "select": "id"},
                       json={"estado": "bajando", "paso": "Empezando"}, timeout=20)
    r.raise_for_status()
    return bool(r.json())

def escribir(run_id, **campos):
    """Escribe y VERIFICA que haya escrito. PostgREST contesta 204 aunque no toque
    ninguna fila (por ejemplo si una politica lo bloquea), asi que pedirle que devuelva
    el id es la unica forma de saber que paso algo de verdad. Este proyecto no puede
    tener fallas silenciosas: son las que le marcamos a los demas."""
    r = SESION.patch(REST, headers={**CAB, "Prefer": "return=representation"},
                     params={"id": f"eq.{run_id}", "select": "id"},
                       json=campos, timeout=20)
    r.raise_for_status()
    filas = r.json()
    if not filas:
        raise RuntimeError(
            f"el PATCH sobre {run_id} no toco ninguna fila: o no existe, o una politica "
            f"de la base lo bloqueo. Campos: {list(campos)}")
    return filas

def correr(run):
    run_id = run["id"]
    repo = run["repo_url"]
    app_url = run.get("app_url") or ""
    print(f"\n  [{run_id[:8]}] {repo}")

    arranco = time.monotonic()
    import costo as libro_costos
    gasto_antes = libro_costos.gastado()

    def queda():
        """Cuantos segundos nos quedan antes del tope total."""
        return TOPE_TOTAL - (time.monotonic() - arranco)

    def controlar(donde):
        if queda() <= 0:
            raise SeColgo(f"pasamos los {TOPE_TOTAL}s en: {donde}")

    # Esto tarda dos o tres minutos. Si no avisamos seguido, la persona mira una rueda
    # girando y no sabe si se colgo. Cada aviso es un PATCH chiquito al campo paso.
    def avisar(texto):
        print(f"    · {texto}")
        try:
            escribir(run_id, paso=texto)
        except Exception as e:
            # No tiramos abajo el analisis por no poder avisar, pero que quede dicho:
            # si esto falla seguido, la pantalla se congela y hay que mirarlo.
            print(f"      (no pude avisar: {type(e).__name__}: {e})", file=sys.stderr)

    carpeta = None
    try:
        # 1. bajar
        escribir(run_id, paso=f"Bajando {repo.split('/')[-1]} de GitHub")
        carpeta = caronte.clonar(repo, tope=TOPE_CLONAR)
        avisar("Codigo bajado, empezamos a mirarlo")

        # 2. revisar el codigo
        escribir(run_id, estado="revisando", paso="Revisando los archivos")
        stack, hecha_con = caronte.detectar_stack(carpeta)
        avisar("Buscando claves sueltas y archivos que no deberian estar")
        problemas = caronte.revisar_codigo(carpeta)
        avisar(f"Revisados los archivos: {len(problemas)} cosas hasta aca")
        ejes = {"segura", "compartible"}
        rutas = caronte.rutas_de_la_app(carpeta)

        # 2b. entender que es la app, y volver a juzgar los hallazgos con eso.
        # Sin este paso no hay forma de distinguir una lista de precios abierta (que esta
        # bien) de una tabla con telefonos abierta (que es gravisimo): para el regex son
        # la misma cosa.
        controlar("antes de entender la app")
        avisar("Leyendo tu codigo para entender que es tu app")
        lectura = entender(carpeta, tope=TOPE_MODELO)
        if lectura:
            avisar(f"Entendido: {lectura.get('que_es', 'tu app')}")
        problemas = rejuzgar(problemas, lectura)

        resultado = armar_resultado(run, problemas, [], ejes, stack, hecha_con, lectura)
        # Se escribe ya, sin plan, para que la pantalla 4 muestre los problemas
        # apareciendo en vivo en vez de dejar a la persona mirando una rueda.
        escribir(run_id, resultado=resultado, hecha_con=hecha_con,
                 paso=f"Encontramos {len(problemas)} cosas en el codigo")

        capturas = []
        if app_url:
            # 3 y 4. abrirla de verdad
            escribir(run_id, estado="levantando", paso="Abriendo tu app publicada")
            problemas += caronte.revisar_publicada(app_url)
            escribir(run_id, estado="probando", paso="Probandola en celular y en compu")
            controlar("antes de abrir el navegador")
            pr, capturas = caronte.revisar_navegador(
                app_url, rutas, avisar=avisar,
                tope=min(TOPE_NAVEGADOR, max(30, queda() - 60)))
            if pr or capturas:
                ejes |= {"funciona", "usable"}
            avisar(f"Guardando las {len(capturas)} fotos que sacamos")
            capturas = subir_capturas(run_id, capturas)
            problemas += pr
            resultado = armar_resultado(run, problemas, capturas, ejes, stack, hecha_con, lectura)
            escribir(run_id, resultado=resultado,
                     paso=f"Encontramos {len(resultado['problemas'])} cosas en total")

        # 5. el plan
        escribir(run_id, estado="armando_plan",
                 paso=f"Ordenando {len(resultado['problemas'])} cosas en un plan")
        plan, resumen = armar_plan(resultado, tope=TOPE_MODELO)
        resultado["plan"] = plan
        if resumen:
            resultado["resumen"] = resumen

        # Lo que costo ESTE analisis, guardado donde no se borra.
        resultado["costo_usd"] = round(libro_costos.gastado() - gasto_antes, 6)
        escribir(run_id, estado="listo", paso=None,
                 score_total=resultado["score"]["total"], resultado=resultado)
        print(f"  [{run_id[:8]}] listo en {time.monotonic() - arranco:.0f}s: "
              f"{resultado['score']['total']}/{resultado['score']['sobre']}, "
              f"{len(resultado['problemas'])} problemas, {len(plan)} pasos de plan")

    except Exception as e:
        traceback.print_exc()
        try:
            escribir(run_id, estado="error", paso=explicar_error(e))
        except Exception as e2:
            # Si ni siquiera podemos escribir el error, que se vea en la terminal.
            # Lo peor que puede pasar es que el analisis quede colgado y nadie sepa.
            print(f"  NO PUDE MARCAR EL ERROR en {run_id}: {e2}", file=sys.stderr)
    finally:
        if carpeta and os.path.isdir(carpeta):
            shutil.rmtree(carpeta, ignore_errors=True)

def subir_capturas(run_id, capturas):
    """Las capturas viajan a Supabase Storage y en el JSON queda la URL publica, no la
    ruta del disco. El contrato (docs/contrato.md) pide una URL, y sin esto la web no
    las puede mostrar."""
    subidas = []
    for c in capturas:
        local = c["url"]
        if not os.path.isfile(local):
            continue
        destino = f"{run_id}/{os.path.basename(local)}"
        try:
            with open(local, "rb") as f:
                r = SESION.post(
                    f"{URL}/storage/v1/object/capturas/{destino}",
                    headers={"apikey": ANON, "Authorization": f"Bearer {ANON}",
                             "Content-Type": "image/png", "x-upsert": "true"},
                    data=f.read(), timeout=60)
            r.raise_for_status()
            c = {**c, "url": f"{URL}/storage/v1/object/public/capturas/{destino}"}
        except Exception as e:
            # Que no se caiga el analisis entero por una captura que no subio.
            print(f"  no pude subir {destino}: {e}")
        subidas.append(c)
    return subidas

def explicar_error(e):
    """El error que ve la persona, sin jerga. Nunca un traceback en pantalla."""
    t = f"{type(e).__name__}: {e}"
    if "clone" in t.lower() or "128" in t:
        return ("No pudimos bajar tu codigo. Fijate que el repositorio sea publico "
                "y que el link este bien copiado.")
    if isinstance(e, SeColgo):
        return ("El analisis tardo mas de lo que esperamos y lo cortamos para no dejarte "
                "esperando. Suele pasar cuando la app tarda mucho en abrir. Podes reintentar.")
    if "Timeout" in t or "timed out" in t:
        return "Tu app tardo demasiado en abrir y cortamos el analisis."
    return "Se nos corto el analisis a la mitad. Podes volver a intentarlo."

def armar_resultado(run, problemas, capturas, ejes, stack, hecha_con, lectura=None):
    # sacar duplicados por id, igual que hace caronte.py cuando corre solo
    vistos, unicos = set(), []
    for pr in problemas:
        if pr["id"] in vistos:
            continue
        vistos.add(pr["id"]); unicos.append(pr)
    orden = {"critico": 0, "alto": 1, "medio": 2, "bajo": 3}
    unicos.sort(key=lambda p: orden.get(p["gravedad"], 9))

    # El nombre que puso el modelo le gana al del repo: "Turnos Belgrano" es mejor
    # que "Turnos-Belgrano" sacado de la URL.
    nombre = re.sub(r"[-_]+", " ", run["repo_url"].rstrip("/").split("/")[-1]).title()
    app = {
        "nombre": (lectura or {}).get("nombre") or nombre,
        "repo": run["repo_url"],
        "url": run.get("app_url") or "",
        "hecha_con": hecha_con,
        "stack": stack,
        "analizada_el": caronte.ahora(),
    }
    if lectura:
        # Esto viaja al prompt del plan y al chat: los dos contestan mejor sabiendo
        # que es la app, no solo que le falta.
        app["que_es"] = lectura.get("que_es", "")
        app["quien_la_usa"] = lectura.get("quien_la_usa", "")

    return {
        "version": 1,
        "estado": "listo",
        "app": app,
        "score": caronte.calcular_score(unicos, ejes),
        "problemas": unicos,
        "plan": [],
        "capturas": capturas,
    }

def barrer_colgados():
    """Si el worker se cayo a la mitad, quedan filas en 'bajando' o 'probando' para
    siempre y la persona ve una rueda girando que no termina nunca. Al arrancar las
    buscamos y las marcamos como error, que es la verdad."""
    # Miramos 'actualizado', no 'creado': lo que importa no es hace cuanto lo pidieron,
    # sino hace cuanto que no da senales de vida. Una corrida lenta pero que avanza no
    # se toca, por mas que lleve media hora.
    limite = datetime.now(timezone.utc) - timedelta(seconds=SILENCIO_MUERTO)
    try:
        r = SESION.get(REST, headers=CAB, timeout=20, params={
            "estado": f"in.({','.join(TRABAJANDO)})",
            "actualizado": f"lt.{limite.isoformat()}",
            "select": "id,estado,actualizado"})
        r.raise_for_status()
    except Exception as e:
        print(f"  no pude revisar si quedaron analisis colgados: {e}", file=sys.stderr)
        return

    for f in r.json():
        try:
            escribir(f["id"], estado="error",
                     paso="Este analisis se corto a la mitad y quedo sin terminar. "
                          "Podes volver a intentarlo.")
            print(f"  {f['id'][:8]} llevaba mas de {SILENCIO_MUERTO}s callado en "
                  f"'{f['estado']}' -> marcado como error")
        except Exception as e:
            print(f"  no pude destrabar {f['id'][:8]}: {e}", file=sys.stderr)

def main():
    ap = argparse.ArgumentParser(description="Levanta analisis de la cola y los corre")
    ap.add_argument("--una-vez", action="store_true", help="agarra uno solo y termina")
    ap.add_argument("--vaciar", action="store_true",
                    help="procesa todo lo que haya en cola y termina (para GitHub Actions)")
    ap.add_argument("--minutos", type=float, default=25,
                    help="con --vaciar: cuanto puede durar la tanda, como mucho")
    a = ap.parse_args()

    if not URL or not ANON:
        sys.exit("Faltan NEXT_PUBLIC_SUPABASE_URL y NEXT_PUBLIC_SUPABASE_ANON_KEY "
                 "en caronte-web/.env.local")

    print(f"  Worker escuchando la cola de {URL}")
    barrer_colgados()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  OJO: no hay ANTHROPIC_API_KEY, los analisis van a salir sin plan.")

    # En GitHub Actions el trabajo es: vaciar la cola y apagarse. Sin tope, una tanda con
    # muchos pedidos podria quedarse horas y quemar minutos al pedo.
    vence = time.monotonic() + a.minutos * 60
    hechos = 0

    while True:
        if a.vaciar and time.monotonic() > vence:
            print(f"  me pase de los {a.minutos} minutos, corto aca. Hechos: {hechos}")
            return

        try:
            run = proximo()
        except Exception as e:
            print(f"  no pude leer la cola ({e}), reintento en 5s")
            time.sleep(5); continue

        if run:
            if not reclamar(run["id"]):
                print(f"  [{run['id'][:8]}] se la llevo otro worker, sigo")
                continue
            correr(run)
            hechos += 1
            if a.una_vez:
                return
        else:
            if a.una_vez or a.vaciar:
                print(f"  no queda nada en cola. Analisis hechos en esta tanda: {hechos}")
                return
            time.sleep(2)

if __name__ == "__main__":
    main()
