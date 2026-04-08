# NJ Courts Civil Case Scraper

Scraper experimental para analizar el flujo de consulta de casos civiles en `portal.njcourts.gov` y documentar los obstáculos técnicos encontrados durante las pruebas.

Este repositorio no documenta una solución final de automatización completa del flujo protegido. El objetivo de este archivo es dejar constancia de:

- las pruebas realizadas;
- los mecanismos observados en el portal;
- los hallazgos del HTML y del flujo JSF;
- por qué ciertos enfoques sí avanzan parcialmente y otros no;
- por qué el proyecto terminó orientándose a un flujo híbrido o de análisis.

## Objetivo

El objetivo inicial fue automatizar la búsqueda por docket en el sistema eCourts Civil Case Jacket de New Jersey y extraer resultados estructurados como JSON.

La idea original era:

1. autenticarse en el portal;
2. navegar al formulario civil;
3. enviar una búsqueda por docket;
4. obtener la página de resultados o el `case summary`;
5. parsear la respuesta.

Durante las pruebas se observó que el portal usa varias capas de protección y validación, algunas visibles y otras invisibles, lo que cambia por completo la estrategia necesaria.

## Resumen del problema

El sitio no es un formulario HTTP simple. El flujo combina:

- protección anti-bots a nivel de perímetro;
- autenticación empresarial SSO;
- flujo JSF con `ViewState`;
- validaciones del lado cliente;
- y, en el formulario de búsqueda, una verificación de `reCAPTCHA Enterprise`.

En la práctica, eso significa que no basta con hacer un `POST` con los campos del formulario.

## Capas observadas

### 1. Protección perimetral

Durante las primeras pruebas se observó comportamiento compatible con WAF / anti-bot en el acceso inicial al portal.

Síntomas típicos encontrados:

- respuestas que no correspondían al contenido esperado;
- redirecciones o páginas intermedias;
- necesidad de cookies de sesión válidas antes de avanzar;
- diferencias de comportamiento según navegador o cliente HTTP.

Esto sugería filtrado por reputación del cliente, fingerprints o challenges previos.

### 2. Login IBM ISAM / SSO

El acceso autenticado se realiza mediante `pkmslogin.form`, propio de un flujo IBM ISAM / SSO.

Hallazgos:

- el login no es una pantalla aislada, sino parte de un flujo encadenado;
- después del envío de credenciales puede aparecer una pantalla de 2FA;
- las cookies de sesión posteriores al login son necesarias para acceder al portal civil;
- no todos los clientes HTTP se comportan igual frente a este login.

### 3. 2FA / OTP

Tras autenticar usuario y contraseña, el sistema puede requerir OTP.

En las pruebas se validó que:

- aparece una pantalla de selección de método;
- luego se presenta el formulario de ingreso del código OTP;
- el flujo depende de campos ocultos y del estado del formulario;
- la sesión debe mantenerse viva entre el login, la selección del método y el submit del OTP.

### 4. JSF y `javax.faces.ViewState`

El formulario civil usa JavaServer Faces.

Eso implica:

- formularios con campos ocultos generados por JSF;
- `javax.faces.ViewState` obligatorio;
- nombres de campos acoplados al árbol de componentes JSF;
- posibles submits distintos según el botón presionado.

En otras palabras, no alcanza con enviar solo `county`, `docket`, `year` y `court`.

### 5. reCAPTCHA Enterprise en la búsqueda civil

En una etapa de las pruebas se pensó que la búsqueda por docket no usaba captcha, porque el HTML de `civilCaseSummary.faces` no lo mostraba.

Luego, al inspeccionar el HTML real de `civilCaseSearch.faces`, se confirmó que sí existe `reCAPTCHA Enterprise` en el formulario de búsqueda.

## Hallazgo clave: el captcha no está en la página de resultado

Una confusión inicial vino de analizar la página de `civilCaseSummary.faces`, donde ya no aparece el formulario de búsqueda ni el script de Google.

Ejemplo de señales observadas en esa página:

- `id="caseSummaryDiv"`
- `id="idCaseTitle"`
- acción hacia `civilCaseSummary.faces?cid=1`

Conclusión:

- esa página corresponde al resultado o detalle del caso;
- no sirve para determinar el flujo del submit de búsqueda;
- ahí no aparece el token del captcha porque el captcha ya ocurrió antes.

## Hallazgo clave: sí hay reCAPTCHA en `civilCaseSearch.faces`

Si logramos extaer datos sin embargo no son los esperados, ya que el reCaptcha no nos permite pasar, con el api FreeCaptchaBypass que es la solucion que se uso en el intento alterno.

```python
[6] county=ATLANTIC docket=000001/15
ViewState: ...
status=200
[HTML] -> output/html/07_search_results.html
Extrayendo pagina 1...
Total: 8 filas
```

## Intento alterno: arquitectura híbrida `curl_cffi + Playwright`

Además del enfoque de HTTP puro con validación estricta de respuesta, se probó una segunda arquitectura híbrida con el siguiente objetivo:

- usar `curl_cffi` para el login y el OTP;
- transferir la sesión autenticada al navegador;
- usar un navegador real para cargar el portal civil;
- llenar el formulario de búsqueda desde el contexto del browser;
- resolver la parte de `reCAPTCHA Enterprise` dentro de ese flujo;
- y extraer el `case summary` o la tabla de resultados desde una página ya renderizada.

La motivación de este enfoque fue que el login y el formulario de búsqueda parecían tener requisitos distintos:

- el login se comportaba mejor con `curl_cffi`;
- la búsqueda civil dependía de JavaScript del lado cliente;
- y el navegador era el único contexto donde se observaba el flujo completo del formulario.

### Arquitectura de este intento

La arquitectura híbrida se dividió en dos capas:

### Capa 1: `curl_cffi`

Se usó `curl_cffi` para:

- abrir el portal SAML;
- ir a `pkmslogin.form`;
- enviar credenciales;
- resolver el flujo OTP;
- obtener cookies de sesión válidas.

La idea era que `curl_cffi`, al impersonar un navegador a nivel de socket, se comportaba mejor que un cliente HTTP estándar durante el login.

### Capa 2: `Playwright`

Una vez autenticada la sesión, las cookies se transferían a Playwright para:

- abrir el portal civil;
- cargar `civilCaseSearch.faces`;
- esperar el JavaScript del formulario;
- llenar los campos del docket;
- ejecutar el flujo del formulario desde el browser;
- y capturar el HTML final para parsearlo.

## Resolución de `reCAPTCHA Enterprise` en este intento

En esta arquitectura híbrida sí se incluyó una integración con un servicio externo para resolver `reCAPTCHA v3 Enterprise`.

La función usada fue:

- `solve_recaptcha_fcb(...)`

que se apoyaba en la API de **FreeCaptchaBypass (FCB)**.

### Papel de `solve_recaptcha_fcb`

El flujo era:

1. obtener la `site key` del formulario civil;
2. usar la acción correcta observada en el HTML y JavaScript de la página;
3. solicitar a FreeCaptchaBypass un token válido;
4. inyectar ese token en el campo oculto:
   - `searchByDocForm:recaptchaResponse`
5. disparar el submit real del formulario.

### Parámetros relevantes observados

En las pruebas se identificó que el formulario usaba:

- `site key`: `6LeSprIqAAAAACbw4xnAsXH42Q4mfXk6t2MB09dq`
- `pageAction`: `CivilSearch`
- campo oculto: `searchByDocForm:recaptchaResponse`

La implementación de `solve_recaptcha_fcb(...)` enviaba esos datos a FreeCaptchaBypass para pedir un token de `ReCaptchaV3EnterpriseTaskProxyLess`.

### Uso dentro del intento híbrido

Dentro de `search_civil_case(...)`, la lógica era aproximadamente:

- si había `fcb_api_key`, usar `solve_recaptcha_fcb(...)`;
- inyectar el token en `searchByDocForm:recaptchaResponse`;
- disparar `searchByDocForm:btnSearch` mediante JavaScript;
- y solo si eso fallaba, intentar un flujo alterno apoyado en el navegador.

### Por qué esto importa en el análisis

Es importante dejarlo documentado porque este intento híbrido no fue solamente:

- `curl_cffi` para login;
- `Playwright` para navegación.

También dependía de una tercera pieza crítica:

- **FreeCaptchaBypass**, usada para obtener el token de `reCAPTCHA Enterprise` durante la búsqueda.

Sin esa parte, el intento híbrido no quedaría descrito correctamente.

## Componentes explorados en esa arquitectura

Durante ese intento también se exploraron varias piezas auxiliares:

- `Playwright` para navegación y extracción;
- `seleniumbase` en modo CDP para lanzar Chrome y luego adjuntar Playwright;
- `FreeCaptchaBypass` mediante `solve_recaptcha_fcb(...)` para `reCAPTCHA Enterprise`;
- almacenamiento de sesión en `session_latest.json`;
- transferencia de cookies entre `curl_cffi` y el navegador;
- guardado de screenshots y HTML por paso para depuración.

## Hallazgo importante en ese intento

El comportamiento no fue estable en todos los modos de ejecución.

### Resultado observado

Ese enfoque híbrido llegó a funcionar de forma parcial o consistente únicamente cuando la configuración estaba en: "headless": True

Se lograron extraer los datos

```python
[
  {
    "docket_number": "ATL-L-000001-15",
    "Case Caption": "Tiffin Betty Vs Daiichi Sankyo Inc",
    "Court": "Civil Part",
    "Venue": "Atlantic",
    "Case Initiation Date": "12/31/2014",
    "Case Type": "Olmesartan Medoxomil Medications/Benicar",
    "Case Status": "Closed",
    "Jury Demand": "6 Jurors",
    "Case Track": "4",
    "Judge": "Nelson C Johnson",
    "Team": "1",
    "# of Discovery Days": "537",
    "Age of Case": "00 YR 00 MO",
    "Original Discovery End Date": "05/08/2016",
    "Current Discovery End Date": "08/03/2016",
    "# of DED Extensions": "1",
    "Original Arbitration Date": "",
    "Current Arbitration Date": "",
    "# of Arb Adjournments": "0",
    "Original Trial Date": "",
    "Trial Date": "",
    "# of Trial Date Adjournments": "0",
    "Disposition Date": "10/01/2018",
    "Case Disposition": "Sett-Not Sched Trial,Arbitr, Or Othr Cdr/Friend Hear Not Compl",
    "Consolidated Case": "N",
    "Statewide Lien": ""
  }
]
```
