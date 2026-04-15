# Investigación: Rotación de IP para NJ Courts Scraper

## Contexto

El scraper `bien.py` accede al portal de NJ Courts para extraer expedientes civiles.
El portal usa **Imperva** como anti-bot, que bloquea IPs de datacenters, nodos conocidos y requests sospechosos.

El objetivo fue encontrar una solución real y funcional para rotar IP en cada ejecución desde Colombia, accediendo a un sitio que solo acepta tráfico de USA.

---

## Etapa 1 — Tor (descartado)

### Qué intentamos

Usamos Tor con la señal `NEWNYM` para cambiar de circuito y obtener una IP nueva en cada ejecución.

```python
from stem import Signal
from stem.control import Controller

with Controller.from_port(port=9051) as ctrl:
    ctrl.authenticate()
    ctrl.signal(Signal.NEWNYM)
    time.sleep(ctrl.get_newnym_wait())
```

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
