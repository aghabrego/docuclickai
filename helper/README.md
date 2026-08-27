# DocuClickAI Helper

Utilidad Python (CLI) para extraer transferencias de los PDFs NCR de Wendy's
(p. ej. `45 -Transferencias Versalles junio 2026.pdf`) y convertirlas a un
JSON agrupado por tienda, usando un modelo de Ollama local.

## Modelo por defecto

`phi4-mini:latest` ejecutándose en `http://localhost:11434`.

## Estructura

```
helper/
├── docuclickai        # Entry point ejecutable (clic)
├── main.py            # Módulo principal
├── pdf_extractor.py   # Extracción de texto con pypdf
├── ollama_client.py   # Cliente HTTP a Ollama
├── tmp_manager.py     # tmp/ con nombres largos y lazy cleanup 24h
└── requirements.txt
```

A nivel del proyecto:

```
docuclickai/
├── tmp/       # Auto-gestionado, se purga lo >24h al ejecutar
└── output/    # JSON final persistente (default)
```

## Instalación

```bash
cd helper
pip install --user pypdf
```

Asegúrate de tener Ollama corriendo y el modelo descargado:

```bash
ollama pull phi4-mini:latest
```

## Uso (clic)

```bash
./docuclickai \
  --pdf "../docs/45 -Transferencias Versalles junio 2026.pdf"
```

Si omites `--out`, el JSON se guarda en `../output/transfers.json`.

Flags opcionales:

| Flag      | Default                                          | Descripción                |
|-----------|--------------------------------------------------|----------------------------|
| `--pdf`   | (requerido)                                      | Ruta al PDF de transferencias |
| `--out`   | `<proyecto>/output/transfers.json`               | Ruta del JSON de salida |
| `--model` | `phi4-mini:latest`                              | Modelo de Ollama           |
| `--host`  | `http://localhost:11434`                         | URL del servidor Ollama    |

## tmp/ y limpieza

Cada ejecución crea una sesión con nombre:

```
tmp/session_YYYYMMDD-HHMMSS_<token>/
  ├── pdf_text_<ms>_<uuid>__txt.txt
  └── ollama_raw_response_<ms>_<uuid>__json.json
```

- **Nombres largos**: `prefijo_<13d ms>_<32 hex>` con etiqueta legible `__<kind>` y extensión real.
- **Lazy cleanup**: al ejecutar, `tmp_manager.lazy_cleanup` borra entradas con
  `mtime > 24h`. No hace falta cron.
- Si quieres forzar arranque limpio: `rm -rf ../tmp/*`.

## Esquema JSON de salida

```json
{
  "origen": "18 Wen Versalles",
  "anio_fiscal": 2026,
  "periodo": 6,
  "rango_fechas": "06/01/2026 - 06/28/2026",
  "transferencias": [
    {
      "tienda": "05 Wen Metromall",
      "tipo": "IN",
      "transfer_id": "5",
      "transfer_date": "06/01/2026",
      "items": [
        {
          "description": "Chicken Crispy 5/30 UN",
          "category": "Alimentos",
          "quantity_transferred": 1.00,
          "unit_transferred": "CA=5/30 UN",
          "cost_unit": 56.22,
          "extension": 56.22
        }
      ],
      "transfer_total": 100.12
    }
  ]
}
```
