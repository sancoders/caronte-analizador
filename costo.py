"""
Cuanto salio cada llamada. Precios de la API de Anthropic, en dolares por millon de tokens.
Si cambian los precios, se cambian aca y en ningun otro lado.
"""
import json, os, time
from pathlib import Path

PRECIOS = {                     # (entrada, salida) por millon de tokens
    "claude-opus-5":   (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

LIBRO = Path(__file__).resolve().parent.parent / "datos" / "gasto.json"

def calcular(modelo, uso):
    """uso es el objeto usage que devuelve la API."""
    entrada, salida = PRECIOS.get(modelo, (0, 0))
    ent = getattr(uso, "input_tokens", 0) or 0
    sal = getattr(uso, "output_tokens", 0) or 0
    cache_w = getattr(uso, "cache_creation_input_tokens", 0) or 0
    cache_r = getattr(uso, "cache_read_input_tokens", 0) or 0
    # escribir en cache cuesta 1.25x la entrada, leer de cache cuesta 0.1x
    usd = (ent * entrada + cache_w * entrada * 1.25 + cache_r * entrada * 0.10
           + sal * salida) / 1_000_000
    return {"modelo": modelo, "entrada": ent, "salida": sal,
            "cache_escrito": cache_w, "cache_leido": cache_r, "usd": round(usd, 6)}

def anotar(para_que, modelo, uso):
    """Guarda la llamada en datos/gasto.json y devuelve el detalle. Asi el gasto queda
    contado de verdad y no estimado."""
    d = calcular(modelo, uso)
    d["para_que"] = para_que
    d["cuando"] = time.strftime("%Y-%m-%d %H:%M:%S")
    libro = {"llamadas": [], "total_usd": 0.0}
    if LIBRO.exists():
        try:
            libro = json.loads(LIBRO.read_text(encoding="utf-8"))
        except Exception:
            pass
    libro["llamadas"].append(d)
    libro["total_usd"] = round(sum(x["usd"] for x in libro["llamadas"]), 6)
    LIBRO.parent.mkdir(parents=True, exist_ok=True)
    LIBRO.write_text(json.dumps(libro, ensure_ascii=False, indent=2), encoding="utf-8")
    return d, libro["total_usd"]

def resumen():
    if not LIBRO.exists():
        return "todavia no se gasto nada"
    libro = json.loads(LIBRO.read_text(encoding="utf-8"))
    lineas = [f"  {x['cuando']}  {x['para_que']:<22} {x['entrada']:>6} ent + {x['salida']:>5} sal = ${x['usd']:.4f}"
              for x in libro["llamadas"]]
    lineas.append(f"  {'':<22}{'':>24}  TOTAL  ${libro['total_usd']:.4f}")
    return "\n".join(lineas)

if __name__ == "__main__":
    print(resumen())
