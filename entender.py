#!/usr/bin/env python3
"""
Entender que es la app antes de juzgarla (el paso 3a de docs/prompt-plan.md).

Por que existe: el codigo lo escribio un modelo, sin arquitectura fija, asi que no hay
convencion contra la cual validar. Un regex puede ver que una tabla no tiene candado,
pero no puede saber si eso esta bien o esta mal. Una lista de precios abierta esta bien.
Una tabla con los telefonos de las clientas abierta es gravisimo. Es la misma consulta
SQL y son dos veredictos opuestos, y lo unico que los separa es entender que es la app.

Esto lee el repo y decide. Despues rejuzgar() usa esa lectura para corregir la gravedad
y el texto de los hallazgos de base de datos.
"""
import json, os, sys

import caronte

MODELO = "claude-sonnet-5"
TOPE_CARACTERES = 24_000   # tope duro de lo que le mandamos, para que no se dispare el costo

SYSTEM = """Te paso el codigo de una app que alguien hizo escribiendole a una IA.
Tu trabajo es entender QUE ES esa app antes de que la juzguemos.

Lo que mas importa son las tablas de la base. De cada una necesitamos saber que guarda de
verdad y quien tendria que poder tocarla, mirando como la usa la app. No mires si tiene
permisos puestos o no: eso ya lo sabemos. Queremos saber que CORRESPONDERIA.

Pensalo desde el negocio, no desde el codigo:
- Una tabla con nombres, telefonos, mails o mensajes de clientes NO la tiene que poder leer
  cualquiera, aunque la app deje cargar datos sin registrarse.
- Una lista de precios, un catalogo o un menu SI los puede leer cualquiera: esa es la gracia.
  Pero que los pueda MODIFICAR cualquiera casi nunca esta bien.
- Si la app deja hacer algo sin registrarse (sacar un turno, mandar un mensaje), entonces
  crear filas si tiene que poder cualquiera, aunque leerlas no.

Escribi todo en espanol rioplatense y en criollo, como si se lo explicaras al dueno del
negocio. En que_guarda no digas "registros de reservas": deci "el nombre, el telefono y
el mail de las clientas que sacan turno"."""

HERRAMIENTA = {
    "name": "describir_la_app",
    "description": "Devuelve que es la app y que corresponderia con cada tabla.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "nombre": {"type": "string",
                       "description": "Como se llama el negocio o la app, como lo diria su dueno"},
            "que_es": {"type": "string",
                       "description": "Una oracion. Ej: una app de turnos para una peluqueria de barrio"},
            "quien_la_usa": {"type": "string", "description": "Una oracion sobre quien entra y a que"},
            "tablas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nombre": {"type": "string"},
                        "que_guarda": {"type": "string",
                                       "description": "En criollo, nombrando los campos reales"},
                        "tiene_datos_de_personas": {
                            "type": "boolean",
                            "description": "true si guarda nombre, telefono, mail, direccion o mensajes de alguien"},
                        "puede_leerla_cualquiera": {
                            "type": "boolean",
                            "description": "Si esta bien que cualquiera sin registrarse vea TODO el contenido"},
                        "pueden_crear_cualquiera": {
                            "type": "boolean",
                            "description": "Si esta bien que cualquiera sin registrarse agregue filas"},
                        "puede_modificarla_cualquiera": {
                            "type": "boolean",
                            "description": "Si esta bien que cualquiera sin registrarse edite o borre lo que ya esta"},
                    },
                    "additionalProperties": False,
                    "required": ["nombre", "que_guarda", "tiene_datos_de_personas",
                                 "puede_leerla_cualquiera", "pueden_crear_cualquiera",
                                 "puede_modificarla_cualquiera"],
                },
            },
        },
        "required": ["nombre", "que_es", "quien_la_usa", "tablas"],
    },
}

def recolectar(carpeta):
    """Lo que le mandamos al modelo: el arbol, las migraciones enteras (son chicas y son
    lo que importa) y un pedazo de las pantallas. Con tope, para que no se dispare."""
    partes, arbol = [], []
    for p in caronte.archivos(carpeta, exts=(".ts", ".tsx", ".js", ".jsx", ".sql", ".json", ".html")):
        r = caronte.rel(carpeta, p)
        if "package-lock" in r or r.endswith(".d.ts"):
            continue
        arbol.append(r)
    partes.append("ARCHIVOS DEL REPO:\n" + "\n".join(sorted(arbol)[:120]))

    mig = os.path.join(carpeta, "supabase", "migrations")
    if os.path.isdir(mig):
        for p in sorted(caronte.archivos(mig, exts=(".sql",))):
            partes.append(f"--- {caronte.rel(carpeta, p)} ---\n{caronte.leer(p)[:6000]}")

    for p in caronte.archivos(carpeta, exts=(".tsx", ".jsx")):
        r = caronte.rel(carpeta, p)
        if "/dist/" in r:
            continue
        partes.append(f"--- {r} ---\n{caronte.leer(p)[:3500]}")

    for nombre in ("package.json", "index.html"):
        f = os.path.join(carpeta, nombre)
        if os.path.exists(f):
            partes.append(f"--- {nombre} ---\n{caronte.leer(f)[:1500]}")

    return "\n\n".join(partes)[:TOPE_CARACTERES]

def entender(carpeta, api_key=None, tope=90):
    """Devuelve el dict con lo que es la app, o None si no hay clave."""
    import anthropic
    clave = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not clave:
        print("  (no hay ANTHROPIC_API_KEY, no puedo entender la app)", file=sys.stderr)
        return None

    # Con tope: si el modelo no contesta, cortamos en vez de dejar el analisis colgado.
    cliente = anthropic.Anthropic(api_key=clave, timeout=float(tope), max_retries=1)
    r = cliente.messages.create(
        model=MODELO,
        max_tokens=2000,
        system=SYSTEM,
        tools=[HERRAMIENTA],
        tool_choice={"type": "tool", "name": "describir_la_app"},
        messages=[{"role": "user", "content": recolectar(carpeta)}],
    )
    from costo import anotar
    detalle, total = anotar("entender la app", MODELO, r.usage)
    print(f"  costo de entender la app: {detalle['entrada']} ent + {detalle['salida']} sal "
          f"= ${detalle['usd']:.4f}   (total gastado: ${total:.4f})")
    from plan import desenvolver
    return desenvolver(r.content[0].input)

def rejuzgar(problemas, lectura):
    """Aca esta el punto de todo esto. Los hallazgos de base de datos vienen con una
    gravedad puesta por un diccionario a mano. Con lo que el modelo entendio de la app,
    los volvemos a juzgar: una tabla abierta que no deberia leerse es critica y se explica
    con lo que guarda de verdad; una que si puede leerla cualquiera deja de ser un hallazgo."""
    if not lectura:
        return problemas
    por_tabla = {t["nombre"].lower(): t for t in lectura.get("tablas", [])}
    salida = []

    for pr in problemas:
        if not (pr["id"].startswith("db-") and pr["id"].endswith("-abierta")):
            salida.append(pr)
            continue

        tabla = pr["id"][3:-len("-abierta")]
        t = por_tabla.get(tabla)
        if not t:
            salida.append(pr)
            continue

        guarda = t["que_guarda"]
        if not t["puede_leerla_cualquiera"]:
            pr["gravedad"] = "critico"
            pr["titulo"] = f"Cualquiera puede bajarse lo que hay en {tabla}, sin estar logueado"
            pr["por_que_importa"] = (
                f"En {tabla} estan {guarda}. Como la clave publica de tu app viene en el codigo "
                "(y eso es normal), cualquiera que abra tu app se puede bajar esa lista entera, "
                "o borrarla.")
        elif not t["puede_modificarla_cualquiera"]:
            pr["gravedad"] = "alto"
            pr["titulo"] = f"Cualquiera puede editar o borrar {tabla}"
            pr["por_que_importa"] = (
                f"En {tabla} estan {guarda}. Que se lea esta bien, para eso esta. El problema es "
                "que hoy cualquiera tambien puede cambiar esos datos o borrarlos, y no te ibas a "
                "enterar hasta que un cliente te lo diga.")
        else:
            # El modelo dice que esta bien que sea publica de punta a punta. No es un hallazgo.
            print(f"  ({tabla} queda afuera: es publica a proposito segun lo que hace la app)")
            continue
        pr["juzgado_por_la_ia"] = True
        salida.append(pr)

    return salida

if __name__ == "__main__":
    carpeta = sys.argv[1] if len(sys.argv) > 1 else "."
    if "--seco" in sys.argv:
        texto = recolectar(carpeta)
        tokens = len(texto) // 4
        print(f"  se le mandarian {len(texto)} caracteres (~{tokens} tokens)")
        print(f"  costo estimado: ${(tokens * 2 + 500 * 10) / 1_000_000:.4f} por analisis")
        sys.exit()

    from plan import _cargar_env
    _cargar_env()
    print(json.dumps(entender(carpeta), ensure_ascii=False, indent=2))
