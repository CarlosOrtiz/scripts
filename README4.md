# Investigación: Rotación de IP para NJ Courts Scraper

## Contexto

El scraper `bien.py` accede al portal de NJ Courts para extraer expedientes civiles.
El portal usa **Imperva** como anti-bot, que bloquea IPs de datacenters, nodos conocidos y requests sospechosos.

El objetivo fue encontrar una solución real y funcional para rotar IP en cada ejecución desde Colombia, accediendo a un sitio que solo acepta tráfico de USA.

---

## Etapa 1 — Tor (descartado)

### Qué intentamos

Usamos Tor con la señal `NEWNYM` para cambiar de circuito y obtener una IP nueva en cada ejecución.

### Log de resultado

```
[TOR] IP vieja: 192.42.116.47
[TOR] IP nueva: 192.76.153.253 ✅

Login fallo: Imperva bloqueo portal-cloud.
Revisa: output/html/01_cffi_landing.html
Usa IP residencial o bootstrap con browser real.
```

### Por qué falló

El problema no era técnico — Tor funcionó y cambió la IP. El problema es que:

- Los nodos de salida de Tor están en **listas negras públicas**
- Imperva las tiene indexadas y bloquea automáticamente
- Las IPs son de **datacenters europeos**, no residenciales USA
- El flujo resultante era: `Colombia → VPN USA → Tor Europa → NJ Courts ❌`
- NJ Courts solo acepta tráfico de USA con IPs residenciales

---

## Etapa 2 — Bright Data (requiere KYC)

### Qué intentamos

Bright Data es un proveedor de proxies residenciales. Sus IPs son de casas reales en USA, lo que evita la detección de Imperva.

```python
BRD_HOST = "brd.superproxy.io"
BRD_PORT = "22225"
BRD_USER = "brd-customer-XXXX-zone-njcourts"
BRD_PASS = "tu_password"

session.proxies = {
    "http":  f"http://{BRD_USER}:{BRD_PASS}@{BRD_HOST}:{BRD_PORT}",
    "https": f"http://{BRD_USER}:{BRD_PASS}@{BRD_HOST}:{BRD_PORT}",
}
```

### Log de resultado

```
[PROXY] IP activa: 104.63.45.77 ✅

[HTTP] Target funcional: safari15_3
[1] GET https://portal-cloud.njcourts.gov/...   status=200
[2] GET https://portal.njcourts.gov/pkmslogin.form   status=500
[3] POST https://portal-cloud.njcourts.gov/pkmslogin.form   status=402
```

Contenido de `output/html/02_cffi_post_login.html`:

```
<!doctype html>
<h1>Webpage not available</h1>
<p>Residential Failed (bad_endpoint): Requested site is not available
for immediate residential (no KYC) access mode due to the fact that
POST requests are not allowed. To get full residential access for
targeting this site, fill in the KYC form:
https://brightdata.com/cp/kyc</p>
```

### Por qué falló

Bright Data requiere verificación de identidad (**KYC — Know Your Customer**) para hacer requests POST en sitios gubernamentales o bancarios. Sin KYC:

- GET funciona
- POST está bloqueado
- El login de NJ Courts requiere POST — imposible completarlo

### Proceso para desbloquear

Para usar Bright Data con NJ Courts se requiere:

1. Ir a `https://brightdata.com/cp/kyc`
2. Llenar el formulario de verificación de identidad
3. Esperar aprobación (1-2 días hábiles)
4. Una vez aprobado, los POST quedan habilitados

Por tiempo y urgencia, se descartó esta opción y se pasó a IPRoyal.

---

## Etapa 3 — IPRoyal (solución actual)

### Por qué IPRoyal

- No requiere KYC para sitios gubernamentales
- Permite POST sin restricciones
- IPs residenciales USA reales
- Precio desde $7/GB
- Simple de integrar

### Configuración

```python
IPROYAL_HOST = "geo.iproyal.com"
IPROYAL_PORT = "12321"  # HTTP — probar 12323 si falla, o 12322 SOCKS5
IPROYAL_USER = os.getenv("IPROYAL_USER", "")
IPROYAL_PASS = os.getenv("IPROYAL_PASS", "")
```

Correr con variables de entorno:

```bash
export IPROYAL_USER="tu_usuario"
export IPROYAL_PASS="tu_password"
python bien.py
```

### Primer intento — puerto 12321 (error SSL)

```
[PROXY] Error conectando al proxy: SSLCertVerificationError(1,
'[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
self-signed certificate in certificate chain')
```

**Solución:** agregar `verify=False` a los requests porque IPRoyal usa certificado propio.

```python
resp = requests.get(IP_SERVICE, proxies=proxies, timeout=15, verify=False)
session.verify = False
```

### Segundo intento — puerto 12321 (proxy activo, error CONNECT 403)

```
[PROXY] IP activa: 67.83.86.44 ✅

[HTTP] Probe falló para safari15_3: Failed to perform,
curl: (56) CONNECT tunnel failed, response 403.

[HTTP] Probe falló para safari15_5: Failed to perform,
curl: (56) CONNECT tunnel failed, response 403.

[HTTP] Probe falló para safari18_0: Failed to perform,
curl: (56) CONNECT tunnel failed, response 403.
```

**Causa:** `curl_cffi` usa CONNECT tunnel para HTTPS. El puerto 12321 de IPRoyal no lo soporta bien.

**Solución:** cambiar a puerto `12323` (HTTP plano) o `12322` (SOCKS5).

### Tercer intento — puerto 12323 (error CONNECT persiste)

```
[PROXY] IP activa: 174.166.199.225 ✅

[HTTP] Probe falló para safari15_3: curl: (56) CONNECT tunnel failed, response 403.
[HTTP] Probe falló para safari15_5: curl: (56) CONNECT tunnel failed, response 403.
[HTTP] Probe falló para safari18_0: curl: (56) CONNECT tunnel failed, response 403.
```

**Causa:** `curl_cffi` no es compatible con proxies HTTP CONNECT para sitios HTTPS. Requiere SOCKS5.

### Solución final — SOCKS5 puerto 12322

Cambiar el protocolo del proxy de `http://` a `socks5://`:

```python
IPROYAL_PORT = "12322"

def get_proxy_url(rotate: bool = False) -> str:
    user = IPROYAL_USER
    if rotate:
        user = f"{IPROYAL_USER}_session-{random.randint(1, 999999)}"
    return f"socks5://{user}:{IPROYAL_PASS}@{IPROYAL_HOST}:{IPROYAL_PORT}"
```

---

## Resumen de puertos IPRoyal

| Puerto | Protocolo | Compatible con curl_cffi |
|--------|-----------|--------------------------|
| 12321  | HTTP      | No — falla CONNECT SSL   |
| 12323  | HTTP      | No — falla CONNECT 403   |
| 12322  | SOCKS5    | Sí                       |

---

## Flujo final correcto
```
Usando IPRoyal SOCKS5 USA, no es necesario usar VPN
```

```
Tu PC (Colombia) → IPRoyal SOCKS5 USA → NJ Courts ✅
```

Versus el flujo que fallaba:

```
Tu PC (Colombia) → VPN USA → Tor (Europa, IP en lista negra) → NJ Courts ❌
Tu PC (Colombia) → Bright Data (POST bloqueado sin KYC) → NJ Courts ❌
Tu PC (Colombia) → IPRoyal HTTP puerto 12321/12323 (CONNECT 403) → NJ Courts ❌
```

---

## Comandos de referencia

```bash
# Instalar dependencias
pip install requests stem curl_cffi beautifulsoup4

# Configurar credenciales
export IPROYAL_USER="tu_usuario"
export IPROYAL_PASS="tu_password"
export NJ_USERNAME="tu_usuario_njcourts"
export NJ_PASSWORD="tu_password_njcourts"
export TWOCAPTCHA_API_KEY="tu_api_key"

# Correr scraper
python cffi_v1.py
```

---

## Costo estimado IPRoyal

```
1 búsqueda en NJ Courts ≈ 50KB
1000 búsquedas           ≈ 50MB
Plan mínimo IPRoyal      = $7 (1GB)
1GB alcanza para         ≈ 20,000 búsquedas
```

## Observación — ¿Es necesario rotar IP con IPRoyal?
 
### Pocas búsquedas (menos de 500/día)
 
No hace falta rotar. IPRoyal asigna una IP residencial USA que Imperva no bloquea porque parece un usuario normal. La misma IP puede durar horas sin problema.
 
### Muchas búsquedas (500+/día)
 
Imperva empieza a detectar el patrón: misma IP, muchos requests seguidos, mismo User-Agent. En ese caso conviene activar rotación automática.
 
### Cómo activar rotación cuando sea necesario
 
IPRoyal soporta rotación automática por request agregando `_streaming-1` al usuario. No requiere cambiar la sesión autenticada:
 
```python
def get_proxy_url() -> str:
    # _streaming-1 = IPRoyal rota IP automáticamente en cada request
    user = f"{IPROYAL_USER}_streaming-1"
    return f"socks5://{user}:{IPROYAL_PASS}@{IPROYAL_HOST}:{IPROYAL_PORT}"
```
 
> **Importante:** no recrear la sesión `curl_cffi` para rotar IP — eso pierde las cookies del login. La rotación debe ocurrir solo en el proxy, manteniendo la misma sesión autenticada.
 
### Recomendación
 
Empezar sin rotación. Si Imperva bloquea después de muchos dockets, activar `_streaming-1` en `get_proxy_url()`. Para el volumen típico de este scraper no es necesario desde el inicio.

logs
```
22:33:02 [DEBUG] Starting new HTTPS connection (1): api.ipify.org:443
/Users/caol/.pyenv/versions/3.12.13/lib/python3.12/site-packages/urllib3/connectionpool.py:1097: InsecureRequestWarning: Unverified HTTPS request is being made to host 'api.ipify.org'. Adding certificate verification is strongly advised. See: https://urllib3.readthedocs.io/en/latest/advanced-usage.html#tls-warnings
  warnings.warn(
22:33:03 [DEBUG] https://api.ipify.org:443 "GET / HTTP/1.1" 200 11
[PROXY] IP activa: 73.10.98.19 ✅
22:33:03 [INFO] [CHECKPOINT] Encontrado -> último procesado: 000007 / 21  (guardado: 2026-04-14T22:25:19)

[CHECKPOINT] Retomando desde 000008 (último completado: 000007).

[=== curl_cffi: TLS fingerprint Safari + IPRoyal ===]
22:33:06 [INFO] [HTTP] Target funcional: safari15_3
22:33:06 [INFO] [1] GET https://portal-cloud.njcourts.gov/prweb/PRAuth/CloudSAMLAuth?AppName=ESSO
22:33:07 [INFO]   status=200  url=https://portal-cloud.njcourts.gov/prweb/PRAuth/CloudSAMLAuth?AppName=ESSO
22:33:07 [INFO]   [HTML] -> output/html/01_cffi_landing.html
22:33:07 [INFO] [2] GET https://portal.njcourts.gov/pkmslogin.form
22:33:09 [INFO]   status=500  url=https://portal.njcourts.gov/pkmslogin.form
22:33:09 [INFO]   [HTML] -> output/html/01b_cffi_idp_login.html
22:33:09 [INFO] [3] POST https://portal-cloud.njcourts.gov/pkmslogin.form
22:33:11 [INFO]   status=200  url=https://portal-cloud.njcourts.gov/prweb/PRAuth/app/default/CiqhCm6F71NzfFYz9kC01ILonOXvI_1W*/!STANDARD?AppName=ESSO
22:33:11 [INFO]   [HTML] -> output/html/02_cffi_post_login.html
22:33:11 [INFO]   Autenticacion exitosa

[=== Búsqueda civil — dockets 000008 a 000010 / año 21 ===]

────────────────────────────────────────────────────────────
[DOCKET] Procesando: 000008 / 21
────────────────────────────────────────────────────────────
22:33:11 [INFO] [INTENTO 1/3] docket=000008
22:33:11 [INFO] [4] GET https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/
22:33:11 [INFO]   status=200  url=https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/
22:33:11 [INFO]   [HTML] -> output/html/04_esso_portal.html
22:33:11 [INFO] [5] GET https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces
22:33:12 [INFO]   status=200  url=https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces
22:33:12 [INFO]   [HTML] -> output/html/05_civil_search_form.html
22:33:12 [INFO]   Formulario civil cargado via HTTP
22:33:12 [INFO] 
[6] county=ATLANTIC docket=000008/21
22:33:12 [INFO]   ViewState: Ct1k4VoLjnvJQHnrgzCLYzM9OYyV6JEyQd//jlIN...
22:33:12 [INFO]   Court: Civil Part (LCV)
22:33:12 [INFO]   County: ATLANTIC (ATL)
22:33:12 [INFO] [CAPTCHA] Solicitando token reCAPTCHA v3 2captcha...
22:33:12 [INFO] [CAPTCHA] Response status: 200
22:33:12 [DEBUG] [CAPTCHA] Response body: {"errorId":0,"taskId":82427222382}
22:33:12 [INFO] [CAPTCHA] Task creado: 82427222382
22:33:16 [DEBUG] [CAPTCHA] Polling 1/60 — status: processing
22:33:19 [DEBUG] [CAPTCHA] Polling 2/60 — status: processing
22:33:23 [DEBUG] [CAPTCHA] Polling 3/60 — status: processing
22:33:27 [DEBUG] [CAPTCHA] Polling 4/60 — status: processing
22:33:30 [DEBUG] [CAPTCHA] Polling 5/60 — status: processing
22:33:34 [DEBUG] [CAPTCHA] Polling 6/60 — status: processing
22:33:37 [DEBUG] [CAPTCHA] Polling 7/60 — status: processing
22:33:41 [DEBUG] [CAPTCHA] Polling 8/60 — status: processing
22:33:45 [DEBUG] [CAPTCHA] Polling 9/60 — status: processing
22:33:48 [DEBUG] [CAPTCHA] Polling 10/60 — status: processing
22:33:52 [DEBUG] [CAPTCHA] Polling 11/60 — status: processing
22:33:57 [DEBUG] [CAPTCHA] Polling 12/60 — status: processing
22:34:01 [DEBUG] [CAPTCHA] Polling 13/60 — status: processing
22:34:04 [DEBUG] [CAPTCHA] Polling 14/60 — status: processing
22:34:08 [DEBUG] [CAPTCHA] Polling 15/60 — status: processing
22:34:11 [INFO] [CAPTCHA] Token obtenido (2148 chars)
22:34:11 [INFO]   POST URL: https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces?cid=1
22:34:12 [INFO]   status=200  url=https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces?cid=1
22:34:12 [INFO]   [HTML] -> output/html/07_search_results_000008_21.html
22:34:12 [INFO]   Response title: eCourts Civil Case Jacket
22:34:12 [INFO]   Response length: 60437 chars
22:34:12 [INFO]   Contains caseSummaryDiv: True
22:34:12 [INFO]   docVenueTitleDC: ATL
22:34:12 [INFO]   Docket:  ATL-L-000008-21
22:34:12 [INFO]   Caption: Thames Ebony  Vs Bally'S Wild Wild We St Casino
22:34:12 [INFO] [7] POST https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSummary.faces?cid=1 (summary PDF)
22:34:14 [INFO]   status=200  url=https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSummary.faces?cid=1  content-type=application/pdf
22:34:14 [INFO]   [FILE] -> output/ATL-L-000008-21.pdf

[EXPORT] JSON -> output/docket_000008_21.json
1 casos exportados
22:34:14 [INFO] [CHECKPOINT] Guardado -> output/checkpoint.json  (último: 000008/21)

────────────────────────────────────────────────────────────
[DOCKET] Procesando: 000009 / 21
────────────────────────────────────────────────────────────
22:34:14 [INFO] [INTENTO 1/3] docket=000009
22:34:14 [INFO] [4] GET https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/
22:34:14 [INFO]   status=200  url=https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/
22:34:14 [INFO]   [HTML] -> output/html/04_esso_portal.html
22:34:14 [INFO] [5] GET https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces
22:34:14 [INFO]   status=200  url=https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces
22:34:14 [INFO]   [HTML] -> output/html/05_civil_search_form.html
22:34:14 [INFO]   Formulario civil cargado via HTTP
22:34:14 [INFO] 
[6] county=ATLANTIC docket=000009/21
22:34:14 [INFO]   ViewState: vdZh1kjHZx1XiaggqtxA+Dj6goMAmuosrgpjesaM...
22:34:14 [INFO]   Court: Civil Part (LCV)
22:34:14 [INFO]   County: ATLANTIC (ATL)
22:34:14 [INFO] [CAPTCHA] Solicitando token reCAPTCHA v3 2captcha...
22:34:15 [INFO] [CAPTCHA] Response status: 200
22:34:15 [DEBUG] [CAPTCHA] Response body: {"errorId":0,"taskId":82427227355}
22:34:15 [INFO] [CAPTCHA] Task creado: 82427227355
22:34:18 [DEBUG] [CAPTCHA] Polling 1/60 — status: processing
22:34:22 [DEBUG] [CAPTCHA] Polling 2/60 — status: processing
22:34:25 [DEBUG] [CAPTCHA] Polling 3/60 — status: processing
22:34:29 [DEBUG] [CAPTCHA] Polling 4/60 — status: processing
22:34:32 [INFO] [CAPTCHA] Token obtenido (2190 chars)
22:34:32 [INFO]   POST URL: https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces?cid=2
22:34:33 [INFO]   status=200  url=https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces?cid=2
22:34:33 [INFO]   [HTML] -> output/html/07_search_results_000009_21.html
22:34:33 [INFO]   Response title: eCourts Civil Case Jacket
22:34:33 [INFO]   Response length: 22274 chars
22:34:33 [INFO]   Contains caseSummaryDiv: False
22:34:33 [WARNING] [RESULTADO] No se encontró 'docVenueTitleDC'. Se reintentará.
22:34:33 [WARNING] [INTENTO 1/3] Sin datos. Reintentando...
22:34:35 [INFO] [INTENTO 2/3] docket=000009
22:34:35 [INFO] [4] GET https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/
22:34:35 [INFO]   status=200  url=https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/
22:34:35 [INFO]   [HTML] -> output/html/04_esso_portal.html
22:34:35 [INFO] [5] GET https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces
22:34:36 [INFO]   status=200  url=https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces
22:34:36 [INFO]   [HTML] -> output/html/05_civil_search_form.html
22:34:36 [INFO]   Formulario civil cargado via HTTP
22:34:36 [INFO] 
[6] county=ATLANTIC docket=000009/21
22:34:36 [INFO]   ViewState: yvdK5nAWWnHkCn5pBvkWUu02D6sUU0FjMqYGCnMw...
22:34:36 [INFO]   Court: Civil Part (LCV)
22:34:36 [INFO]   County: ATLANTIC (ATL)
22:34:36 [INFO] [CAPTCHA] Solicitando token reCAPTCHA v3 2captcha...
22:34:37 [INFO] [CAPTCHA] Response status: 200
22:34:37 [DEBUG] [CAPTCHA] Response body: {"errorId":0,"taskId":82427228994}
22:34:37 [INFO] [CAPTCHA] Task creado: 82427228994
22:34:40 [DEBUG] [CAPTCHA] Polling 1/60 — status: processing
22:34:44 [DEBUG] [CAPTCHA] Polling 2/60 — status: processing
22:34:47 [DEBUG] [CAPTCHA] Polling 3/60 — status: processing
22:34:51 [DEBUG] [CAPTCHA] Polling 4/60 — status: processing
22:34:54 [DEBUG] [CAPTCHA] Polling 5/60 — status: processing
22:34:58 [DEBUG] [CAPTCHA] Polling 6/60 — status: processing
22:35:01 [DEBUG] [CAPTCHA] Polling 7/60 — status: processing
22:35:05 [DEBUG] [CAPTCHA] Polling 8/60 — status: processing
22:35:08 [INFO] [CAPTCHA] Token obtenido (2212 chars)
22:35:08 [INFO]   POST URL: https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces?cid=3
22:35:09 [INFO]   status=200  url=https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces?cid=3
22:35:09 [INFO]   [HTML] -> output/html/07_search_results_000009_21.html
22:35:09 [INFO]   Response title: eCourts Civil Case Jacket
22:35:09 [INFO]   Response length: 22295 chars
22:35:09 [INFO]   Contains caseSummaryDiv: False
22:35:09 [WARNING] [RESULTADO] No se encontró 'docVenueTitleDC'. Se reintentará.
22:35:09 [WARNING] [INTENTO 2/3] Sin datos. Reintentando...
22:35:11 [INFO] [INTENTO 3/3] docket=000009
22:35:11 [INFO] [4] GET https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/
22:35:11 [INFO]   status=200  url=https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/
22:35:11 [INFO]   [HTML] -> output/html/04_esso_portal.html
22:35:11 [INFO] [5] GET https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces
22:35:11 [INFO]   status=200  url=https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces
22:35:11 [INFO]   [HTML] -> output/html/05_civil_search_form.html
22:35:11 [INFO]   Formulario civil cargado via HTTP
22:35:11 [INFO] 
[6] county=ATLANTIC docket=000009/21
22:35:11 [INFO]   ViewState: vI2RcPGaSCmJpnBV+IUs+QIH5Ip86CzB5sWMkIAr...
22:35:11 [INFO]   Court: Civil Part (LCV)
22:35:11 [INFO]   County: ATLANTIC (ATL)
22:35:11 [INFO] [CAPTCHA] Solicitando token reCAPTCHA v3 2captcha...
22:35:12 [INFO] [CAPTCHA] Response status: 200
22:35:12 [DEBUG] [CAPTCHA] Response body: {"errorId":0,"taskId":82427231781}
22:35:12 [INFO] [CAPTCHA] Task creado: 82427231781
22:35:15 [DEBUG] [CAPTCHA] Polling 1/60 — status: processing
22:35:19 [DEBUG] [CAPTCHA] Polling 2/60 — status: processing
22:35:22 [DEBUG] [CAPTCHA] Polling 3/60 — status: processing
22:35:25 [DEBUG] [CAPTCHA] Polling 4/60 — status: processing
22:35:29 [DEBUG] [CAPTCHA] Polling 5/60 — status: processing
22:35:32 [DEBUG] [CAPTCHA] Polling 6/60 — status: processing
22:35:36 [DEBUG] [CAPTCHA] Polling 7/60 — status: processing
22:35:39 [DEBUG] [CAPTCHA] Polling 8/60 — status: processing
22:35:43 [DEBUG] [CAPTCHA] Polling 9/60 — status: processing
22:35:47 [DEBUG] [CAPTCHA] Polling 10/60 — status: processing
22:35:50 [INFO] [CAPTCHA] Token obtenido (2190 chars)
22:35:50 [INFO]   POST URL: https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces?cid=4
22:35:51 [INFO]   status=200  url=https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces?cid=4
22:35:51 [INFO]   [HTML] -> output/html/07_search_results_000009_21.html
22:35:51 [INFO]   Response title: eCourts Civil Case Jacket
22:35:51 [INFO]   Response length: 22142 chars
22:35:51 [INFO]   Contains caseSummaryDiv: False
22:35:51 [WARNING] [RESULTADO] No se encontró 'docVenueTitleDC'. Se reintentará.
22:35:51 [WARNING] [INTENTO 3/3] Sin datos. Reintentando...
22:35:53 [WARNING] [DOCKET 000009] Sin resultado. Se omite checkpoint.

────────────────────────────────────────────────────────────
[DOCKET] Procesando: 000010 / 21
────────────────────────────────────────────────────────────
22:35:53 [INFO] [INTENTO 1/3] docket=000010
22:35:53 [INFO] [4] GET https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/
22:35:53 [INFO]   status=200  url=https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/
22:35:53 [INFO]   [HTML] -> output/html/04_esso_portal.html
22:35:53 [INFO] [5] GET https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces
22:35:54 [INFO]   status=200  url=https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces
22:35:54 [INFO]   [HTML] -> output/html/05_civil_search_form.html
22:35:54 [INFO]   Formulario civil cargado via HTTP
22:35:54 [INFO] 
[6] county=ATLANTIC docket=000010/21
22:35:54 [INFO]   ViewState: t2+e61cmSGOFSDz1rNGS9dxB8jtcRmWy8b9PiV2Q...
22:35:54 [INFO]   Court: Civil Part (LCV)
22:35:54 [INFO]   County: ATLANTIC (ATL)
22:35:54 [INFO] [CAPTCHA] Solicitando token reCAPTCHA v3 2captcha...
22:35:54 [INFO] [CAPTCHA] Response status: 200
22:35:54 [DEBUG] [CAPTCHA] Response body: {"errorId":0,"taskId":82427235055}
22:35:54 [INFO] [CAPTCHA] Task creado: 82427235055
22:35:58 [DEBUG] [CAPTCHA] Polling 1/60 — status: processing
22:36:01 [DEBUG] [CAPTCHA] Polling 2/60 — status: processing
22:36:05 [DEBUG] [CAPTCHA] Polling 3/60 — status: processing
22:36:08 [DEBUG] [CAPTCHA] Polling 4/60 — status: processing
22:36:12 [DEBUG] [CAPTCHA] Polling 5/60 — status: processing
22:36:15 [DEBUG] [CAPTCHA] Polling 6/60 — status: processing
22:36:19 [DEBUG] [CAPTCHA] Polling 7/60 — status: processing
22:36:22 [DEBUG] [CAPTCHA] Polling 8/60 — status: processing
22:36:25 [DEBUG] [CAPTCHA] Polling 9/60 — status: processing
22:36:29 [DEBUG] [CAPTCHA] Polling 10/60 — status: processing
22:36:33 [INFO] [CAPTCHA] Token obtenido (2212 chars)
22:36:33 [INFO]   POST URL: https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces?cid=5
22:36:33 [INFO]   status=200  url=https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces?cid=5
22:36:33 [INFO]   [HTML] -> output/html/07_search_results_000010_21.html
22:36:33 [INFO]   Response title: Pardon Our Interruption
22:36:33 [INFO]   Response length: 7426 chars
22:36:33 [INFO]   Contains caseSummaryDiv: False
22:36:33 [ERROR] [INTENTO 1/3] Error: Bloqueado por anti-bot: 'Pardon Our Interruption'
22:36:35 [INFO] [INTENTO 2/3] docket=000010
22:36:35 [INFO] [4] GET https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/
22:36:36 [INFO]   status=200  url=https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/
22:36:36 [INFO]   [HTML] -> output/html/04_esso_portal.html
22:36:36 [INFO] [5] GET https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces
22:36:36 [INFO]   status=200  url=https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces
22:36:36 [INFO]   [HTML] -> output/html/05_civil_search_form.html
22:36:36 [ERROR] [INTENTO 2/3] Error: Formulario civil no encontrado
22:36:38 [INFO] [INTENTO 3/3] docket=000010
22:36:38 [INFO] [4] GET https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/
22:36:39 [INFO]   status=200  url=https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/
22:36:39 [INFO]   [HTML] -> output/html/04_esso_portal.html
22:36:39 [INFO] [5] GET https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces
22:36:39 [INFO]   status=200  url=https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces
22:36:39 [INFO]   [HTML] -> output/html/05_civil_search_form.html
22:36:39 [ERROR] [INTENTO 3/3] Error: Formulario civil no encontrado
22:36:39 [ERROR] [DOCKET 000010] Falló tras 3 intentos.
22:36:39 [WARNING] [DOCKET 000010] Sin resultado. Se omite checkpoint.

[FIN] Proceso completado: dockets 000008–000010.
```

## Evidencia de resultados

![Resultados extracción](https://github.com/CarlosOrtiz/scripts/blob/main/img/result_v2.png?raw=true)

