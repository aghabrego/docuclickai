# DocuClickAI

Utilidad CLI para extraer **transferencias de los PDFs NCR de Wendy's
(Panamá)** y convertirlas a un JSON agrupado por tienda, usando un modelo
de lenguaje (LLM).

**Backends soportados** (auto-detectados por el nombre del modelo):

| Prefijo de ejemplo | Backend |
|---|---|
| `qwen2.5:7b`, `phi4-mini:latest` (formato `nombre:tag`) | **Ollama** local (`http://localhost:11434`) |
| `gpt-4o-mini`, `gpt-4o`, `o1-mini`, `chatgpt-4o-latest` | **OpenAI-compatible** (oficial, Azure, OpenRouter, proxy local) |

Default: `qwen2.5:7b` corriendo en Ollama local.

Si Redis está disponible, publica eventos `pdf.processed` y `pdf.error` en
Pub/Sub por cada tienda procesada (desactivable con `--no-publish`).

```
docs/  (PDFs de ejemplo)  ─┐
                           ├─►  helper/  ─►  output/transfers.json
tmp/   (artefactos auto)  ─┘                       └─► Redis Pub/Sub
```

---

## Tabla de contenidos

1. [Requisitos previos](#1-requisitos-previos)
2. [Configuración inicial (venv)](#2-configuración-inicial-venv)
3. [Variables de entorno (.env)](#3-variables-de-entorno-env)
4. [Uso rápido (modo CLI / desarrollo)](#4-uso-rápido-modo-cli--desarrollo)
5. [Backends LLM: Ollama vs OpenAI](#5-backends-llm-ollama-vs-openai)
6. [Distribución como binario standalone](#6-distribución-como-binario-standalone)
7. [¿Dónde van los archivos generados?](#7-dónde-van-los-archivos-generados)
8. [Pre-flight (chequeos antes de procesar)](#8-pre-flight-chequeos-antes-de-procesar)
9. [Eventos Redis](#9-eventos-redis)
10. [Estructura del repositorio](#10-estructura-del-repositorio)
11. [Solución de problemas](#11-solución-de-problemas)

---

## 1. Requisitos previos

- **Python 3.10+** con `venv` (módulo estándar).
- **Ollama** corriendo en `http://localhost:11434` — **solo si vas a usar
  modelos Ollama** (formato `nombre:tag`).
- **Redis** corriendo en `localhost:6379` — opcional; desactivable con
  `--no-publish`.
- **API key de OpenAI** — solo si vas a usar `--model gpt-*`, `o1-*`, etc.

```bash
# Solo si usas Ollama:
ollama pull qwen2.5:7b
```

> El binario (`helper/dist/docuclickai`) **NO** autoinstala Ollama ni
> descarga el modelo sin tu confirmación. Ver
> [§ 8 Pre-flight](#8-pre-flight-chequeos-antes-de-procesar).

---

## 2. Configuración inicial (venv)

Todo se ejecuta dentro de un entorno virtual. **No instales dependencias en
el Python del sistema.**

### 2.1 Crear el venv (obligatorio la primera vez)

> **Si te aparece `.venv/bin/activate: No such file or directory` al
> activar, es porque todavía no creaste el venv.** Ejecuta primero este
> paso y vuelve a intentar la activación.

Desde la raíz del proyecto:

```bash
python3 -m venv .venv
```

> El directorio `.venv/` está en `.gitignore`, así que no contamina el repo.

### 2.2 Activar el venv

| Shell         | Comando              |
|---------------|----------------------|
| bash / zsh    | `source .venv/bin/activate` |
| fish          | `source .venv/bin/activate.fish` |
| csh / tcsh    | `source .venv/bin/activate.csh` |
| PowerShell    | `.venv\Scripts\Activate.ps1` |
| cmd (Windows) | `.venv\Scripts\activate.bat` |

Cuando esté activo, el prompt mostrará `(.venv)` al inicio:

```text
(.venv) user@host:~/code/docuclickai$
```

### 2.3 Instalar dependencias (dentro del venv)

```bash
pip install --upgrade pip
pip install -r helper/requirements.txt
```

### 2.4 Desactivar el venv

```bash
deactivate
```

> **Tip:** cada vez que vuelvas a trabajar en el proyecto, primero ejecuta
> `source .venv/bin/activate`. El resto del flujo asume que el venv está
> activo.

---

## 3. Variables de entorno (.env)

Todos los módulos (`llm_client`, `openai_client`, `preflight`,
`dotenv_loader`) **auto-cargan un `.env`** al importarse, si existe. El
envío por CLI tiene prioridad sobre el `.env`.

Copia el archivo de ejemplo y rellena solo lo que necesites:

```bash
cp .env.example .env
```

Variables reconocidas:

| Variable | Default | Descripción |
|---|---|---|
| `OPENAI_API_KEY` | _(vacío)_ | API key de OpenAI. Requerida para `--model gpt-*`. Obtener en [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Endpoint OpenAI-compatible. Útil para Azure OpenAI, OpenRouter o un proxy local |
| `REDIS_HOST` | `localhost` | Host de Redis |
| `REDIS_PORT` | `6379` | Puerto de Redis |
| `REDIS_DB` | `0` | DB numérica de Redis |
| `REDIS_PASSWORD` | _(sin auth)_ | Password de Redis (si `requirepass` está activo) |
| `OLLAMA_HOST` | `http://localhost:11434` | URL del servidor Ollama |

> Las variables presentes en el shell (`export OPENAI_API_KEY=...`)
> tienen prioridad sobre `.env`. Para sobrescribir `.env` siempre, pasa
> `override=True` programáticamente o exporta la variable antes.

---

## 4. Uso rápido (modo CLI / desarrollo)

Con el venv activo, desde la raíz del proyecto:

```bash
./helper/docuclickai \
  --pdf "docs/45 -Transferencias Versalles junio 2026.pdf"
```

Salida por defecto: `./output/transfers.json`.
Artefactos intermedios: `./tmp/session_<ts>_<token>/`.

### Flags principales

| Flag | Default | Descripción |
|---|---|---|
| `--pdf` | _(requerido)_ | Ruta al PDF de transferencias |
| `--out` | `<cwd>/output/transfers.json` | Ruta del JSON de salida |
| `--model` | `qwen2.5:7b` | Modelo LLM (ver [§ 5](#5-backends-llm-ollama-vs-openai)) |
| `--host` | `http://localhost:11434` | URL del servidor Ollama |
| `--timeout` | `600` | Timeout por sección en segundos |
| `--max-retries` | `2` | Reintentos si el JSON viene truncado |

### Flags de OpenAI (solo si `--model` empieza con `gpt-`, `o1-`, etc.)

| Flag | Default | Descripción |
|---|---|---|
| `--openai-api-key` | env `OPENAI_API_KEY` o `.env` | API key de OpenAI |
| `--openai-base-url` | `https://api.openai.com/v1` | Endpoint compatible (Azure, OpenRouter, proxy) |

### Flags de Redis

| Flag | Default | Descripción |
|---|---|---|
| `--no-publish` | _(off)_ | Publish events en Redis Pub/Sub |
| `--redis-host` | `localhost` o env `REDIS_HOST` | Host de Redis |
| `--redis-port` | `6379` o env `REDIS_PORT` | Puerto de Redis |
| `--redis-db` | `0` o env `REDIS_DB` | DB numérica de Redis |
| `--redis-password` | _(sin auth)_ o env `REDIS_PASSWORD` | Password de Redis |

### Flags de pre-flight (solo entry point)

| Flag | Descripción |
|---|---|
| `--auto-pull-model` | Descargar el modelo Ollama automáticamente si falta (sin preguntar) |
| `--skip-preflight` | Saltar chequeos de Ollama/modelo (útil para tests) |

Más detalle en [`helper/README.md`](helper/README.md).

---

## 5. Backends LLM: Ollama vs OpenAI

El módulo [`helper/llm_client.py`](helper/llm_client.py) decide qué backend
usar según el nombre del modelo. Las reglas (en orden) son:

| Regla | Señal | Backend |
|---|---|---|
| 1 | El modelo empieza con `gpt-`, `o1-`, `o3-`, `o4-`, `chatgpt-`, `openai/` | **OpenAI** |
| 2 | El modelo tiene formato `nombre:tag` (p. ej. `qwen2.5:7b`) | **Ollama** |
| 3 | Sin pistas claras: si Ollama responde → Ollama; si no, OpenAI si hay key | Fallback |
| 4 | Nada aplica | `LLMBackendError` (exit 3) |

### Ejemplos

```bash
# Ollama (default)
./helper/docuclickai --pdf docs/x.pdf --model qwen2.5:7b

# OpenAI oficial
./helper/docuclickai --pdf docs/x.pdf --model gpt-4o-mini \
    --openai-api-key sk-...

# Azure OpenAI
./helper/docuclickai --pdf docs/x.pdf --model gpt-4o \
    --openai-base-url "https://mi-recurso.openai.azure.com/openai/deployments/mi-deploy/v1" \
    --openai-api-key sk-...

# OpenRouter
./helper/docuclickai --pdf docs/x.pdf --model "openai/gpt-4o-mini" \
    --openai-base-url "https://openrouter.ai/api/v1" \
    --openai-api-key sk-or-...

# Proxy local (LM Studio, Ollama con OpenAI-compat, etc.)
./helper/docuclickai --pdf docs/x.pdf --model gpt-3.5-turbo \
    --openai-base-url "http://localhost:1234/v1" \
    --openai-api-key "lm-studio"
```

> Las keys de OpenAI también se pueden definir en `.env` (ver
> [§ 3](#3-variables-de-entorno-env)).

---

## 6. Distribución como binario standalone

Si quieres usar `docuclickai` en otro server **sin instalar Python ni
dependencias**, genera el binario con PyInstaller.

### 6.1 Build local (en tu máquina de desarrollo)

```bash
source .venv/bin/activate
pip install -r helper/requirements-dev.txt   # instala pyinstaller
cd helper && ./build.sh
```

Resultado: `helper/dist/docuclickai` (~50 MB, un solo archivo).

### 6.2 Build portable con Docker (GLIBC 2.31+)

Para máxima portabilidad (cualquier Debian 11+ / Ubuntu 20.04+):

```bash
cd helper && ./build-docker.sh
```

Usa una imagen Docker basada en Debian 11 (GLIBC 2.31) para que el
binario corra en servers antiguos sin requerir GLIBC 2.36+.

### 6.3 Distribuir a otro server

```bash
scp helper/dist/docuclickai user@server:/usr/local/bin/docuclickai
```

En el server destino, el binario es **autocontenido**: trae Python,
`pypdf`, `ollama-client`, `openai-client`, `llm-client`, `redis-client`
y `dotenv_loader`. **NO** requiere venv ni pip.

### 6.4 Uso en el server destino

```bash
docuclickai --pdf "ruta/al.pdf"
docuclickai --pdf "ruta/al.pdf" --model gpt-4o-mini   # vía OpenAI
docuclickai --pdf "ruta/al.pdf" --no-publish          # sin Redis
```

El binario corre pre-flight (ver [§ 8](#8-pre-flight-chequeos-antes-de-procesar)).
Si Ollama/modelo faltan, falla con instrucciones claras (exit 5/6/7).
**NO** toca el sistema.

---

## 7. ¿Dónde van los archivos generados?

Los artefactos intermedios (`tmp/`) y el JSON final (`output/`) se crean
**siempre relativos al directorio de trabajo (CWD)** del usuario que
ejecuta el comando, **NO** relativos a la ubicación del binario.

| Cómo ejecutas | CWD típico | `tmp/` queda en |
|---|---|---|
| `./helper/docuclickai --pdf x.pdf` (desde repo) | `/path/al/proyecto/` | `/path/al/proyecto/tmp/` |
| `cd /otro/lugar && ./dist/docuclickai --pdf x.pdf` | `/otro/lugar/` | `/otro/lugar/tmp/` |
| `docuclickai --pdf x.pdf` (instalado en `/usr/local/bin/`) | tu CWD | `<tu CWD>/tmp/` |

Para forzar una ubicación fija (ej. un directorio compartido):

```bash
export DOCUCLICKAI_HOME=/var/lib/docuclickai
docuclickai --pdf x.pdf
# → tmp/ y output/ quedan en /var/lib/docuclickai/
```

Estructura típica de `tmp/`:

```
tmp/session_20260829-194802_285561361d5e/
  ├── pdf_full_1788045249869_346863c01...__txt.txt
  ├── pdf_header_1788045249869_3e22c8e10...__txt.txt
  ├── pdf_transfer_in_1788045249869_869e1ab5...__txt.txt
  ├── pdf_transfer_out_1788045249869_30d1730b...__txt.txt
  ├── ollama_05_Wen_Metromall_raw_1788045381002_...__json.json
  └── openai_15_Wen_San_Miguelito_raw_1788045605112_...__json.json
```

Los prefijos cambian según el backend:

- `ollama_<tienda>_*` — modelo fue Ollama
- `openai_<tienda>_*` — modelo fue OpenAI-compatible

Estos artefactos son:

- Texto crudo del PDF extraído (`pdf_*`)
- Respuesta cruda del LLM por tienda (`<backend>_<tienda>_raw_*`)

Sirven para debug y auditoría. Se borran automáticamente los >24h al
ejecutar de nuevo (lazy cleanup). Para forzar: `rm -rf tmp/*`.

---

## 8. Pre-flight (chequeos antes de procesar)

El entry point (`./helper/docuclickai` o el binario) ejecuta **antes de
cualquier trabajo** chequeos fail-fast. **NO** se autoinstala nada.

### 8.1 Backend Ollama

Si Ollama no está disponible, el binario falla con **exit 5** y muestra:

```text
Error preflight: Ollama no detectado en PATH.
Instálalo desde: https://ollama.com/download/linux
  curl -fsSL https://ollama.com/install.sh | sh
Luego verifica con: ollama --version
```

### 8.2 Modelo Ollama

Si Ollama está pero el modelo falta, falla con **exit 6**:

| Contexto | Comportamiento |
|---|---|
| TTY (terminal interactivo) | Pregunta `[y/N]`. Si dices `y`, ejecuta `ollama pull`. |
| No TTY (CI, cron, pipe) | Falla sin descargar. Debes correr `ollama pull` a mano. |
| `--auto-pull-model` | Descarga sin preguntar. |

### 8.3 Backend OpenAI

Si el modelo es de la familia OpenAI (`gpt-*`, `o1-*`, ...), el pre-flight
valida contra el endpoint. Falla con **exit 7** si:

- Falta API key (ni en flag ni en `OPENAI_API_KEY` / `.env`)
- La key es inválida o el endpoint no responde
- El modelo pedido no aparece en `/v1/models`

### 8.4 Tabla de exit codes

| Code | Causa |
|---|---|
| 0 | OK |
| 1 | Error inesperado (ej. PDF no existe) |
| 2 | `SchemaError` (JSON no cumple estructura mínima) |
| 3 | `OllamaError` / `OpenAIError` / `LLMBackendError` (durante procesamiento) |
| 4 | `RedisPublishError` (Redis no responde al construir el publisher) |
| 5 | Ollama no detectado (pre-flight) |
| 6 | Modelo Ollama no disponible (pre-flight) |
| 7 | OpenAI/API key (pre-flight) |

Para saltarlos (solo para tests): `--skip-preflight`.

---

## 9. Eventos Redis

Si Redis está disponible (y no se pasó `--no-publish`), el CLI publica:

| Canal | Evento | Cuándo |
|---|---|---|
| `pdf.processed` | `{evento, session_id, archivo, tipo, tienda, ...}` | 1 por tienda (IN o OUT), en cuanto el LLM termina con esa tienda |
| `pdf.processed` | `evento=pdf.processed.summary` | 1 al final del PDF, con totales y contadores |
| `pdf.error` | `{evento, session_id, archivo, error, contexto}` | Solo si falla LLM/schema/general |

Todos los eventos llevan `session_id` (ej. `session_20260829-194802_285561361d5e`)
para que el consumidor pueda correlacionar todos los mensajes del mismo run.

### Consumir eventos en tiempo real

```bash
# Suscripción simple (verás el JSON crudo)
redis-cli SUBSCRIBE pdf.processed pdf.error

# Versión formateada
source .venv/bin/activate
python -c "
import redis, json
r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True)
ps = r.pubsub(ignore_subscribe_messages=True)
ps.subscribe('pdf.processed', 'pdf.error')
for m in ps.listen():
    print(json.dumps(json.loads(m['data']), indent=2, ensure_ascii=False))
    print('---')
"
```

---

## 10. Estructura del repositorio

```
docuclickai/
├── .env.example                # Plantilla de variables de entorno
├── .venv/                       # Ignorado: entorno virtual local
├── docs/                        # PDFs NCR de ejemplo (Versalles, AN, CR, ME, SAN, VR, VES, BR)
├── helper/                      # Código fuente (CLI Python)
│   ├── docuclickai              # Entry point ejecutable
│   ├── docuclickai.spec         # Spec de PyInstaller
│   ├── main.py                  # Orquestación: extract → LLM → JSON
│   ├── llm_client.py            # Dispatcher Ollama ↔ OpenAI
│   ├── ollama_client.py         # Cliente HTTP a Ollama + parse_json + retry
│   ├── openai_client.py         # Cliente OpenAI-compatible (oficial, Azure, OpenRouter)
│   ├── pdf_extractor.py         # Extracción de texto con pypdf (layout mode)
│   ├── events.py                # Publisher Redis Pub/Sub (pdf.processed / pdf.error)
│   ├── preflight.py             # Chequeos fail-fast de Ollama / modelo / OpenAI
│   ├── tmp_manager.py           # tmp/ con nombres largos y lazy cleanup 24h
│   ├── dotenv_loader.py         # Loader .env minimalista (sin dependencias)
│   ├── build.sh                 # Script PyInstaller --onefile (local)
│   ├── build-docker.sh          # Wrapper Docker para build portable (GLIBC 2.31)
│   ├── Dockerfile.build         # Imagen base Debian 11 para el build
│   ├── requirements.txt         # Runtime: pypdf, ollama, redis
│   ├── requirements-dev.txt      # Dev: pyinstaller
│   └── README.md                # Documentación detallada del helper
├── output/                      # Ignorado: JSON final persistente
├── tmp/                         # Ignorado: artefactos intermedios (auto-purge 24h)
└── README.md                    # Este archivo
```

---

## 11. Solución de problemas

- **`.venv/bin/activate: No such file or directory`** — el venv no existe
  todavía. Créalo con `python3 -m venv .venv` (paso 2.1) y vuelve a
  intentar.
- **`ModuleNotFoundError: No module named 'pypdf'`** — el venv no está
  activo o no instalaste las dependencias. Repite los pasos 2.2 y 2.3.
- **`OllamaError: ... connection refused`** — asegúrate de que Ollama
  esté corriendo (`ollama serve` o el servicio del sistema) y de haber
  descargado el modelo (`ollama pull qwen2.5:7b`).
- **`OpenAIError: Falta API key de OpenAI`** — exporta `OPENAI_API_KEY`
  en el entorno o pásala con `--openai-api-key sk-...`.
- **`OpenAIError: HTTP 401 ...`** — la key es inválida o no tiene acceso
  al modelo. Verifica en [platform.openai.com/api-keys](https://platform.openai.com/api-keys).
- **`docuclickai: command not found`** — el binario no está en `PATH`.
  Usa `./docuclickai` desde su carpeta, cópialo a `/usr/local/bin/`, o
  agrégalo a `PATH`.
- **`Error preflight: Ollama no detectado...`** (exit 5) — instala Ollama
  primero. El binario NO lo hace automáticamente.
- **`Error preflight: Modelo 'qwen2.5:7b' no disponible...`** (exit 6) —
  descarga con `ollama pull qwen2.5:7b` o usa `--auto-pull-model`.
- **`Error preflight: Modelo 'gpt-4o-mini' no está disponible...`** (exit
  7) — el modelo no está en tu catálogo de OpenAI. Prueba con otro nombre
  o revisa el scope de tu API key.
- **`LLMBackendError: No se pudo resolver un backend`** (exit 3) — ni
  Ollama responde en `localhost:11434` ni hay `OPENAI_API_KEY` válida.
- **`totales.transfer_in_total` o `transfer_out_total` sale `0.00`** —
  ningún `subtotal_tienda` quedó poblado. Revisa los artefactos en
  `tmp/session_*/` (los `ollama_<tienda>_raw_*.json` o
  `openai_<tienda>_raw_*.json`) y el log del PDF extraído.
- **JSON con `_validation_warning` o `_transfer_total_inferred`** — son
  marcados de auditoría: el modelo tuvo inconsistencias pero el JSON es
  utilizable. Detalle y campos afectados en
  [`helper/README.md`](helper/README.md#marcadores-de-auditoría).
- **Quiero borrar caché de ejecuciones anteriores** —
  `rm -rf tmp/* output/*` (ambos directorios están en `.gitignore`).
- **GLIBC `version 'GLIBC_2.34' not found` al correr el binario** —
  rebuildea con `./build-docker.sh` (target Debian 11 / GLIBC 2.31).