# DocuClickAI

Utilidad para extraer transferencias de PDFs NCR de Wendy's y convertirlas a
JSON agrupado por tienda, usando un modelo de Ollama local.

La lógica vive en [`helper/`](helper/). La raíz del proyecto solo contiene
`docs/` (PDFs de ejemplo), `helper/` y este README.

---

## 1. Requisitos previos

- **Python 3.10+** con `venv` (módulo estándar).
- **Ollama** corriendo en `http://localhost:11434`.
- Modelo por defecto: `phi4-mini:latest`.

```bash
ollama pull phi4-mini:latest
```

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

## 3. Uso rápido

Con el venv activo, desde la raíz del proyecto:

```bash
./helper/docuclickai \
  --pdf "docs/45 -Transferencias Versalles junio 2026.pdf"
```

Salida por defecto: `./output/transfers.json`.

Flags principales:

| Flag      | Default                                  | Descripción                |
|-----------|------------------------------------------|----------------------------|
| `--pdf`   | (requerido)                              | Ruta al PDF de transferencias |
| `--out`   | `<proyecto>/output/transfers.json`       | Ruta del JSON de salida |
| `--model` | `phi4-mini:latest`                       | Modelo de Ollama           |
| `--host`  | `http://localhost:11434`                 | URL del servidor Ollama    |

Más detalle en [`helper/README.md`](helper/README.md).

---

## 4. Estructura

```
docuclickai/
├── .venv/                  # Ignorado: entorno virtual local
├── docs/                   # PDFs NCR de ejemplo
├── helper/                 # Código fuente (CLI Python)
│   ├── docuclickai         # Entry point ejecutable
│   ├── main.py
│   ├── pdf_extractor.py
│   ├── ollama_client.py
│   ├── tmp_manager.py
│   └── requirements.txt
├── output/                 # Ignorado: JSON final persistente
├── tmp/                    # Ignorado: artefactos intermedios (auto-purge 24h)
└── README.md               # Este archivo
```

---

## 5. Solución de problemas

- **`.venv/bin/activate: No such file or directory`** — el venv no existe todavía.
  Créalo con `python3 -m venv .venv` (paso 2.1) y vuelve a activar.
- **`ModuleNotFoundError: No module named 'pypdf'`** — el venv no está activo o
  no instalaste las dependencias. Repite los pasos 2.2 y 2.3.
- **`OllamaError: ... connection refused`** — asegúrate de que Ollama esté
  corriendo (`ollama serve` o el servicio del sistema) y de haber descargado
  el modelo (`ollama pull phi4-mini:latest`).
- **Quiero borrar caché de ejecuciones anteriores** —
  `rm -rf tmp/* output/*` (ambos directorios están en `.gitignore`).
