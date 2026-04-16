# NJ Courts Civil Case Scraper — Análisis Técnico

Scraper para extraer datos de casos civiles del sistema **eCourts** del Poder Judicial de New Jersey (`portal.njcourts.gov`). Este documento explica el análisis de seguridad realizado, los obstáculos encontrados y por qué se eligió cada tecnología.

---

## El Problema: Múltiples Capas de Protección

El portal de NJ Courts no es un sitio web simple. Tiene **cuatro capas independientes de seguridad** que deben superarse en orden:

```
Internet
    │
    ▼
┌─────────────────────────────────┐
│  CAPA 1: Incapsula / Imperva    │  ← Firewall de aplicaciones web (WAF)
│  Bot detection por JS challenge │
└────────────────┬────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│  CAPA 2: IBM ISAM / SAML SSO   │  ← Autenticación empresarial
│  Detección por TLS fingerprint  │
└────────────────┬────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│  CAPA 3: hCaptcha               │  ← En páginas del portal ESSO y Civil
│  Challenge visual / invisible   │
└────────────────┬────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│  CAPA 4: reCAPTCHA v3 Enterprise│  ← En el formulario de búsqueda
│  Score-based (0.0 – 1.0)        │
└─────────────────────────────────┘
```

Cada capa requiere una solución diferente. Ninguna librería sola las resuelve todas.

---

## Análisis de Cada Capa

### Capa 1 — Incapsula / Imperva

**¿Qué hace?**
Incapsula sirve un challenge de JavaScript que el browser debe ejecutar para recibir cookies de sesión válidas (`reese84`, `visid_incap_*`, `incap_ses_*`, `nlbi_*`). Sin esas cookies, cualquier request HTTP recibe una página de bloqueo: *"Pardon Our Interruption"*.

**¿Por qué no funciona con requests HTTP simples?**
`requests`, `httpx`, `aiohttp` no ejecutan JavaScript. Incapsula lo sabe y los bloquea. Incluso `curl_cffi` no puede superar este challenge porque el JS es demasiado complejo.

**Solución: Bootstrap con Playwright**
Se lanza un browser real (Chromium) que ejecuta el challenge JS de Incapsula. Una vez resuelto, se extraen todas las cookies y se reutilizan en las capas siguientes. El bootstrap visita **ambos subdominios** porque cada uno tiene su propio site ID de Incapsula:
- `portal-cloud.njcourts.gov` (site ID 2548127) → SAML / SSO
- `portal.njcourts.gov` → Civil Case Search

---

### Capa 2 — IBM ISAM / SAML SSO

**¿Qué hace?**
El login del portal (`pkmslogin.form`) es gestionado por IBM Security Access Manager (ISAM). Además del usuario y contraseña, el sistema analiza la **huella TLS** del cliente a nivel de socket (JA3/JA4 fingerprint). Playwright/Chromium siempre presenta la huella TLS de Chromium, que está en la lista negra del ISAM.

**¿Por qué no funciona con Playwright para el login?**
El handshake TLS de Chromium automatizado es reconocible. El servidor ISAM lo identifica como bot y bloquea la autenticación, aunque el JS stealth esté activo.

**Solución: `curl_cffi`**
`curl_cffi` es una librería Python que impersona el TLS fingerprint de diferentes browsers a nivel de socket. Configurado con `safari15_5`, el handshake TLS pasa como Safari legítimo. Se usa **solo para el login** (GET portal → POST credenciales). Una vez autenticado, las cookies de sesión (`PD-S-SESSION-ID`, `JSESSIONID`, `Pega-RULES`) se transfieren al browser Playwright.

---

### Capa 3 — hCaptcha

**¿Qué hace?**
Imperva/Incapsula usa hCaptcha como segunda validación en las páginas del portal. En modo **invisible**, el challenge se resuelve automáticamente si el browser parece suficientemente legítimo. En modo **visual**, presenta un grid de imágenes para seleccionar.

**El problema del modo headless**
En `headless=True`, el browser presenta señales de automatización (ausencia de pantalla, hardware virtual) que hCaptcha detecta, activando el challenge visual. `sb.solve_captcha()` de SeleniumBase no puede resolver challenges visuales sin pantalla.

**Solución aplicada**
- En modo visible (`headless=False`): hCaptcha resuelve automáticamente (modo invisible).
- En modo headless: se aumenta el timeout de espera a 45 segundos y se implementa un retry de hasta 3 intentos de navegación para dar tiempo al challenge de auto-resolverse.

---

### Capa 4 — reCAPTCHA v3 Enterprise

**¿Qué hace?**
El formulario de búsqueda civil usa reCAPTCHA v3 Enterprise. A diferencia de v2, **no hay imagen para seleccionar**. En cambio, Google evalúa el comportamiento del browser y asigna un **score de 0.0 a 1.0**. El servidor rechaza requests con score bajo con el mensaje:
> *"Captcha verification has failed for this session."*

**Hallazgos del HTML analizado**
Del análisis del HTML de la página de búsqueda se extrajeron datos críticos:
- **Site key**: `6LeSprIqAAAAACbw4xnAsXH42Q4mfXk6t2MB09dq`
- **pageAction**: `CivilSearch` (no `search`, no `verify` — exactamente `CivilSearch`)
- **Campo oculto**: `searchByDocForm:recaptchaResponse`
- **Botón real**: `searchByDocForm:btnSearch` (con `display:none`)
- **Botón visible**: `searchByDocForm:searchBtnDummy` (dispara el JS que obtiene el token)

El JS de la página hace:
```javascript
grecaptcha.enterprise.execute('6LeSprIqAAAAACbw4xnAsXH42Q4mfXk6t2MB09dq', {action: 'CivilSearch'})
```

Si el `pageAction` no coincide exactamente, el servidor rechaza el token aunque el score sea alto.

**Solución: FreeCaptchaBypass (FCB)**
Se integra la API de [freecaptchabypass.com](https://freecaptchabypass.com) usando `ReCaptchaV3EnterpriseTaskProxyLess` con `minScore: 0.7`. FCB resuelve el reCAPTCHA externamente y devuelve el token, que se inyecta directamente en el campo oculto. El botón `btnSearch` se dispara via `page.evaluate()` porque es `display:none` y Playwright no puede hacer click en elementos invisibles.

**Fallback**
Si no hay API key de FCB configurada, se usa `playwright-recaptcha` que captura pasivamente el token cuando el JS nativo llama al endpoint `/reload`.

---

## Arquitectura Final — Tres Capas

```
┌──────────────────────────────────────────────────────────────────┐
│  CAPA A — Playwright Bootstrap (se abre y cierra)                │
│                                                                  │
│  1. Lanza Chromium headless                                      │
│  2. Visita portal-cloud.njcourts.gov → resuelve Incapsula        │
│  3. Visita portal.njcourts.gov → resuelve Incapsula              │
│  4. Extrae cookies: reese84, visid_incap_*, incap_ses_*, nlbi_*  │
│  5. browser.close()                                              │
└──────────────────────────────────────────────────────────────────┘
                              │ cookies
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  CAPA B — curl_cffi (HTTP puro, sin browser)                     │
│                                                                  │
│  1. Inyecta cookies de Capa A                                    │
│  2. GET portal-cloud → obtiene form SAML                         │
│  3. POST pkmslogin.form con TLS fingerprint Safari 15.x          │
│  4. Obtiene cookies de sesión: PD-S-SESSION-ID, JSESSIONID, etc. │
└──────────────────────────────────────────────────────────────────┘
                              │ cookies sesión
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  CAPA C — SeleniumBase CDP + Playwright (browser principal)      │
│                                                                  │
│  1. SeleniumBase lanza Chrome con patches anti-detección CDP     │
│  2. Playwright se conecta vía connect_over_cdp()                 │
│  3. Carga cookies de Capa A + Capa B                             │
│  4. Navega ESSO portal → resuelve hCaptcha                       │
│  5. Abre nueva pestaña → carga Civil Search URL                  │
│  6. Llena formulario (county, docket type, número, año)          │
│  7. Simula comportamiento humano (scroll, mouse, clicks)         │
│  8. FCB obtiene token reCAPTCHA v3 Enterprise                    │
│  9. Inyecta token → dispara btnSearch via JS                     │
│  10. Extrae datos del case summary                               │
│  11. Exporta JSON                                                │
└──────────────────────────────────────────────────────────────────┘
```

---

## Por Qué Cada Tecnología

### Playwright
Browser automation con API async. Se usa en dos momentos distintos:
1. **Bootstrap**: Para ejecutar el JS challenge de Incapsula y obtener cookies.
2. **Sesión principal**: Conectado al Chrome de SeleniumBase via `connect_over_cdp()` para navegar el portal, llenar formularios y extraer datos.

**¿Por qué no solo Playwright?**
Playwright lanzado con `pw.chromium.launch()` deja señales de automatización en el protocolo CDP que Incapsula detecta. Por eso se conecta al Chrome de SeleniumBase que ya tiene los patches aplicados.

### SeleniumBase CDP (`cdp_driver`)
SeleniumBase en modo CDP lanza Google Chrome real con patches de anti-detección aplicados **a nivel de protocolo**, no solo en JavaScript:
- Elimina `navigator.webdriver` desde el protocolo CDP (no parcheable con JS)
- Oculta flags de automatización de Chrome
- Permite que reCAPTCHA Enterprise genere scores reales (no el 0.1 de fallback que obtiene Playwright puro)

**¿Por qué no undetected-chromedriver?**
SeleniumBase CDP permite adjuntar Playwright via `connect_over_cdp()`, lo que da acceso a toda la API de Playwright (selectores, network interception, etc.) sobre un Chrome sin detección. Es lo mejor de ambos mundos.

### `curl_cffi`
Librería Python que envuelve `libcurl` con soporte para impersonar TLS fingerprints de browsers reales. Configurado con `safari15_5` o `safari15_3`:
- El handshake TLS presenta las mismas cipher suites, extensiones y orden que Safari real
- IBM ISAM no lo detecta como bot
- Se usa **solo para el login** — no para navegación general

**¿Por qué no `requests` o `httpx`?**
Ambos tienen TLS fingerprints estándar de Python/OpenSSL que IBM ISAM tiene en lista negra.

### `playwright-recaptcha`
Librería que escucha pasivamente el POST al endpoint `/reload` de reCAPTCHA y captura el token de la respuesta. Se inicializa **antes** de navegar a la página con reCAPTCHA (requerimiento del README oficial) para no perder ninguna petición.

Se usa como **fallback** cuando no hay API key de FCB configurada.

### FreeCaptchaBypass (FCB)
Servicio externo que resuelve reCAPTCHA v3 Enterprise usando sus propios browsers con comportamiento humano real, garantizando scores altos. La integración usa `cffi_requests` (ya disponible) para llamar su API — no requiere librería adicional.

Parámetros críticos:
- `type`: `ReCaptchaV3EnterpriseTaskProxyLess`
- `pageAction`: `CivilSearch` (obtenido del análisis del JS de la página)
- `minScore`: `0.7`

### BeautifulSoup
Parser HTML para extraer los campos del case summary. El HTML de `civilCaseSummary.faces` usa una estructura JSF (JavaServer Faces) con spans de clase `ValueField`/`LabelField` y IDs específicos para cada campo.

---

## Campos Extraídos

Del case summary se extraen 26 campos:

| Campo | Fuente en HTML |
|-------|----------------|
| `docket_number` | Spans: `docVenueTitleDC` + `docTypeCodeTitle` + `docSeqNumTitle` + `docYeaerTitle` |
| `Case Caption` | `id="idCaseTitle"` |
| `Court`, `Venue`, `Case Type`, etc. | `<span class="ValueField">Label:</span>` → sibling `<span class="LabelField">` |
| `Consolidated Case` | `id` contiene `consolidatedCaseN` |
| `Statewide Lien` | `id` contiene `jdgmntStatewideLien` |

---

## Configuración

```python
CONFIG = {
    "username": "tu_usuario",
    "password": "tu_password",
    "headless": False,       # True = producción, False = debug (ver browser)
    "cffi_target": "safari15_3",
    "fcb_api_key": "",       # Key de freecaptchabypass.com (opcional)
}
```

### headless=False (recomendado para desarrollo)
El browser es visible. hCaptcha pasa en modo invisible automáticamente. Útil para depurar.

### headless=True (producción)
El browser corre sin ventana. hCaptcha puede tardar más — el código reintenta hasta 3 veces con timeout de 45s. Funciona con sesiones previas guardadas en `session_latest.json`.

---

## Instalación

```bash
pip install -r requirements.txt
playwright install chromium
```

**`requirements.txt`**
```
beautifulsoup4
curl-cffi
playwright
playwright-recaptcha
seleniumbase
```

---

## Output

El scraper genera en `./output/`:
```
output/
├── casos_YYYYMMDD_HHMMSS.json   ← Datos extraídos
├── screenshots/                  ← Capturas de cada paso
├── html/                         ← HTML de cada página
└── session_latest.json           ← Cookies para reutilizar en siguiente ejecución
```

El JSON tiene la siguiente estructura por caso:
```json
{
  "docket_number": "ATL-L-000001-15",
  "Case Caption": "Tiffin Betty Vs Daiichi Sankyo Inc",
  "Court": "...",
  "Venue": "ATLANTIC",
  "Case Initiation Date": "...",
  "Case Type": "...",
  "Case Status": "...",
  ...
}
```

---

## Lecciones Aprendidas

1. **Cada capa de seguridad necesita su propia solución** — no existe una bala de plata.
2. **El TLS fingerprint es tan importante como el JS stealth** — IBM ISAM filtra por handshake TLS antes de revisar headers o JS.
3. **El `pageAction` de reCAPTCHA debe ser exacto** — enviar `"search"` cuando el sitio usa `"CivilSearch"` resulta en rechazo aunque el score sea alto.
4. **`display:none` bloquea Playwright** — `btnSearch` es invisible, hay que dispararlo via `page.evaluate()`.
5. **La persistencia de sesión mejora el score de reCAPTCHA** — cookies de sesiones anteriores en `session_latest.json` hacen que el browser parezca "conocido" para Google.
6. **hCaptcha en headless necesita más tiempo** — aumentar timeouts y reintentar es más confiable que intentar resolver el challenge visualmente.
7. **Incapsula tiene site IDs por subdominio** — las cookies de `portal-cloud.njcourts.gov` no son válidas para `portal.njcourts.gov` automáticamente; hay que visitar ambos en el bootstrap.
