"""
NJ Courts Civil Case Scraper — Pure curl_cffi (HTTP)
=====================================================

Cloudflare/Incapsula bypassed by impersonating Safari TLS fingerprint
via curl_cffi. reCAPTCHA v3 Enterprise solved externally via
FreeCaptchaBypass API. No browser (Playwright/Selenium) required.

FLUJO:
  1. Probar targets de impersonación hasta encontrar uno que pase Incapsula.
  2. Login IBM ISAM (pkmslogin.form) con credenciales + 2FA OTP.
  3. GET formulario civil -> extraer ViewState JSF.
  4. Resolver reCAPTCHA v3 Enterprise via FCB API.
  5. POST formulario con campos + token reCAPTCHA.
  6. Parsear resultados con BeautifulSoup.
"""

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

# ─────────────────────────────────────────────────────────────
#  LOGGER
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("njcourts")

# ─────────────────────────────────────────────────────────────
#  CONFIGURACION
# ─────────────────────────────────────────────────────────────
USER = "CarlosOrtizL"
PASSWORD = "Abcd123456789@"

CONFIG = {
    "username": USER,
    "password": PASSWORD,
    "portal_url": "https://portal-cloud.njcourts.gov/prweb/PRAuth/CloudSAMLAuth?AppName=ESSO",
    "timeout": 30,
    "output_dir": "./output",
    "save_html": True,
    # FreeCaptchaBypass API key para resolver reCAPTCHA v3 Enterprise.
    "fcb_api_key": "3d6be63fcc0b8a4482803636f872425f",
}

CIVIL_SEARCH_URL = "https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces"

RECAPTCHA_SITE_KEY = "6LeSprIqAAAAACbw4xnAsXH42Q4mfXk6t2MB09dq"

COUNTY_CODES = {
    "ATLANTIC": "ATL",
    "BERGEN": "BER",
    "BURLINGTON": "BUR",
    "CAMDEN": "CAM",
    "CAPE MAY": "CPM",
    "CUMBERLAND": "CUM",
    "ESSEX": "ESX",
    "GLOUCESTER": "GLO",
    "HUDSON": "HUD",
    "HUNTERDON": "HNT",
    "MERCER": "MER",
    "MIDDLESEX": "MID",
    "MONMOUTH": "MON",
    "MORRIS": "MRS",
    "OCEAN": "OCN",
    "PASSAIC": "PAS",
    "SALEM": "SLM",
    "SOMERSET": "SOM",
    "SUSSEX": "SSX",
    "UNION": "UNN",
    "WARREN": "WRN",
}
COURT_VALUES = {
    "Civil Part": "LCV",
    "General Equity": "CHC",
    "Special Civil Part": "SCP",
    "Tax": "TAX",
}

SAFARI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

_IMPERSONATE_TARGETS = [
    "safari18_0",
    "safari15_5",
    "safari15_3",
    "safari17_0",
    "firefox133",
]


# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────
def make_output_dir():
    out = Path(CONFIG["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "html").mkdir(exist_ok=True)
    return out


def write_html(name, html, out):
    if CONFIG["save_html"]:
        path = out / "html" / f"{name}.html"
        path.write_text(html, encoding="utf-8")
        log.info(f"  [HTML] -> {path}")


def extract_form_fields(soup):
    """Extracts form fields exactly as a browser would."""
    fields = {}
    for inp in soup.find_all("input", {"name": True}):
        itype = inp.get("type", "text").lower()
        if itype in ("image", "submit", "button"):
            continue
        if itype == "checkbox":
            if inp.has_attr("checked"):
                fields[inp["name"]] = inp.get("value", "on")
        else:
            fields[inp["name"]] = inp.get("value", "")
    for sel in soup.find_all("select", {"name": True}):
        selected = sel.find("option", {"selected": True})
        fields[sel["name"]] = selected.get("value", "") if selected else ""
    return fields


# ─────────────────────────────────────────────────────────────
#  HTTP SESSION — self-healing impersonation probe
# ─────────────────────────────────────────────────────────────
def get_http_session(probe_url: str | None = None) -> cffi_requests.Session:
    """Creates a curl_cffi session. Probes impersonation targets against
    probe_url until one returns 200 (passes Incapsula/Cloudflare)."""
    if probe_url:
        for target in _IMPERSONATE_TARGETS:
            try:
                session = cffi_requests.Session(impersonate=target)
                resp = session.get(probe_url, timeout=15, allow_redirects=True)

                if (
                    resp.status_code == 200
                    and "Pardon Our Interruption" not in resp.text
                ):
                    log.info(f"[HTTP] Using impersonation target: {target}")
                    return session
            except Exception as e:
                log.debug(f"  [HTTP] Probe failed for {target}: {e}")
                continue

    # Fallback
    log.info(f"[HTTP] All probes failed. Falling back to {_IMPERSONATE_TARGETS[0]}")
    session = cffi_requests.Session(impersonate=_IMPERSONATE_TARGETS[0])
    return session


from playwright.sync_api import sync_playwright  # noqa: E402


def get_recaptcha_token_headless(website_url, website_key, page_action):
    """
    Genera un token de reCAPTCHA v3 Enterprise de forma invisible
    usando un navegador real en segundo plano.
    """
    log.info(f"  [Local-Solver] Iniciando navegador invisible para {page_action}...")

    with sync_playwright() as p:
        # Lanzamos el navegador en modo headless (sin ventana)
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # Vamos a la URL para que el script de Google tenga el "Origin" correcto
        page.goto(website_url, wait_until="networkidle")

        log.info("  [Local-Solver] Ejecutando script de Google...")

        # Inyectamos el comando de ejecución de Enterprise
        # Usamos un timeout por si el script de Google tarda en cargar
        token = page.evaluate(f"""
            async () => {{
                if (typeof grecaptcha === 'undefined' || !grecaptcha.enterprise) {{
                    return "ERROR: grecaptcha no cargado";
                }}
                return await grecaptcha.enterprise.execute('{website_key}', {{action: '{page_action}'}});
            }}
        """)

        browser.close()

        if "ERROR" in token:
            raise RuntimeError(f"No se pudo generar el token localmente: {token}")

        log.info(f"  [Local-Solver] Token generado con éxito ({len(token)} caracteres)")
        return token


# ─────────────────────────────────────────────────────────────
#  FREE CAPTCHA BYPASS — reCAPTCHA v3 Enterprise
# ─────────────────────────────────────────────────────────────
def solve_recaptcha_fcb(
    website_url: str,
    website_key: str,
    page_action: str = "",
) -> str:
    """Llamada a FreeCaptchaBypass con manejo de errores robusto."""
    api_key = CONFIG.get("fcb_api_key", "")
    if not api_key:
        raise ValueError("fcb_api_key no configurado en CONFIG")

    create_url = "https://freecaptchabypass.com/createTask"
    result_url = "https://freecaptchabypass.com/getTaskResult"

    task = {
        "type": "ReCaptchaV3EnterpriseTaskProxyLess",
        "websiteURL": website_url,
        "websiteKey": website_key,
        "minScore": 0.7,
    }
    if page_action:
        task["pageAction"] = page_action

    log.info(f"  [FCB] Creando tarea para {page_action}...")

    # Usamos headers genéricos para la API para evitar bloqueos por impersonación
    api_headers = {"Content-Type": "application/json"}

    try:
        r = cffi_requests.post(
            create_url, json={"clientKey": api_key, "task": task}, headers=api_headers
        )

        # Si no es 200, imprimimos el error para saber qué pasa
        if r.status_code != 200:
            log.error(f"  [FCB] Error API (Status {r.status_code}): {r.text}")
            raise RuntimeError(f"API de Captcha devolvió status {r.status_code}")

        resp = r.json()
    except Exception as e:
        log.error(f"  [FCB] Error crítico en createTask: {e}")
        if "r" in locals():
            log.debug(f"  [FCB] Respuesta cruda: {r.text}")
        raise

    if resp.get("errorId"):
        raise RuntimeError(f"FCB createTask error: {resp.get('errorDescription')}")

    task_id = resp["taskId"]
    log.info(f"  [FCB] taskId={task_id} — esperando solución...")

    for attempt in range(45):  # Un poco más de tiempo por si acaso
        time.sleep(2)
        try:
            r = cffi_requests.post(
                result_url,
                json={"clientKey": api_key, "taskId": task_id},
                headers=api_headers,
            )
            data = r.json()
        except Exception:
            continue  # Reintentar si el JSON falla a mitad de camino

        if data.get("status") == "ready":
            token = data["solution"]["gRecaptchaResponse"]
            log.info(f"  [FCB] Token obtenido con éxito ({len(token)} chars)")
            return token

        if data.get("errorId"):
            raise RuntimeError(
                f"FCB getTaskResult error: {data.get('errorDescription')}"
            )

    raise TimeoutError("FCB: El servidor no entregó la solución a tiempo (45s)")


# ─────────────────────────────────────────────────────────────
#  CAPA 1: curl_cffi — Login IBM ISAM con TLS fingerprint Safari
# ─────────────────────────────────────────────────────────────
def cffi_login(http: cffi_requests.Session, out, otp_code=""):
    """
    Login flow:
      1. GET portal SAML -> inicializa cookies Incapsula
      2. GET pkmslogin.form -> formulario IBM ISAM
      3. POST credenciales
      4. 2FA OTP si es requerido
    Returns the authenticated session.
    """
    # ── Paso 1: GET portal SAML ──────────────────────────────────
    log.info(f"[1] GET {CONFIG['portal_url']}")
    resp0 = http.get(
        CONFIG["portal_url"], timeout=CONFIG["timeout"], allow_redirects=True
    )
    log.info(f"  status={resp0.status_code}  url={resp0.url}")
    write_html("01_cffi_landing", resp0.text, out)

    # ── Paso 2: GET pagina de login IBM ISAM ─────────────────────
    idp_login_url = "https://portal.njcourts.gov/pkmslogin.form"
    log.info(f"[2] GET {idp_login_url}")
    resp_login = http.get(
        idp_login_url,
        timeout=CONFIG["timeout"],
        allow_redirects=True,
        headers={"Referer": resp0.url},
    )
    log.info(f"  status={resp_login.status_code}  url={resp_login.url}")
    write_html("01b_cffi_idp_login", resp_login.text, out)

    soup = BeautifulSoup(resp_login.text, "html.parser")
    soup0 = BeautifulSoup(resp0.text, "html.parser")

    def find_login_form(s):
        return (
            s.find("form", {"name": "LoginEntryForm"})
            or s.find("form", action=lambda a: a and "pkmslogin" in str(a))
            or (s.find("input", {"name": "username"}) and s.find("form"))
        )

    login_form = find_login_form(soup)
    if login_form:
        log.info("  Form encontrado en pkmslogin.form")
    else:
        login_form = find_login_form(soup0)
        if login_form:
            log.info("  Form encontrado en portal-cloud")
            soup = soup0
            resp_login = resp0
        else:
            t0 = soup0.title.string.strip() if soup0.title else "?"
            t1 = soup.title.string.strip() if soup.title else "?"
            log.error(f"  SAML page: title='{t0}'")
            log.error(f"  IDP page : title='{t1}'")
            raise RuntimeError(
                "No se encontro formulario de login.\n"
                "Revisa: output/html/01_cffi_landing.html y 01b_cffi_idp_login.html\n"
                "Ejecuta test_targets.py para diagnostico completo."
            )

    # ── Paso 3: Extraer action y campos del form ─────────────────
    form_action = login_form.get("action", "")
    if not form_action:
        raise RuntimeError("El form no tiene atributo action")
    if not form_action.startswith("http"):
        form_action = urljoin(resp_login.url, form_action)
    log.info(f"  Form action: {form_action}")

    user_inp = (
        soup.find("input", {"name": "username"})
        or soup.find("input", {"id": "userid"})
        or soup.find("input", {"name": "UserIdentifier"})
        or soup.find("input", attrs={"type": "text"})
    )
    pass_inp = (
        soup.find("input", {"name": "password"})
        or soup.find("input", {"id": "passwd"})
        or soup.find("input", {"name": "Password"})
        or soup.find("input", attrs={"type": "password"})
    )
    if not user_inp or not pass_inp:
        raise RuntimeError("No se encontraron campos usuario/password.")

    fields = {}
    for inp in login_form.find_all("input", {"name": True}):
        if inp.get("type", "").lower() in ("submit", "button", "image"):
            continue
        fields[inp["name"]] = inp.get("value", "")
    fields[user_inp["name"]] = CONFIG["username"]
    fields[pass_inp["name"]] = CONFIG["password"]

    # ── Paso 4: POST credenciales ─────────────────────────────────
    log.info(f"[3] POST {form_action}")
    resp2 = http.post(
        form_action,
        data=fields,
        timeout=CONFIG["timeout"],
        allow_redirects=True,
        headers={
            "Referer": resp_login.url,
            "Origin": "https://portal.njcourts.gov",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    log.info(f"  status={resp2.status_code}  url={resp2.url}")
    write_html("02_cffi_post_login", resp2.text, out)

    soup2 = BeautifulSoup(resp2.text, "html.parser")
    still_login = bool(
        soup2.find("form", {"name": "LoginEntryForm"})
        or soup2.find("input", {"name": "username"})
    )
    if still_login:
        if "Authentication Failed" in resp2.text or "invalid" in resp2.text.lower():
            raise RuntimeError("Credenciales incorrectas — Authentication Failed")
        raise RuntimeError(f"Login fallo — sigue en pagina de login. URL: {resp2.url}")

    # ── 2FA ────────────────────────────────────────────────────────
    is_2fa = (
        "choiceSelect" in resp2.text
        or "OTP" in resp2.text
        or "Two-Factor" in resp2.text
    )
    if is_2fa:
        log.info("[2FA] Pantalla 2FA detectada")
        resp2 = cffi_handle_2fa(http, resp2.url, soup2, otp_code, out)
        write_html("03_cffi_post_2fa", resp2.text, out)

    log.info(f"  URL final: {resp2.url}")
    cookies = [c.name for c in http.cookies.jar]
    log.info(f"  Cookies: {cookies}")
    log.info("  Autenticacion exitosa!")
    return http


def cffi_handle_2fa(http, url, soup, otp_code, out):
    """
    Maneja el flujo de 2FA usando headers de navegación para evadir Imperva.
    """

    # --- FASE 1: SELECCIÓN DE MÉTODO (Email vs SMS) ---
    selection_form = soup.find("form", action=re.compile(r"StateId="))
    choice_select = soup.find("select", {"id": "choiceSelect"})

    if choice_select and selection_form:
        log.info("[2FA-1] Pantalla de selección de método detectada.")

        selection_url = urljoin(url, selection_form.get("action"))
        fields = extract_form_fields(selection_form)

        # Forzamos los valores para asegurar el envío de Email
        fields["choice"] = "0"
        fields["operation"] = "verify"

        # HEADERS QUIRÚRGICOS: Estos imitan un clic real en el formulario
        navigation_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.8,en-US;q=0.5,en;q=0.3",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://portal-cloud.njcourts.gov",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Referer": url,
        }

        log.info(f"[2FA-1] Enviando selección (Email) a: {selection_url}")

        resp = http.post(
            selection_url,
            data=fields,
            headers=navigation_headers,  # <--- IMPORTANTE: Usamos los headers nuevos
            timeout=CONFIG["timeout"],
        )

        write_html("02b_cffi_2fa_method_selected", resp.text, out)
        soup = BeautifulSoup(resp.text, "html.parser")
        url = resp.url

    # --- FASE 2: INGRESO DEL CÓDIGO OTP ---

    verify_form = soup.find("form", action=re.compile(r"StateId="))

    if not verify_form:
        # Si aquí sale el error, revisa el HTML generado
        log.error("❌ No se encontró el formulario de OTP tras la selección.")
        log.error(f"URL de respuesta: {url}")
        raise RuntimeError(
            "Imperva bloqueó el POST de selección. Revisa 02b_cffi_2fa_method_selected.html"
        )

    # Extraer el HINT (el número de referencia)
    hint_span = soup.find("span", id="otpHintSpan")
    hint_text = hint_span.get_text(strip=True) if hint_span else "No disponible"

    action_url = urljoin(url, verify_form.get("action"))
    fields = extract_form_fields(verify_form)

    if not otp_code:
        print("\n" + "═" * 50)
        print(f"  REFERENCIA (HINT): {hint_text}")
        print("  El código fue enviado a tu correo.")
        print("═" * 50)
        otp_code = input("  -> Ingresa el código OTP: ").strip()

    # El campo en el HTML es 'otp.user.otp'
    fields["otp.user.otp"] = otp_code
    fields["operation"] = "verify"

    log.info(f"[2FA-2] Enviando código OTP a: {action_url}")

    # Para el envío del código también usamos los headers de navegación
    return http.post(
        action_url, data=fields, headers=navigation_headers, timeout=CONFIG["timeout"]
    )


# ─────────────────────────────────────────────────────────────
#  CAPA 2: Busqueda civil via HTTP POST (JSF + reCAPTCHA)
# ─────────────────────────────────────────────────────────────
def navigate_to_civil_search(http: cffi_requests.Session, out) -> BeautifulSoup:
    """GET al formulario civil. Primero visita ESSO portal para registrar
    la sesion, luego carga civilCaseSearch.faces."""

    # Registrar sesion en ESSO portal
    esso_url = "https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/"
    log.info(f"[4] GET {esso_url}")
    resp_esso = http.get(esso_url, timeout=CONFIG["timeout"], allow_redirects=True)
    log.info(f"  status={resp_esso.status_code}  url={resp_esso.url}")
    write_html("04_esso_portal", resp_esso.text, out)

    if "login" in resp_esso.url.lower() or "pkmslogin" in resp_esso.url:
        raise RuntimeError("Sesion invalida — redirigido a login tras ESSO portal")

    # Cargar formulario civil
    log.info(f"[5] GET {CIVIL_SEARCH_URL}")
    resp_civil = http.get(
        CIVIL_SEARCH_URL,
        timeout=CONFIG["timeout"],
        allow_redirects=True,
        headers={"Referer": esso_url},
    )
    log.info(f"  status={resp_civil.status_code}  url={resp_civil.url}")
    write_html("05_civil_search_form", resp_civil.text, out)

    if "login" in resp_civil.url.lower() or "pkmslogin" in resp_civil.url:
        raise RuntimeError("Sesion invalida — redirigido a login en civil search")

    soup = BeautifulSoup(resp_civil.text, "html.parser")

    # Verificar que el formulario JSF cargo
    form = soup.find("form", id="searchByDocForm") or soup.find(
        "form", id="civilCaseSearchForm"
    )
    if not form:
        raise RuntimeError(
            "Formulario civil no encontrado en la respuesta.\n"
            "Revisa: output/html/05_civil_search_form.html"
        )

    log.info("  Formulario civil cargado via HTTP")
    return soup


def search_civil_case(
    http: cffi_requests.Session,
    form_soup: BeautifulSoup,
    out,
    docket_num="000001",
    docket_year="15",
    court_type="Civil Part",
    county="ATLANTIC",
    docket_type="L",
) -> list:
    """Fills the JSF form via HTTP POST and returns parsed results."""
    log.info(f"\n[6] county={county} docket={docket_num}/{docket_year}")

    # Extraer todos los campos del formulario JSF (ViewState incluido)
    fields = extract_form_fields(form_soup)

    # Rellenar campos del formulario
    court_value = COURT_VALUES.get(court_type, "LCV")
    county_code = COUNTY_CODES.get(county.upper(), county)

    # IDs de campos JSF (confirmados en el HTML)
    field_mapping = {
        "civilCaseSearchForm:idDiv": court_value,
        "searchByDocForm:idCivilVenue": county_code,
        "searchByDocForm:docketType": docket_type,
        "searchByDocForm:idCivilDocketNum": docket_num,
        "searchByDocForm:idCivilDocketYear": docket_year,
    }

    for field_name, value in field_mapping.items():
        if field_name in fields:
            fields[field_name] = value
        else:
            fields[field_name] = value
            log.debug(f"  Campo {field_name} no existia en form — agregado")

    log.info(f"  Court: {court_type} ({court_value})")
    log.info(f"  County: {county} ({county_code})")
    log.info(f"  DocketType: {docket_type}")
    log.info(f"  DocketNum: {docket_num}")
    log.info(f"  DocketYear: {docket_year}")

    # ── Resolver reCAPTCHA v3 Enterprise via FCB ─────────────────
    log.info("  [6f] Resolviendo reCAPTCHA v3 Enterprise via FCB...")
    try:
        token = solve_recaptcha_fcb(
            website_url=CIVIL_SEARCH_URL,
            website_key=RECAPTCHA_SITE_KEY,
            page_action="CivilSearch",
        )
        fields["searchByDocForm:recaptchaResponse"] = token
        log.info(f"  Token reCAPTCHA inyectado ({len(token)} chars)")
    except Exception as e:
        log.error(f"  Error resolviendo reCAPTCHA: {e}")
        raise

    # ── Submit via btnSearch ─────────────────────────────────────
    # En JSF el submit se hace incluyendo el nombre del boton en el POST
    fields["searchByDocForm:btnSearch"] = "Search"

    log.info(f"  [6g] POST {CIVIL_SEARCH_URL}")
    resp = http.post(
        CIVIL_SEARCH_URL,
        data=fields,
        timeout=CONFIG["timeout"],
        allow_redirects=True,
        headers={
            "Referer": CIVIL_SEARCH_URL,
            "Origin": "https://portal.njcourts.gov",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    log.info(f"  status={resp.status_code}  url={resp.url}")
    write_html("07_search_results", resp.text, out)

    content = resp.text
    if "Captcha verification has failed" in content:
        log.error("  reCAPTCHA rechazado por el servidor")
        return []

    # Detectar tipo de respuesta
    if "caseSummaryDiv" in content or "idCaseTitle" in content:
        log.info("  Pagina de case summary detectada -> extrayendo campos")
        return [extract_case_summary(content)]

    return extract_table_data_http(http, content, out)


# ─────────────────────────────────────────────────────────────
#  EXTRACCION DE RESULTADOS (BeautifulSoup)
# ─────────────────────────────────────────────────────────────
def extract_case_summary(html: str) -> dict:
    """Extracts case summary fields from civilCaseSummary.faces HTML."""
    soup = BeautifulSoup(html, "html.parser")
    data = {}

    # Docket Number — 4 spans: venue + type + seq + year
    venue = soup.find(id="docVenueTitleDC") or {}
    venue = venue.get_text(strip=True) if hasattr(venue, "get_text") else ""
    dtype = soup.find(id="docTypeCodeTitle") or {}
    dtype = dtype.get_text(strip=True) if hasattr(dtype, "get_text") else ""
    seq = soup.find(id="docSeqNumTitle") or {}
    seq = seq.get_text(strip=True) if hasattr(seq, "get_text") else ""
    year = soup.find(id="docYeaerTitle") or {}
    year = year.get_text(strip=True) if hasattr(year, "get_text") else ""
    parts = [p for p in [venue, dtype, seq, year] if p]
    data["docket_number"] = "-".join(parts)

    el = soup.find(id="idCaseTitle")
    data["Case Caption"] = el.get_text(strip=True) if el else ""

    label_map = [
        ("Court", "Court:"),
        ("Venue", "Venue:"),
        ("Case Initiation Date", "Case Initiation Date:"),
        ("Case Type", "Case Type:"),
        ("Case Status", "Case Status:"),
        ("Jury Demand", "Jury Demand:"),
        ("Case Track", "Case Track:"),
        ("Judge", "Judge:"),
        ("Team", "Team:"),
        ("# of Discovery Days", "# of Discovery Days:"),
        ("Age of Case", "Age of Case:"),
        ("Original Discovery End Date", "Original Discovery End Date:"),
        ("Current Discovery End Date", "Current Discovery End Date:"),
        ("# of DED Extensions", "# of DED Extensions:"),
        ("Original Arbitration Date", "Original Arbitration Date:"),
        ("Current Arbitration Date", "Current Arbitration Date:"),
        ("# of Arb Adjournments", "# of Arb Adjournments:"),
        ("Original Trial Date", "Original Trial Date:"),
        ("Trial Date", "Trial Date:"),
        ("# of Trial Date Adjournments", "# of Trial Date Adjournments:"),
        ("Disposition Date", "Disposition Date:"),
        ("Case Disposition", "Case Disposition:"),
    ]
    for field_name, label_text in label_map:
        vf = soup.find(
            "span",
            class_="ValueField",
            string=lambda s, lt=label_text: s and s.strip().rstrip() == lt,
        )
        if vf:
            lf = vf.find_next_sibling("span", class_="LabelField")
            data[field_name] = lf.get_text(strip=True) if lf else ""
        else:
            data[field_name] = ""

    el = soup.find(id=lambda x: x and "consolidatedCaseN" in x)
    data["Consolidated Case"] = el.get_text(strip=True) if el else ""

    el = soup.find(id=lambda x: x and "jdgmntStatewideLien" in x)
    data["Statewide Lien"] = el.get_text(strip=True) if el else ""

    return data


def extract_table_data_http(http: cffi_requests.Session, html: str, out) -> list:
    """Extracts result rows from the table via BeautifulSoup, handling pagination."""
    all_rows = []
    page_num = 1

    while True:
        log.info(f"  Extrayendo pagina {page_num}...")
        soup = BeautifulSoup(html, "html.parser")

        # Extraer headers
        headers = []
        for th in soup.select("table th"):
            text = th.get_text(strip=True)
            if text:
                headers.append(text)

        # Extraer filas
        page_rows = []
        for row in soup.select("tbody tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            values = [c.get_text(strip=True) for c in cells]
            if not any(values):
                continue
            if headers and len(headers) == len(values):
                rd = dict(zip(headers, values))
            else:
                rd = {f"col_{i}": v for i, v in enumerate(values)}

            # Extraer links de la fila
            hrefs = []
            for a in row.find_all("a", href=True):
                href = a.get("href", "")
                if href:
                    hrefs.append(href)
            if hrefs:
                rd["_links"] = "; ".join(hrefs)

            page_rows.append(rd)

        if not page_rows:
            if page_num == 1:
                log.warning(
                    "  No se encontraron filas -> revisa output/html/07_search_results.html"
                )
            break

        log.info(f"    -> {len(page_rows)} filas")
        all_rows.extend(page_rows)

        # Buscar link de paginacion "Next"
        next_link = None
        for a in soup.find_all("a"):
            text = a.get_text(strip=True).lower()
            if text == "next" and not a.get("disabled"):
                next_link = a
                break

        if not next_link:
            break

        # Navegar a siguiente pagina via POST (JSF postback)
        href = next_link.get("href", "")
        postback_match = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href)
        onclick = next_link.get("onclick", "")

        if postback_match:
            fields = extract_form_fields(soup)
            fields["__EVENTTARGET"] = postback_match.group(1)
            fields["__EVENTARGUMENT"] = postback_match.group(2)
        elif "mojarra.jsfcljs" in onclick or "jsf" in href.lower():
            # JSF pagination — submit the form with the link's id
            fields = extract_form_fields(soup)
            link_id = next_link.get("id", "")
            if link_id:
                fields[link_id] = link_id
        else:
            # Direct link — GET
            next_url = (
                href if href.startswith("http") else urljoin(CIVIL_SEARCH_URL, href)
            )
            resp = http.get(
                next_url,
                timeout=CONFIG["timeout"],
                headers={"Referer": CIVIL_SEARCH_URL},
            )
            html = resp.text
            page_num += 1
            continue

        resp = http.post(
            CIVIL_SEARCH_URL,
            data=fields,
            timeout=CONFIG["timeout"],
            allow_redirects=True,
            headers={
                "Referer": CIVIL_SEARCH_URL,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        html = resp.text
        page_num += 1

    log.info(f"  Total: {len(all_rows)} filas")
    return all_rows


def export_results(data, out):
    if not data:
        print("\n No hay datos para exportar.")
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_p = out / f"docket_{ts}.json"
    json_p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"\n[EXPORT] JSON -> {json_p}")
    print(f"{len(data)} casos exportados")


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def main(otp_code=""):
    out = make_output_dir()

    # ── Crear sesion HTTP con impersonacion Safari ───────────────
    print("\n[=== curl_cffi: TLS fingerprint Safari ===]")
    http = get_http_session(probe_url=CONFIG["portal_url"])

    # ── Login IBM ISAM ───────────────────────────────────────────
    try:
        http = cffi_login(http, out, otp_code=otp_code)
    except RuntimeError as e:
        print(f"\n Login fallo: {e}")
        return

    # ── Navegar al formulario civil ──────────────────────────────
    print("\n[=== Busqueda civil via HTTP ===]")
    try:
        form_soup = navigate_to_civil_search(http, out)
    except RuntimeError as e:
        print(f"\n No se pudo cargar formulario civil: {e}")
        return

    # ── Ejecutar busqueda ────────────────────────────────────────
    try:
        data = search_civil_case(http, form_soup, out)
    except Exception as e:
        print(f"\n Error en busqueda: {e}")
        return

    export_results(data, out)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="NJ Courts Scraper — Pure curl_cffi (HTTP)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python scraper_v1.py              # pide OTP en consola
  python scraper_v1.py --otp 847291 # OTP directo

Si el login falla: ejecuta test_targets.py para encontrar un target valido.
        """,
    )
    parser.add_argument("--otp", default="", help="Codigo OTP 2FA")
    args = parser.parse_args()
    main(otp_code=args.otp)
