# DocuClickAI

Utilidad para extraer transferencias de PDFs NCR de Wendy's y convertirlas a
JSON agrupado por tienda, usando un modelo de Ollama local. Publica
eventos `pdf.processed` y `pdf.error` en Redis Pub/Sub por cada tienda
procesada.

La lógica vive en [`helper/`](helper/). La raíz del proyecto solo contiene
`docs/` (PDFs de ejemplo), `helper/` y este README.

---

## 1. Requisitos previos

- **Python 3.10+** con `venv` (módulo estándar).
- **Ollama** corriendo en `http://localhost:11434`.
- **Redis** corriendo en `localhost:6379` (opcional, desactivable con `--no-publish`).
- Modelo por defecto: `qwen2.5:7b`.

```bash
ollama pull qwen2.5:7b
```

> El binario (`helper/dist/docuclickai`) **NO** autoinstala Ollama ni descarga
> el modelo automáticamente sin tu confirmación. Ver [§ 6 Pre-flight](#6-pre-flight).

---

## 2. Activar el modo virtual (venv)

Todo se ejecuta dentro de un entorno virtual. **No instales dependencias en el
Python del sistema.**

### 2.1 Crear el venv (obligatorio la primera vez)

> **Si te aparece `.venv/bin/activate: No such file or directory` al activar,
> es porque todavía no creaste el venv.** Ejecuta primero este paso y vuelve
> a intentar la activación.

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

Cuando termines:

```bash
deactivate
```

> **Tip:** cada vez que vuelvas a trabajar en el proyecto, primero ejecuta
> `source .venv/bin/activate`. El resto del flujo asume que el venv está
> activo.

---

## 3. Uso rápido (modo CLI / desarrollo)

Con el venv activo, desde la raíz del proyecto:

```bash
./helper/docuclickai \
  --pdf "docs/45 -Transferencias Versalles junio 2026.pdf"
```

Salida por defecto: `./output/transfers.json`.
Artefactos intermedios: `./tmp/session_<ts>_<token>/`.

### Flags principales

| Flag | Default | Descripción |
|------|---------|-------------|
| `--pdf` | (requerido) | Ruta al PDF de transferencias |
| `--out` | `<cwd>/output/transfers.json` | Ruta del JSON de salida |
| `--model` | `qwen2.5:7b` | Modelo de Ollama |
| `--host` | `http://localhost:11434` | URL del servidor Ollama |
| `--timeout` | `600` | Timeout por sección en segundos |
| `--max-retries` | `2` | Reintentos si el JSON viene truncado |

### Flags Redis

| Flag | Default | Descripción |
|------|---------|-------------|
| `--no-publish` | (off) | No publicar eventos en Redis (solo guarda JSON) |
| `--redis-host` | `localhost` o env `REDIS_HOST` | Host de Redis |
| `--redis-port` | `6379` o env `REDIS_PORT` | Puerto de Redis |
| `--redis-db` | `0` o env `REDIS_DB` | DB numérica de Redis |
| `--redis-password` | (sin auth) o env `REDIS_PASSWORD` | Password de Redis (si `requirepass` está activo) |

### Flags pre-flight (solo entry point)

| Flag | Descripción |
|------|-------------|
| `--auto-pull-model` | Descargar el modelo automáticamente si falta (sin preguntar) |
| `--skip-preflight` | Saltar chequeos de Ollama/modelo (útil para tests) |

Más detalle en [`helper/README.md`](helper/README.md).

---

## 4. Distribución como binario standalone

Si quieres usar `docuclickai` en otro server **sin instalar Python ni
dependencias**, genera el binario con PyInstaller.

### 4.1 Build (una vez, en tu máquina de desarrollo)

```bash
source .venv/bin/activate
pip install -r helper/requirements-dev.txt   # instala pyinstaller
cd helper && ./build.sh
```

Resultado: `helper/dist/docuclickai` (~11 MB, un solo archivo).

### 4.2 Distribuir a otro server

```bash
# Desde tu máquina local
scp helper/dist/docuclickai user@server:/usr/local/bin/docuclickai
```

En el server destino, el binario es **autocontenido**: trae Python,
`pypdf`, `ollama-client` y `redis-client`. **NO** requiere venv ni pip.

### 4.3 Uso en el server destino

```bash
docuclickai --pdf "ruta/al.pdf"
```

El binario corre pre-flight (ver § 6). Si Ollama/modelo faltan, falla con
instrucciones claras (exit 5/6). NO toca el sistema.

---

## 5. ¿Dónde van los archivos generados?

Los artefactos intermedios (`tmp/`) y el JSON final (`output/`) se crean
**siempre relativos al directorio de trabajo (CWD)** del usuario que ejecuta
el comando, **NO** relativos a la ubicación del binario.

| Cómo ejecutas | CWD típico | `tmp/` queda en |
|---------------|-----------|-----------------|
| `./helper/docuclickai --pdf x.pdf` (desde repo) | `/path/al/proyecto/` | `/path/al/proyecto/tmp/` |
| `cd /otro/lugar && ./dist/docuclickai --pdf x.pdf` | `/otro/lugar/` | `/otro/lugar/tmp/` |
| `docuclickai --pdf x.pdf` (instalado en `/usr/local/bin/`) | tu CWD | `<tu CWD>/tmp/` |

Para forzar una ubicación fija (ej. un directorio compartido), exporta:

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
  └── ollama_15_Wen_San_Miguelito_raw_1788045605112_...__json.json
```

Estos artefactos son:

- Texto crudo del PDF extraído (`pdf_*`)
- Respuesta cruda de Ollama por tienda (`ollama_<tienda>_raw_*`)

Sirven para debug y auditoría. Se borran automáticamente los >24h al
ejecutar de nuevo (lazy cleanup). Para forzar: `rm -rf tmp/*`.

---

## 6. Pre-flight (chequeos antes de procesar)

El entry point (`./helper/docuclickai` o el binario) ejecuta **antes de
cualquier trabajo** dos chequeos fail-fast. **NO** se autoinstala nada.

### 6.1 Ollama

Si Ollama no está disponible, el binario falla con **exit 5** y muestra:

```text
Error preflight: Ollama no detectado en PATH.
Instálalo desde: https://ollama.com/download/linux
  curl -fsSL https://ollama.com/install.sh | sh
Luego verifica con: ollama --version
```

### 6.2 Modelo

Si Ollama está pero el modelo falta, falla con **exit 6**. Comportamiento:

| Contexto | Comportamiento |
|----------|----------------|
| TTY (terminal interactivo) | Pregunta `[y/N]`. Si dices `y`, ejecuta `ollama pull`. |
| No TTY (CI, cron, pipe) | Falla sin descargar. Debes correr `ollama pull qwen2.5:7b` a mano. |
| `--auto-pull-model` | Descarga sin preguntar. |

Para saltarlos (solo para tests): `--skip-preflight`.

### 6.3 Tabla de exit codes

| Code | Causa |
|------|-------|
| 0 | OK |
| 1 | Error inesperado (ej. PDF no existe) |
| 2 | `SchemaError` (JSON no cumple estructura mínima) |
| 3 | `OllamaError` (timeout/conexión durante procesamiento) |
| 4 | `RedisPublishError` (Redis no responde al construir el publisher) |
| 5 | Ollama no detectado (pre-flight) |
| 6 | Modelo no disponible (pre-flight) |

---

## 7. Eventos Redis

Si Redis está disponible (o `--no-publish` no se pasó), el CLI publica:

| Canal | Evento | Cuándo |
|-------|--------|--------|
| `pdf.processed` | `{evento, session_id, archivo, tipo, tienda, ...}` | 1 por tienda (IN o OUT), en cuanto Ollama termina con esa tienda |
| `pdf.processed` | `evento=pdf.processed.summary` | 1 al final del PDF, con totales y contadores |
| `pdf.error` | `{evento, session_id, archivo, error, contexto}` | Solo si falla Ollama/schema/general |

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

## 8. Estructura

```
docuclickai/
├── .venv/                       # Ignorado: entorno virtual local
├── docs/                        # PDFs NCR de ejemplo
├── helper/                      # Código fuente (CLI Python)
│   ├── docuclickai              # Entry point ejecutable
│   ├── main.py                  # Orquestación: extract → Ollama → JSON
│   ├── pdf_extractor.py         # Extracción de texto con pypdf (layout mode)
│   ├── ollama_client.py         # Cliente HTTP a Ollama + parse_json + retry
│   ├── events.py                # Publisher Redis Pub/Sub (pdf.processed / pdf.error)
│   ├── preflight.py             # Chequeos fail-fast de Ollama + modelo
│   ├── tmp_manager.py           # tmp/ con nombres largos y lazy cleanup 24h
│   ├── build.sh                 # Script PyInstaller --onefile
│   ├── requirements.txt         # Runtime: ollama, pypdf, redis
│   └── requirements-dev.txt     # Dev: pyinstaller
├── output/                      # Ignorado: JSON final persistente
├── tmp/                         # Ignorado: artefactos intermedios (auto-purge 24h)
└── README.md                    # Este archivo
```

---

## 9. Solución de problemas

- **`.venv/bin/activate: No such file or directory`** — el venv no existe todavía.
  Créalo con `python3 -m venv .venv` (paso 2.1) y vuelve a intentar.
- **`ModuleNotFoundError: No module named 'pypdf'`** — el venv no está activo o
  no instalaste las dependencias. Repite los pasos 2.2 y 2.3.
- **`OllamaError: ... connection refused`** — asegúrate de que Ollama esté
  corriendo (`ollama serve` o el servicio del sistema) y de haber descargado
  el modelo (`ollama pull qwen2.5:7b`).
- **`docuclickai: command not found`** — el binario no está en `PATH`. Usa
  `./docuclickai` desde su carpeta, cópialo a `/usr/local/bin/`, o agrégalo
  a `PATH`.
- **`Error preflight: Ollama no detectado...`** (exit 5) — instala Ollama
  primero. El binario NO lo hace automáticamente.
- **`Error preflight: Modelo 'qwen2.5:7b' no disponible...`** (exit 6) —
  descarga con `ollama pull qwen2.5:7b` o usa `--auto-pull-model`.
- **`totales.transfer_in_total` o `transfer_out_total` sale `0.00`** — ningún
  `subtotal_tienda` quedó poblado. Revisa los artefactos en `tmp/session_*/`
  (los `ollama_<tienda>_raw_*.json`) y el log del PDF extraído.
- **JSON con `_validation_warning` o `_transfer_total_inferred`** — son
  marcados de auditoría: el modelo tuvo inconsistencias pero el JSON es
  utilizable. Detalle y campos afectados en
  [helper/README.md → Marcadores de auditoría](helper/README.md#marcadores-de-auditoría).
- **Quiero borrar caché de ejecuciones anteriores** —
  `rm -rf tmp/* output/*` (ambos directorios están en `.gitignore`).