#!/usr/bin/env python3
"""
El paso que convierte la lista de problemas en un plan (tarea 8).

Esta escrito tal cual esta especificado en docs/prompt-plan.md. El tool_choice forzado
es lo que garantiza que siempre vuelva JSON valido y nunca texto suelto.

    python plan.py resultado.json
"""
import json, os, sys
from pathlib import Path

MODELO = "claude-sonnet-5"

def _cargar_env():
    """Las claves viven en caronte-web/.env.local, un solo lugar. worker.py hace lo mismo;
    esto es para cuando se corre plan.py suelto."""
    archivo = Path(__file__).resolve().parent.parent / "caronte-web" / ".env.local"
    if not archivo.exists():
        return
    for linea in archivo.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            k, v = linea.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

_cargar_env()

SYSTEM = """Sos el que arma el plan de lanzamiento de Caronte.

A vos te llega una app que alguien hizo con una herramienta de IA (Lovable, Bolt,
v0, Claude Code) y la lista de problemas que encontro el analizador. Tu trabajo es
convertir esa lista en un plan ordenado para que esa persona pueda publicar su app
y que la use gente.

Quien te lee: alguien que NO es programador. Armo su app escribiendole a una IA.
No sabe que es RLS, ni una variable de entorno, ni un DNS. Escribile como le
explicarias a un amigo que tiene una peluqueria, no a un colega.

Reglas:

1. Usa SOLO los problemas que te paso. No inventes ni supongas otros. Si algo no
   esta en la lista, no existe.

2. Ordena por lo que realmente traba. Primero lo que le puede hacer dano hoy
   (datos de sus clientes a la vista, la app no abre). Despues lo que hace que la
   gente se vaya. Al final lo cosmetico. Un paso que depende de otro va despues.

3. Un paso puede juntar varios problemas si se arreglan de una sentada. No hagas
   un paso por cada problema.

4. Marca bloqueante: true solo si publicar sin resolverlo seria irresponsable.
   Como mucho dos o tres pasos bloqueantes.

5. En por_que escribi UNA oracion, en criollo, que diga que le pasa a ESTA app
   si no lo hace. Nada de teoria. Mal: "las politicas RLS protegen los datos".
   Bien: "hoy cualquiera que abra tu app puede bajarse la lista con los telefonos
   de tus clientas".

6. En pasos pone acciones concretas para ESTA app, con los nombres reales de sus
   tablas, archivos y variables, que vienen en los problemas. Entre 2 y 5 pasos.
   Nada de "configura la seguridad".

7. lo_hace_caronte: true solo si es un cambio de codigo que se puede escribir y
   verificar solo. Pone false para lo que necesita que la persona entre a una
   cuenta, pague algo, o decida (comprar un dominio, crear una cuenta, elegir un
   nombre).

8. Cerra siempre con un paso de publicar y uno de verificar, aunque no haya ningun
   problema que lo pida. El plan termina con la app en internet, no con la lista
   de arreglos.

9. Escribi en espanol rioplatense, de vos. Sin markdown adentro de los textos."""

HERRAMIENTA = {
    "name": "devolver_plan",
    "description": "Devuelve el plan de lanzamiento ordenado.",
    # strict obliga al modelo a respetar el esquema al pie de la letra. Sin esto nos
    # devolvio una vez el objeto entero como texto adentro del campo plan.
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "resumen": {
                "type": "string",
                "description": "Una oracion sobre en que estado esta la app y que falta para publicarla."
            },
            "plan": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "orden":        {"type": "integer"},
                        "titulo":       {"type": "string", "description": "Corto y en infinitivo. Ej: Cerrar el acceso a la base de datos"},
                        "bloqueante":   {"type": "boolean"},
                        "estado":       {"type": "string", "enum": ["pendiente"]},
                        "lo_hace_caronte": {"type": "boolean"},
                        "por_que":      {"type": "string", "description": "Una oracion, en criollo, sobre que le pasa a esta app si no lo hace"},
                        "pasos":        {"type": "array", "items": {"type": "string"},
                                         "description": "Entre 2 y 5 acciones concretas para esta app"},
                        "problemas":    {"type": "array", "items": {"type": "string"},
                                         "description": "Los id de los problemas que cierra este paso"}
                    },
                    "additionalProperties": False,
                    "required": ["orden","titulo","bloqueante","estado","lo_hace_caronte","por_que","pasos","problemas"]
                }
            }
        },
        "required": ["resumen","plan"]
    }
}

def desenvolver(salida):
    """A veces el modelo devuelve el objeto entero como un string adentro de un campo.
    Con strict no deberia pasar mas, pero si pasa preferimos entenderlo antes que romper."""
    if isinstance(salida, str):
        salida = json.loads(salida)
    plan = salida.get("plan")
    if isinstance(plan, str):
        adentro = json.loads(plan)
        salida = adentro if isinstance(adentro, dict) and "plan" in adentro else {**salida, "plan": adentro}
    return salida

def armar_plan(resultado, api_key=None, tope=90):
    """Devuelve (plan, resumen). Si no hay clave, devuelve ([], "") y no rompe nada:
    el resto del analisis vale igual sin el plan.
    Cada llamada queda anotada en datos/gasto.json con lo que costo de verdad."""
    import anthropic
    clave = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not clave:
        print("  (no hay ANTHROPIC_API_KEY, salteo el plan)", file=sys.stderr)
        return [], ""

    # Con tope: si el modelo no contesta, cortamos en vez de dejar el analisis colgado.
    cliente = anthropic.Anthropic(api_key=clave, timeout=float(tope), max_retries=1)
    r = cliente.messages.create(
        model=MODELO,
        max_tokens=4000,
        system=SYSTEM,
        tools=[HERRAMIENTA],
        tool_choice={"type": "tool", "name": "devolver_plan"},
        messages=[{"role": "user", "content": json.dumps({
            "app": resultado["app"],
            "problemas": resultado["problemas"],
        }, ensure_ascii=False)}],
    )
    from costo import anotar
    detalle, total = anotar("plan", MODELO, r.usage)
    print(f"  costo del plan: {detalle['entrada']} tokens de entrada + {detalle['salida']} "
          f"de salida = ${detalle['usd']:.4f}   (gastado en total: ${total:.4f})")

    salida = desenvolver(r.content[0].input)
    return salida["plan"], salida.get("resumen", "")

def chequear(resultado, plan):
    """Los 7 chequeos del final de docs/prompt-plan.md. Devuelve la lista de los que fallan."""
    ids = {p["id"] for p in resultado["problemas"]}
    criticos = [p["id"] for p in resultado["problemas"] if p["gravedad"] == "critico"]
    fallas = []

    inventados = {i for paso in plan for i in paso.get("problemas", [])} - ids
    if inventados:
        fallas.append(f"inventó problemas que no existen: {sorted(inventados)}")

    if criticos:
        # La regla de verdad no es "en los dos primeros" (eso salio de que el ejemplo
        # tenia dos criticos): es que ningun paso que no resuelva algo critico se meta
        # antes de uno que si. Lo urgente va primero y sin nada colado en el medio.
        cierra_critico = [any(i in criticos for i in paso.get("problemas", [])) for paso in plan]
        cubiertos = {i for paso in plan for i in paso.get("problemas", []) if i in criticos}
        faltan = [c for c in criticos if c not in cubiertos]
        if faltan:
            fallas.append(f"hay criticos que no los resuelve ningun paso: {faltan}")
        ultimo = max([i for i, x in enumerate(cierra_critico) if x], default=-1)
        colados = [plan[i]["titulo"] for i in range(ultimo) if not cierra_critico[i]]
        if colados:
            fallas.append(f"hay pasos que no son urgentes metidos antes de los criticos: {colados}")

    bloqueantes = sum(1 for p in plan if p.get("bloqueante"))
    if bloqueantes > 3:
        fallas.append(f"hay {bloqueantes} pasos bloqueantes, el maximo es 3")

    if plan:
        ultimo = (plan[-1]["titulo"] + " " + " ".join(plan[-1]["pasos"])).lower()
        if not any(x in ultimo for x in ("public", "verific", "subir", "revisar", "probar")):
            fallas.append(f"el ultimo paso no es publicar ni verificar: {plan[-1]['titulo']}")
    else:
        fallas.append("el plan vino vacio")

    return fallas

if __name__ == "__main__":
    archivo = sys.argv[1] if len(sys.argv) > 1 else "resultado.json"
    with open(archivo, encoding="utf-8") as f:
        resultado = json.load(f)

    plan, resumen = armar_plan(resultado)
    resultado["plan"] = plan
    if resumen:
        resultado["resumen"] = resumen

    with open(archivo, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)

    print(f"\n  {len(plan)} pasos. Resumen: {resumen}\n")
    for p in plan:
        marca = " [BLOQUEANTE]" if p["bloqueante"] else ""
        quien = "Caronte" if p["lo_hace_caronte"] else "la persona"
        print(f"  {p['orden']}. {p['titulo']}{marca}  ({quien})")
        print(f"     {p['por_que']}")

    fallas = chequear(resultado, plan)
    print("\n  Chequeos de docs/prompt-plan.md:")
    if not fallas:
        print("    todos pasaron")
    for f_ in fallas:
        print(f"    FALLA: {f_}")
