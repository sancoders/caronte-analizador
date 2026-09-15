# El analizador

Baja un repo, lo revisa, abre la app en celular y en compu, le pide a Claude que entienda
qué es y arma el plan. Escribe todo en la tabla `runs` de Supabase.

La web **no** hace nada de esto: solo anota el pedido. Este programa es el que trabaja.

## Correrlo en tu máquina

```bash
pip install -r requirements.txt
playwright install chromium
python worker.py
```

Las claves salen de `../caronte-web/.env.local`. Hacen falta `NEXT_PUBLIC_SUPABASE_URL`,
`NEXT_PUBLIC_SUPABASE_ANON_KEY` y `ANTHROPIC_API_KEY`.

## Correrlo en un servidor, que es como tiene que ser

Mientras esto corra en la máquina de alguien, Caronte solo funciona cuando esa persona
tiene la terminal abierta. El `Dockerfile` está listo para cualquier lado que acepte
contenedores. Las tres variables de arriba se cargan como variables de entorno.

| Dónde | Cómo |
|---|---|
| Railway | conectás el repo, detecta el Dockerfile solo |
| Fly.io | `fly launch --no-deploy` y después `fly deploy`, el `fly.toml` ya está |
| Render | servicio del tipo *Background Worker*, apuntando al Dockerfile |
| AWS | es la tarea de ECS Fargate del doc de arquitectura |

No corre como root a propósito: abre código de terceros que no controlamos.

## Los topes de tiempo

Están todos juntos arriba de `worker.py`. Una corrida que avanza no se corta nunca; lo
que se corta es lo que se queda callado. Detalle en `../docs/estado.md`.

## Lo que cuesta

Cada análisis son dos llamadas a Claude, unos US$0,05. Queda anotado en `../datos/gasto.json`
con los tokens reales de cada una.
