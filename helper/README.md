# DocuClickAI Helper

Utilidad Python (CLI) para extraer transferencias de los PDFs NCR de Wendy's
(p. ej. `45 -Transferencias Versalles junio 2026.pdf`) y convertirlas a un
JSON agrupado por tienda, usando un modelo de Ollama local.

## Modelo por defecto

`qwen2.5:7b` ejecutándose en `http://localhost:11434`.

```bash
ollama pull qwen2.5:7b
```

## Estructura

```
helper/
├── docuclickai        # Entry point ejecutable
├── main.py            # Orquestación: extract → Ollama → JSON
├── pdf_extractor.py   # Extracción de texto con pypdf (layout mode)
├── ollama_client.py   # Cliente HTTP a Ollama + parse_json + retry
├── tmp_manager.py     # tmp/ con nombres largos y lazy cleanup 24h
└── requirements.txt   # pypdf, ollama
```

A nivel del proyecto:

```
docuclickai/
├── tmp/       # Auto-gestionado, se purga lo >24h al ejecutar
└── output/    # JSON final persistente (default)
```

## Instalación

Desde la raíz del proyecto, dentro del venv activo:

```bash
pip install -r helper/requirements.txt
```

Esto instala `pypdf` (lector de PDFs) y el cliente Python de Ollama. No
instales dependencias en el Python del sistema — usa siempre el venv (ver
[README raíz](../README.md)).

## Uso (clic)

Desde la raíz del proyecto:

```bash
./helper/docuclickai \
  --pdf "docs/45 -Transferencias Versalles junio 2026.pdf"
```

Si omites `--out`, el JSON se guarda en `output/transfers.json`.

Flags opcionales:

| Flag           | Default                              | Descripción                       |
|----------------|--------------------------------------|-----------------------------------|
| `--pdf`        | (requerido)                          | Ruta al PDF de transferencias     |
| `--out`        | `<proyecto>/output/transfers.json`   | Ruta del JSON de salida           |
| `--model`      | `qwen2.5:7b`                         | Modelo de Ollama                  |
| `--host`       | `http://localhost:11434`             | URL del servidor Ollama           |
| `--timeout`    | `600`                                | Timeout por sección en segundos   |
| `--max-retries`| `2`                                  | Reintentos si el JSON viene truncado |

## tmp/ y limpieza

Cada ejecución crea una sesión con nombre largo:

```
tmp/session_YYYYMMDD-HHMMSS_<token>/
  ├── pdf_full_<ms>_<uuid>__txt.txt
  ├── pdf_header_<ms>_<uuid>__txt.txt
  ├── pdf_transfer_in_<ms>_<uuid>__txt.txt
  ├── pdf_transfer_out_<ms>_<uuid>__txt.txt
  ├── ollama_<tienda>_raw_<ms>_<uuid>__json.json
  └── ollama_<tienda>_total_mismatch_raw_<ms>_<uuid>__json.json
```

- **Nombres largos**: `prefijo_<13d ms>_<32 hex>` con etiqueta legible `__<kind>` y extensión real.
- **Lazy cleanup**: al ejecutar, `tmp_manager.lazy_cleanup` borra entradas con
  `mtime > 24h`. No hace falta cron.
- Si quieres forzar arranque limpio: `rm -rf tmp/*`.

## Esquema JSON de salida

```json
{
  "origen": "18 Wen Versalles",
  "anio_fiscal": 2026,
  "periodo": 6,
  "rango_fechas": "06/01/2026 - 06/28/2026",
  "totales": {
    "transfer_in_total": 656.89,
    "transfer_out_total": 1036.70
  },
  "transfer_in": [
    {
      "tienda": "05 Wen Metromall",
      "subtotal_tienda": 226.48,
      "transferencias": [
        {
          "transfer_id": "5",
          "transfer_datetime": "01/06/2026 06:02:37",
          "transfer_date": "01/06/2026",
          "items": [
            {
              "description": "Chicken Crispy",
              "category": "Alimentos",
              "quantity_transferred": 1.0,
              "unit_transferred": "CA=5/30 UN",
              "cost_unit": 56.22,
              "extension": 56.22
            }
          ],
          "transfer_total": 100.12
        }
      ]
    }
  ],
  "transfer_out": [ /* misma estructura que transfer_in */ ]
}
```

### Campos

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `origen` | string | Tienda que reporta (cabecera del PDF) |
| `anio_fiscal` | int | Año fiscal |
| `periodo` | int | Mes dentro del año fiscal (1-12) |
| `rango_fechas` | string | `DD/MM/YYYY - DD/MM/YYYY` |
| `totales.transfer_in_total` | float | Suma de todos los `subtotal_tienda` IN |
| `totales.transfer_out_total` | float | Suma de todos los `subtotal_tienda` OUT |
| `transfer_in` / `transfer_out` | array | Una entrada por tienda |
| `<tienda>.subtotal_tienda` | float \| null | Suma de `transfer_total` de esa tienda |
| `<tienda>.transferencias` | array | Una entrada por transferencia |
| `transfer_id` | string | ID del transfer (1-2 dígitos según tienda) |
| `transfer_datetime` | string | `DD/MM/YYYY HH:MM:SS` (fecha real del sistema) |
| `transfer_date` | string | `DD/MM/YYYY` derivado del datetime |
| `transfer_total` | float \| null | Total reportado en el PDF (o inferido, ver warning) |
| `items[].description` | string | Descripción del producto |
| `items[].category` | string | `Alimentos` \| `Papeleria` \| `Operaciones` \| `Limpieza` |
| `items[].quantity_transferred` | float | Cantidad transferida |
| `items[].unit_transferred` | string | Empaque (ej. `CA=5/30 UN`, `Bulto = 20 LB`) |
| `items[].cost_unit` | float | Precio unitario (NO el número de empaque) |
| `items[].extension` | float | `quantity × cost_unit` |

### Marcadores de auditoría

Cuando hay inconsistencias detectadas post-Ollama, se añaden campos con
prefijo `_` (no forman parte del contrato, son informativos):

| Marcador | Significado |
|----------|-------------|
| `_transfer_total_inferred: "from_sum_of_extension"` | El modelo devolvió `transfer_total=null`; se calculó desde `Σ extension` |
| `_validation_warning: "qty*cost != extension"` | En ese item: `quantity × cost_unit ≠ extension` |
| `_validation_warning_total: "transfer_total != sum(extension)"` | El total reportado por el modelo no cuadra con la suma de los items |
| `_items_with_warning: <int>` | Cuántos items de la transferencia tienen warning de qty/cost |
| `_empty_response: true` | El bloque no produjo items (fallback del completeness retry) |
| `_validation_warning` (en item) = `"cost_unit_inferred"` | El modelo puso el número de empaque como `cost_unit`; se recalculó |

Si `subtotal_tienda` sale `null`, el modelo no devolvió ningún
`transfer_total` parseable para esa tienda (revisar artefactos en `tmp/`).

## Validaciones defensivas (post-procesado)

1. **Items fantasma**: se descartan filas cuya `description` solo contiene
   paréntesis `()`, coletillas tipo `(720un)` o fragmentos sueltos como
   `8/30un (240un)`.
2. **`transfer_date`**: se recalcula siempre desde `transfer_datetime` (los
   primeros 10 chars después de `" - "`). El campo `Transfer Date:` del PDF
   puede estar desfasado o pegado al item siguiente tras un salto de página.
3. **`cost_unit` inferido**: si `cost_unit × qty > extension × 2` (señal típica
   de que el modelo puso el empaque como precio), se recalcula
   `cost_unit = extension / qty`.
4. **`transfer_total` faltante**: se infiere de `Σ extension` (marcado con
   `_transfer_total_inferred`).
5. **`subtotal_tienda`**: siempre se recalcula desde `Σ transfer_total` para
   evitar alucinaciones del modelo (ej. `-379.81`).
6. **Completitud**: si Ollama omitió transferencias detectadas en el PDF, se
   reintenta cada bloque faltante individualmente.
