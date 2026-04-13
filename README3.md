# NJ Courts Civil Case Scraper — Estado del Proyecto

## Descripción general

Script Python que extrae expedientes civiles del portal **eCourts de NJ Courts**
(`portal.njcourts.gov`) usando HTTP puro con fingerprint TLS de Safari (`curl_cffi`),
autenticación IBM ISAM con 2FA OTP, resolución de reCAPTCHA v3 Enterprise vía 2Captcha,
y parsing de formularios JSF/PrimeFaces con BeautifulSoup.

Por cada docket encontrado genera un **JSON** con los metadatos del caso y descarga el
**PDF** del Summary Report.

---

## Flujo técnico

```
1. Probe de TLS fingerprint (safari15_3 / safari15_5 / safari18_0)
2. Login IBM ISAM  →  pkmslogin.form
3. 2FA OTP         →  código por correo
4. GET formulario civil  →  extrae JSF ViewState (fresco por cada búsqueda)
5. Resolución reCAPTCHA v3 Enterprise  →  2Captcha API (~30–60 s)
6. POST búsqueda con ViewState + token captcha
7. Parsing resultado  →  JSON + PDF por docket
8. Checkpoint actualizado
```

---

## Uso

```bash
# Variables de entorno requeridas
export NJ_USERNAME="tu_usuario"
export NJ_PASSWORD="tu_contraseña"
export TWOCAPTCHA_API_KEY="tu_api_key"

# Ejecutar — dockets 000001 al 000100, año 21
python njcourts_scraper.py --start 1 --end 100 --year 21

# Con OTP directo (si ya lo tienes)
python njcourts_scraper.py --start 1 --end 100 --year 21 --otp 123456
```

### Flags CLI

| Flag | Default | Descripción |
|------|---------|-------------|
| `--start` | `1` | Primer docket number |
| `--end` | `5` | Último docket number |
| `--year` | `21` | Año del docket |
| `--otp` | `""` | Código OTP 2FA (si se omite, lo pide por consola) |

---

## Sistema de checkpoint

Al finalizar cada docket exitoso se escribe `output/checkpoint.json`:

```json
{
  "last_docket_num": "000004",
  "last_docket_int": 4,
  "docket_year": "21",
  "saved_at": "2026-04-13T09:54:04"
}
```

En la siguiente ejecución el script lee ese archivo y **retoma automáticamente
desde el docket siguiente**, sin necesidad de pasar `--start` manualmente.

---

## Outputs generados

```
output/
├── checkpoint.json               ← progreso entre ejecuciones
├── docket_000002_21.json         ← metadatos del caso
├── docket_000003_21.json
├── ATL-L-000002-21.pdf           ← Summary Report PDF
├── ATL-L-000003-21.pdf
└── html/
    ├── 05_civil_search_form.html ← form JSF cargado
    ├── 07_search_results_000002_21.html
    └── ...
```

---

## Estado actual y limitaciones conocidas

### ⚠️ Problema principal: Imperva (anti-bot) bloquea la sesión de forma inconsistente

Este es el cuello de botella dominante del scraper. El portal usa **Imperva / Incapsula**
como WAF y lo activa de forma no determinista durante la sesión. Los síntomas observados
en los logs son:

#### 1. Bloqueo en el login (`portal-cloud.njcourts.gov`)
```
Login fallo: Imperva bloqueo portal-cloud.
```
Aparece en la mayoría de ejecuciones consecutivas. La VPN debe desconectarse
y reconectarse para obtener una IP "limpia" antes de poder autenticarse.

#### 2. Bloqueo durante la generación del PDF (`civilCaseSummary.faces`)
```
content-type=text/html   ← esperaba application/pdf
Error: Bloqueado por anti-bot al generar el PDF: 'Pardon Our Interruption'
```
El docket se encuentra correctamente (JSON extraído), pero Imperva bloquea
la descarga del PDF. En la siguiente ejecución ese docket se reintenta completo.

#### 3. Bloqueo en el POST de búsqueda (`civilCaseSearch.faces`)
```
Response title: Pardon Our Interruption
Response length: 7426 chars
```
La búsqueda misma es bloqueada. Al siguiente intento la sesión HTTP ya está
comprometida y el formulario civil tampoco carga (`Formulario civil no encontrado`).

---

### ⚠️ Inconsistencia en la tasa de extracción

Observado empíricamente en los logs con dockets `000001`–`000005`:

| Ejecución | VPN | Dockets procesados | Resultado |
|-----------|-----|-------------------|-----------|
| 1 | activa | 000001 | 0 extraídos — formulario devuelto siempre |
| 2 | activa | 000002–000005 | 2 extraídos (000002, 000003), bloqueado en 000004 PDF |
| 3 (restart VPN) | reconnect | 000004–000005 | 1 extraído (000004) en intento 3/3, 000005 falla |
| 4 (restart VPN) | reconnect | 000005 | 1 extraído (000005) en intento 3/3 |

**Patrón observado:**
- Nunca se extrajeron 5 dockets en una sola ejecución.
- La extracción exitosa llega en el **intento 2 o 3** de cada docket, no en el primero.
- Cada ejecución logra extraer **1–2 dockets** antes de que Imperva intervenga.
- El proceso **no es lineal**: el mismo docket puede fallar varias ejecuciones
  seguidas y luego funcionar en la siguiente.

---

## Evidencia de resultados

![Resultados extracción](https://github.com/CarlosOrtiz/scripts/blob/main/img/result.png?raw=true)

### ⚠️ Workaround manual requerido: ciclo VPN

El flujo real de operación actualmente es:

```
ejecutar script
   ↓
script falla o extrae 1–2 dockets y se bloquea
   ↓
desactivar VPN → activar VPN  (obtener nueva IP)
   ↓
volver a ejecutar  →  el checkpoint retoma donde quedó
   ↓
repetir hasta completar el rango
```

Este ciclo es completamente manual y consume tiempo proporcional al número
de dockets a extraer.

---

### ⚠️ Costo de captcha por fallo

Cada intento resuelve un captcha vía 2Captcha (~30–60 s de espera).
Si el servidor devuelve el formulario en vez del caso (docket sin resultado o
sesión degradada), ese captcha se consume sin producir datos.

Con 3 reintentos por docket y alta tasa de fallos por Imperva, el gasto
en captchas puede ser significativo para rangos grandes.

---

## Causas raíz identificadas

| Causa | Evidencia en logs |
|-------|------------------|
| Imperva detecta la sesión HTTP como bot después de 2–3 requests exitosos | `Pardon Our Interruption` aparece en el 3er–5to request de la sesión |
| El fingerprint TLS (`safari15_3`) falla en la mayoría de intentos de login | `All probes failed. Falling back to safari15_3` |
| El portal tiene rate-limiting por IP con ventana corta | Varias ejecuciones seguidas fallan; tras rotar IP funciona |
| El docket `000001` no existe en el sistema (año 21) | Siempre devuelve el formulario, ningún reintento lo encuentra |

---

## Dependencias

```
curl_cffi
beautifulsoup4
```

```bash
pip install curl_cffi beautifulsoup4
```

---
