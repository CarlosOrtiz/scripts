"""
NJ Courts Civil Case Scraper — Hybrid curl_cffi + Playwright
=============================================================

ARQUITECTURA:
  CAPA 1 — curl_cffi (TLS fingerprinting Safari a nivel de socket)
    El IBM ISAM detecta bots por la huella TLS del handshake (JA3/JA4).
    Playwright/Chromium siempre tiene la misma huella TLS de Chromium aunque
    uses stealth JS — el ISAM lo bloquea (pkmslogin.form).
    curl_cffi impersona Safari 15.5 a nivel de socket, pasando el ISAM.
    Se usa para: GET portal SAML -> POST credenciales -> 2FA OTP.

  CAPA 2 — Playwright headless=True (con cookies de curl_cffi inyectadas)
    Una vez autenticado con curl_cffi, las cookies de sesion se transfieren
    a Playwright. Playwright carga el portal civil, ejecuta grecaptcha en
    el contexto real del browser, llena el formulario JSF y extrae datos.
"""

import asyncio
import json
import logging
import random as _rnd
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from playwright.async_api import async_playwright
from playwright_recaptcha import recaptchav3
from seleniumbase import cdp_driver

# ─────────────────────────────────────────────────────────────
#  LOGGER
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),  # consola unicamente
    ],
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
    "pw_timeout": 30000,
    "output_dir": "./output",
    "save_html": True,
    "save_screenshots": True,
    "headless": False,  # True = sin ventana (produccion) | False = ver browser (debug)
    # Safari pasa IBM ISAM. Chrome es bloqueado por TLS fingerprint.
    # Si falla, ejecuta test_targets.py para encontrar uno nuevo.
    "cffi_target": "safari15_5",
    # FreeCaptchaBypass API key para resolver reCAPTCHA v3 Enterprise.
    # Registrate en https://freecaptchabypass.com/cp/
    # Si esta vacio se usa el flujo nativo del browser (playwright-recaptcha).
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
        "Version/15.5 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────
def make_output_dir():
    out = Path(CONFIG["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "html").mkdir(exist_ok=True)
    (out / "screenshots").mkdir(exist_ok=True)
    return out


def write_html(name, html, out):
    if CONFIG["save_html"]:
        path = out / "html" / f"{name}.html"
        path.write_text(html, encoding="utf-8")
        print(f"  [HTML] -> {path}")


async def save_html(page, name, out):
    html = await page.content()
    write_html(name, html, out)
    return html


async def screenshot(page, name, out):
    if CONFIG["save_screenshots"]:
        path = out / "screenshots" / f"{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        print(f"  [IMG]  -> {path}")


# ─────────────────────────────────────────────────────────────
#  FREE CAPTCHA BYPASS — reCAPTCHA v3 Enterprise (resolucion externa)
#  https://freecaptchabypass.com/developers/
# ─────────────────────────────────────────────────────────────
def _fcb_sync(
    api_key: str, website_url: str, website_key: str, page_action: str = ""
) -> str:
    """Llama a la API de FreeCaptchaBypass de forma sincrona (en thread executor)."""
    import time

    create_url = "https://freecaptchabypass.com/createTask"
    result_url = "https://freecaptchabypass.com/getTaskResult"

    task = {
        "type": "ReCaptchaV3EnterpriseTaskProxyLess",
        "websiteURL": website_url,
        "websiteKey": website_key,
        "minScore": 0.7,  # pedir score alto para que el servidor no rechace
    }
    if page_action:
        task["pageAction"] = page_action

    r = cffi_requests.post(create_url, json={"clientKey": api_key, "task": task})
    r.raise_for_status()
    resp = r.json()
    if resp.get("errorId"):
        raise RuntimeError(f"FCB createTask error: {resp.get('errorDescription')}")

    task_id = resp["taskId"]
    log.info(f"  [FCB] taskId={task_id} — esperando solucion...")

    for attempt in range(40):  # max ~40s
        time.sleep(1)
        r = cffi_requests.post(
            result_url, json={"clientKey": api_key, "taskId": task_id}
        )
        data = r.json()
        if data.get("status") == "ready":
            token = data["solution"]["gRecaptchaResponse"]
            log.info(f"  [FCB] Token obtenido en {attempt + 1}s ({len(token)} chars)")
            return token
        if data.get("errorId"):
            raise RuntimeError(
                f"FCB getTaskResult error: {data.get('errorDescription')}"
            )

    raise TimeoutError("FCB: sin respuesta en 40s")


async def solve_recaptcha_fcb(
    website_url: str,
    website_key: str,
    page_action: str = "",
) -> str:
    """Wrapper async de _fcb_sync. Corre en thread executor para no bloquear el loop."""
    api_key = CONFIG.get("fcb_api_key", "")
    if not api_key:
        raise ValueError("fcb_api_key no configurado en CONFIG")
    return await asyncio.to_thread(
        _fcb_sync, api_key, website_url, website_key, page_action
    )


SESSION_PATH = Path("./session_latest.json")


async def save_session(context, out=None):
    """Guarda cookies + localStorage. Escribe en session_latest.json (reutilizable)."""
    state = await context.storage_state()
    # Ruta fija para reutilizar en la siguiente ejecucion
    SESSION_PATH.write_text(json.dumps(state, indent=2))
    if out:
        (out / "session.json").write_text(json.dumps(state, indent=2))
    log.info(
        f"  [SESSION] Guardada -> {SESSION_PATH} ({len(state.get('cookies', []))} cookies)"
    )


async def load_session(context):
    """Carga cookies de la sesion anterior para que reCAPTCHA vea un browser con historial."""
    if not SESSION_PATH.exists():
        log.info("  [SESSION] Sin sesion previa — primera ejecucion")
        return
    try:
        state = json.loads(SESSION_PATH.read_text())
        cookies = state.get("cookies", [])
        if cookies:
            await context.add_cookies(cookies)
            log.info(f"  [SESSION] {len(cookies)} cookies de sesion anterior cargadas")
    except Exception as e:
        log.warning(f"  [SESSION] No se pudo cargar sesion previa: {e}")


def cffi_cookies_to_playwright(http):
    """
    Convierte cookies de curl_cffi a formato Playwright.
    Duplica cada cookie en ambos dominios relevantes:
      portal-cloud.njcourts.gov  (SAML / ESSO portal)
      portal.njcourts.gov        (civil case search)
    para que Playwright tenga sesion activa en ambos.
    """
    DOMAINS = [
        ".njcourts.gov",  # wildcard — cubre todos los subdominios
        "portal.njcourts.gov",
        "portal-cloud.njcourts.gov",
    ]
    pw = []
    seen = set()
    for c in http.cookies.jar:
        for domain in DOMAINS:
            key = (c.name, domain)
            if key in seen:
                continue
            seen.add(key)
            entry = {
                "name": c.name,
                "value": c.value,
                "domain": domain,
                "path": c.path or "/",
                "secure": bool(c.secure),
                "httpOnly": False,
                "sameSite": "Lax",
            }
            if c.expires:
                entry["expires"] = float(c.expires)
            pw.append(entry)
    return pw


def extract_form_fields(soup):
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
#  BOOTSTRAP: Playwright resuelve el challenge Incapsula/Imperva
#  antes de que curl_cffi inicie su sesion.
#  Incapsula requiere ejecutar JS para setear cookies (reese84,
#  incap_ses_*, visid_incap_*). curl_cffi no ejecuta JS, por
#  eso el GET al portal devuelve "Pardon Our Interruption".
#  Solucion: Playwright carga la pagina, espera el challenge,
#  extrae las cookies Incapsula y las inyecta en curl_cffi.
# ─────────────────────────────────────────────────────────────
async def bootstrap_incapsula_cookies(urls: list[str]) -> list[dict]:
    """
    Visita cada URL en la lista con Playwright headless, resuelve el challenge
    JS de Incapsula en cada dominio y devuelve todas las cookies combinadas.

    Cada subdominio de njcourts.gov tiene su propio site-ID de Incapsula
    (ej. portal-cloud tiene 2548127, portal puede tener otro ID distinto).
    Las cookies de un subdominio NO son validas para otro, por eso se visitan
    todos los dominios que se usaran mas adelante.
    """
    log.info("[INCAPSULA] Iniciando bootstrap con Playwright...")
    all_cookies: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=CONFIG["headless"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                # En headless, ocultar senales que Incapsula/hCaptcha detectan
                "--disable-features=IsolateOrigins,site-per-process",
                "--window-size=1280,840",
            ],
        )
        context = await browser.new_context(
            user_agent=SAFARI_HEADERS["User-Agent"],
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = await context.new_page()

        try:
            for url in urls:
                log.info(f"[INCAPSULA] GET {url}")
                try:
                    await page.goto(url, wait_until="load", timeout=30000)
                except Exception:
                    try:
                        await page.wait_for_load_state(
                            "domcontentloaded", timeout=10000
                        )
                    except Exception:
                        pass

                # Esperar a que Incapsula resuelva su challenge JS
                for _ in range(12):
                    title = await page.title()
                    content = await page.content()
                    blocked = (
                        "Pardon Our Interruption" in title
                        or "Pardon Our Interruption" in content
                        or "SWUDNSAI" in content  # bloqueo duro Incapsula
                    )
                    if not blocked:
                        log.info(
                            f"[INCAPSULA] Challenge resuelto: '{title}' ({url[:50]})"
                        )
                        break
                    log.info(f"[INCAPSULA] Esperando challenge en {url[:50]}...")
                    await page.wait_for_timeout(2000)

                await human_wander(page)
                await human_delay(page, 500, 900)
                # En headless esperar extra para que hCaptcha termine de resolver
                if CONFIG.get("headless"):
                    await page.wait_for_timeout(4000)

            # Recolectar todas las cookies del contexto (todos los dominios visitados)
            all_cookies = await context.cookies()
            incap = [
                c
                for c in all_cookies
                if any(
                    k in c["name"].lower()
                    for k in ("reese", "incap", "visid", "_imp", "nlbi")
                )
            ]
            log.info(
                f"[INCAPSULA] {len(incap)} cookies Incapsula: {[c['name'] + '@' + c['domain'] for c in incap]}"
            )
            log.info(f"[INCAPSULA] Total cookies: {len(all_cookies)}")

        except Exception as e:
            log.error(f"[INCAPSULA] Error en bootstrap: {e}")
        finally:
            await context.close()
            await browser.close()

    return all_cookies


def inject_playwright_cookies_into_cffi(http, pw_cookies: list[dict]):
    """Inyecta cookies de Playwright en una sesion curl_cffi."""
    from http.cookiejar import Cookie
    import time

    for c in pw_cookies:
        cookie = Cookie(
            version=0,
            name=c["name"],
            value=c["value"],
            port=None,
            port_specified=False,
            domain=c.get("domain", ""),
            domain_specified=bool(c.get("domain")),
            domain_initial_dot=c.get("domain", "").startswith("."),
            path=c.get("path", "/"),
            path_specified=True,
            secure=c.get("secure", False),
            expires=int(c["expires"]) if c.get("expires") else int(time.time()) + 3600,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
        )
        http.cookies.jar.set_cookie(cookie)


# ─────────────────────────────────────────────────────────────
#  CAPA 1: curl_cffi — Login con TLS fingerprint Safari
# ─────────────────────────────────────────────────────────────
def cffi_login(out, otp_code="", incapsula_cookies: list | None = None):
    """
    Flujo real del portal NJ Courts (IBM ISAM / SAML):

    El GET al portal SAML devuelve 200 pero NO hace redirect HTTP al IBM ISAM
    — el redirect es via JavaScript (setup() en onload del body). curl_cffi
    no ejecuta JS, por eso la URL final sigue siendo la del portal SAML.

    Solucion: ir directamente a la pagina de login del IBM ISAM que conocemos
    del HTML (portal.njcourts.gov con el form action pkmslogin.form).
    El portal SAML inicial solo sirve para inicializar las cookies de Incapsula
    (proteccion DDoS) — despues vamos directo al IdP.
    """
    from urllib.parse import urljoin

    http = cffi_requests.Session(impersonate=CONFIG["cffi_target"])
    http.headers.update(SAFARI_HEADERS)

    # ── Pre-paso: inyectar cookies Incapsula obtenidas por Playwright ──────
    if incapsula_cookies:
        inject_playwright_cookies_into_cffi(http, incapsula_cookies)
        log.info(
            f"[curl_cffi] {len(incapsula_cookies)} cookies Incapsula pre-inyectadas"
        )

    log.info(f"[curl_cffi] Target: {CONFIG['cffi_target']}")

    # ── Paso 1: GET al portal SAML — inicializa cookies Incapsula ─────────
    log.info(f"[1] GET {CONFIG['portal_url']} (inicializar cookies)")
    resp0 = http.get(
        CONFIG["portal_url"], timeout=CONFIG["timeout"], allow_redirects=True
    )
    log.info(f"  status={resp0.status_code}  url={resp0.url}")
    write_html("01_cffi_landing", resp0.text, out)

    # ── Paso 2: GET directo a la pagina de login del IBM ISAM ─────────────
    # Del HTML del portal sabemos que la pagina de login esta en portal.njcourts.gov
    # El body tiene onload="setup('AMOS0001', 'https', 'portal.njcourts.gov', ...)"
    # que indica el host del IdP. La pagina de login es /pkmslogin.form pero
    # el GET a esa URL nos da el HTML con el formulario.
    idp_login_url = "https://portal.njcourts.gov/pkmslogin.form"
    log.info(f"[2] GET {idp_login_url} (pagina de login IBM ISAM)")
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

    # Buscar form: primero en pkmslogin.form (paso 2), luego en portal-cloud (paso 1)
    login_form = find_login_form(soup)
    if login_form:
        log.info("  Form encontrado en pkmslogin.form")
    else:
        login_form = find_login_form(soup0)
        if login_form:
            log.info("  Form encontrado en portal-cloud (actua como IdP proxy)")
            soup = soup0
            resp_login = resp0
        else:
            # Diagnostico detallado
            t0 = soup0.title.string.strip() if soup0.title else "?"
            t1 = soup.title.string.strip() if soup.title else "?"
            forms0 = [f.get("action", "?")[:60] for f in soup0.find_all("form")]
            forms1 = [f.get("action", "?")[:60] for f in soup.find_all("form")]
            log.error(f"  SAML page: title='{t0}' forms={forms0}")
            log.error(f"  IDP page : title='{t1}' forms={forms1}")
            log.error("  CAUSA MAS PROBABLE: IP de datacenter bloqueada por Incapsula")
            log.error("  SOLUCION: usar IP residencial o VPN residencial")
            raise RuntimeError(
                "No se encontro formulario de login.\n"
                "Revisa: output/html/01_cffi_landing.html y 01b_cffi_idp_login.html\n"
                "Ejecuta test_targets.py para diagnostico completo."
            )

    # ── Paso 3: Extraer action y campos del form ───────────────────────────
    form_action = login_form.get("action", "")
    if not form_action:
        raise RuntimeError("El form no tiene atributo action")
    if not form_action.startswith("http"):
        form_action = urljoin(resp_login.url, form_action)
    log.info(f"  Form action: {form_action}")

    # Campos del form (del HTML real):
    #   name="username"  id="userid"      <- User ID
    #   name="password"  id="passwd"      <- Password
    #   name="login-form-type" value="pwd" <- hidden
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
        raise RuntimeError(
            "No se encontraron campos usuario/password.\n"
            "Revisa: output/html/01b_cffi_idp_login.html"
        )

    print(f"  Usuario : name={user_inp['name']} id={user_inp.get('id', '')}")
    print(f"  Password: name={pass_inp['name']} id={pass_inp.get('id', '')}")

    # Construir payload — solo campos hidden + credenciales (sin submit)
    fields = {}
    for inp in login_form.find_all("input", {"name": True}):
        if inp.get("type", "").lower() in ("submit", "button", "image"):
            continue
        fields[inp["name"]] = inp.get("value", "")
    fields[user_inp["name"]] = CONFIG["username"]
    fields[pass_inp["name"]] = CONFIG["password"]
    log.debug(f"  Payload fields: {list(fields.keys())}")

    # ── Paso 4: POST al pkmslogin.form ─────────────────────────────────────
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

    # ── Detectar fallo ─────────────────────────────────────────────────────
    soup2 = BeautifulSoup(resp2.text, "html.parser")
    still_login = bool(
        soup2.find("form", {"name": "LoginEntryForm"})
        or soup2.find("input", {"name": "username"})
    )
    if still_login:
        if "Authentication Failed" in resp2.text or "invalid" in resp2.text.lower():
            raise RuntimeError("Credenciales incorrectas — Authentication Failed")
        raise RuntimeError(
            f"Login fallo — sigue en pagina de login. URL: {resp2.url}\n"
            "Verifica usuario y password en CONFIG."
        )

    # ── Detectar 2FA ───────────────────────────────────────────────────────
    is_2fa = (
        "choiceSelect" in resp2.text
        or "OTP" in resp2.text
        or "Two-Factor" in resp2.text
    )
    if is_2fa:
        print("[2FA] Pantalla 2FA detectada")
        resp2 = cffi_handle_2fa(http, resp2.url, soup2, otp_code, out)
        write_html("03_cffi_post_2fa", resp2.text, out)

    final_url = resp2.url
    log.info(f"  URL final: {final_url}")
    cookies = [c.name for c in http.cookies.jar]
    log.info(f"  Cookies obtenidas: {cookies}")
    log.info("  Autenticacion exitosa!")
    return http


def cffi_handle_2fa(http, url, soup, otp_code, out):
    fields = extract_form_fields(soup)
    choice = soup.find("select", {"id": "choiceSelect"})
    if choice:
        fields[choice["name"]] = "0"  # 0 = Email OTP
    confirm = soup.find("input", {"value": "Confirm"}) or soup.find(
        "button", string=re.compile("Confirm", re.I)
    )
    if confirm and confirm.get("name"):
        fields[confirm["name"]] = confirm.get("value", "Confirm")

    print(f"[2FA-1] POST {url} (seleccionando Email OTP)")
    resp = http.post(url, data=fields, timeout=CONFIG["timeout"])
    print(f"  status={resp.status_code}  url={resp.url}")
    write_html("02b_cffi_2fa_confirm", resp.text, out)

    if not otp_code:
        print("\n" + "=" * 55)
        print("  OTP enviado al email (expira en 10 min).")
        print("=" * 55)
        otp_code = input("  -> Ingresa el OTP y presiona Enter: ").strip()
    if not otp_code:
        raise RuntimeError("No se ingreso OTP")

    soup3 = BeautifulSoup(resp.text, "html.parser")
    fields3 = extract_form_fields(soup3)
    otp_inp = (
        soup3.find("input", {"name": "otp"})
        or soup3.find("input", {"name": "OTP"})
        or soup3.find("input", {"name": "passcode"})
        or soup3.find("input", attrs={"maxlength": "6"})
        or soup3.find("input", attrs={"type": "tel"})
        or soup3.find("input", attrs={"type": "text"})
    )
    if not otp_inp:
        write_html("2fa_otp_field_not_found", resp.text, out)
        raise RuntimeError(
            "No se encontro campo OTP. Revisa output/html/2fa_otp_field_not_found.html"
        )

    fields3[otp_inp["name"]] = otp_code
    print(f"  Campo OTP: name='{otp_inp['name']}'")
    print(f"[2FA-2] POST {resp.url} (enviando OTP)")
    resp4 = http.post(resp.url, data=fields3, timeout=CONFIG["timeout"])
    print(f"  status={resp4.status_code}  url={resp4.url}")
    return resp4


# ─────────────────────────────────────────────────────────────
#  CAPA 2: Playwright — portal civil + reCAPTCHA + extraccion
# ─────────────────────────────────────────────────────────────
async def human_delay(page, lo=400, hi=1200):
    """Pausa aleatoria entre lo y hi milisegundos."""
    await page.wait_for_timeout(_rnd.randint(lo, hi))


async def human_wander(page, steps=None):
    """
    Mueve el mouse por la pagina siguiendo una trayectoria
    irregular (no lineal) que imita la lectura visual humana.
    Cada paso tiene una pausa variable entre si.
    """
    vw = page.viewport_size or {"width": 1440, "height": 900}
    w, h = vw["width"], vw["height"]
    n = steps or _rnd.randint(5, 9)

    # Trayectoria en zigzag con deriva vertical (como leer de arriba a abajo)
    x = _rnd.randint(80, w // 2)
    y = _rnd.randint(60, 160)
    for _ in range(n):
        x = max(60, min(w - 60, x + _rnd.randint(-180, 180)))
        y = max(60, min(h - 60, y + _rnd.randint(-60, 120)))
        await page.mouse.move(x, y)
        await page.wait_for_timeout(_rnd.randint(80, 350))


async def human_scroll(page, deep=False):
    """
    Scroll natural: baja gradualmente, pausa leyendo, sube.
    deep=True va mas abajo (paginas con mas contenido).
    """
    bottom = _rnd.randint(500, 900) if deep else _rnd.randint(200, 500)
    step = _rnd.randint(80, 160)
    pos = 0
    while pos < bottom:
        pos = min(pos + step, bottom)
        await page.evaluate(f"window.scrollTo({{ top: {pos}, behavior: 'smooth' }})")
        await page.wait_for_timeout(_rnd.randint(120, 280))
    await human_delay(page, 400, 900)
    # Sube de golpe (comportamiento comun al buscar algo arriba)
    await page.evaluate("window.scrollTo({ top: 0, behavior: 'smooth' })")
    await human_delay(page, 250, 500)


async def random_safe_click(page):
    """
    Hace click en un elemento de texto no interactivo (parrafo, label,
    encabezado) elegido al azar. Evita botones, inputs y links.
    Si no hay elementos seguros disponibles, hace click en coordenadas
    aleatorias del area superior de la pagina (zona de header/texto).
    """
    safe_selectors = ["p", "label", "h1", "h2", "h3", "h4", "span", "td", "th"]
    _rnd.shuffle(safe_selectors)
    for sel in safe_selectors:
        try:
            els = await page.query_selector_all(sel)
            if not els:
                continue
            el = _rnd.choice(els[:20])  # evitar recorrer listas enormes
            box = await el.bounding_box()
            if not box or box["width"] < 10 or box["height"] < 5:
                continue
            cx = box["x"] + _rnd.uniform(5, box["width"] - 5)
            cy = box["y"] + _rnd.uniform(2, box["height"] - 2)
            await page.mouse.move(cx, cy)
            await human_delay(page, 120, 300)
            await page.mouse.click(cx, cy)
            await human_delay(page, 100, 250)
            return
        except Exception:
            continue

    # Fallback: click en area segura del viewport
    vw = page.viewport_size or {"width": 1440, "height": 900}
    await page.mouse.click(
        _rnd.randint(50, vw["width"] // 2),
        _rnd.randint(50, 200),
    )


async def simulate_human_behavior(page):
    """
    Secuencia completa de comportamiento humano antes del submit/reCAPTCHA:
    wander + scroll + clicks aleatorios + hover sobre el boton de busqueda.
    """
    await human_wander(page, steps=_rnd.randint(5, 8))
    await human_scroll(page)
    for _ in range(_rnd.randint(2, 4)):
        await random_safe_click(page)
        await human_delay(page, 200, 500)
    await human_wander(page, steps=_rnd.randint(3, 5))

    # Hover final sobre el boton de busqueda (simula decidirse a buscar)
    try:
        btn = await page.query_selector("#searchByDocForm\\:btnSearch")
        if btn:
            await btn.hover()
            await human_delay(page, 400, 800)
    except Exception:
        pass


async def safe_goto(page, url, timeout=30000):
    """
    Navega a una URL tolerando captchas que bloquean 'networkidle'.
    hCaptcha y similares mantienen conexiones persistentes que impiden
    que 'networkidle' se resuelva. Estrategia:
      1. Intenta 'load'  (espera el evento load, ignora XHRs posteriores)
      2. Si falla, espera 'domcontentloaded' (mas permisivo)
      3. Siempre espera un minimo de 1.5s extra para que el DOM estabilice
    """
    try:
        await page.goto(url, wait_until="load", timeout=timeout)
    except Exception as e:
        log.warning(f"  [goto] 'load' timeout en {url[:60]}: {e}")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
    await page.wait_for_timeout(1500)


async def handle_captcha(page, sb, label=""):
    """
    Detecta hCaptcha o reCAPTCHA de desafio visible y los resuelve
    con SeleniumBase sb.solve_captcha().
    Retorna True si habia captcha y se resolvio, False si no habia.
    """
    captcha_selectors = [
        'iframe[src*="hcaptcha.com"]',
        'iframe[src*="recaptcha.com"][src*="bframe"]',
        ".h-captcha",
        "#hcaptcha",
    ]
    found = False
    for sel in captcha_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                found = True
                break
        except Exception:
            continue

    if not found:
        return False

    log.info(f"  [CAPTCHA] hCaptcha detectado{' en ' + label if label else ''}")
    if sb is None:
        log.warning("  [CAPTCHA] sb=None — no se puede resolver automaticamente")
        return False

    try:
        log.info("  [CAPTCHA] Resolviendo con sb.solve_captcha()...")
        await sb.solve_captcha()
        await page.wait_for_timeout(2000)
        log.info("  [CAPTCHA] Resuelto")
        return True
    except Exception as e:
        log.error(f"  [CAPTCHA] sb.solve_captcha() fallo: {e}")
        return False


async def go_to_civil_search(page, context, out, sb=None):
    """
    Navega al portal civil en una nueva pestaña (Ctrl+T / Cmd+T).
    Retorna (True, civil_page) si el formulario cargo, (False, None) si fallo.
    """
    import platform

    print("\n[5] Navegando al portal civil...")

    # Paso 5a: visitar ESSO portal en la pestaña actual para registrar la sesion
    ESSO_URL = "https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/"
    print(f"  [5a] GET {ESSO_URL}")
    await safe_goto(page, ESSO_URL, timeout=CONFIG["pw_timeout"])
    await handle_captcha(page, sb, label="ESSO portal")
    await human_delay(page, 800, 1800)
    await human_wander(page)
    await human_scroll(page)
    await random_safe_click(page)
    await human_delay(page, 500, 1000)
    await screenshot(page, "05a_esso_portal", out)
    await save_html(page, "05a_esso_portal", out)
    log.info(f"  URL tras ESSO: {page.url}")

    if "login" in page.url.lower() or "pkmslogin" in page.url:
        log.error("  Sesion invalida — sigue en login tras ESSO portal")
        return False, None

    # Paso 5b: abrir nueva pestaña para CIVIL_SEARCH_URL
    # En headless los atajos de teclado no funcionan (no hay UI) — usar new_page() directo.
    # En modo visible se intenta Ctrl+T/Cmd+T primero y se cae a new_page() si falla.
    print(f"  [5b] Abriendo nueva pestana -> {CIVIL_SEARCH_URL}")
    if CONFIG.get("headless", False):
        civil_page = await context.new_page()
        log.info("  Nueva pestana via context.new_page() (headless)")
    else:
        shortcut = "Meta+t" if platform.system() == "Darwin" else "Control+t"
        pages_before = list(context.pages)
        await page.keyboard.press(shortcut)
        await page.wait_for_timeout(1000)
        new_pages = [p for p in context.pages if p not in pages_before]
        if new_pages:
            civil_page = new_pages[0]
            log.info(f"  Nueva pestana via {shortcut}")
        else:
            log.warning(
                f"  {shortcut} no creo nueva pestana — usando context.new_page()"
            )
            civil_page = await context.new_page()

    await civil_page.bring_to_front()

    await safe_goto(civil_page, CIVIL_SEARCH_URL, timeout=CONFIG["pw_timeout"])
    await handle_captcha(civil_page, sb, label="civil search")
    await human_delay(civil_page, 1000, 2200)
    await human_wander(civil_page)
    await human_scroll(civil_page, deep=True)
    for _ in range(_rnd.randint(2, 3)):
        await random_safe_click(civil_page)
        await human_delay(civil_page, 300, 700)
    await human_wander(civil_page, steps=_rnd.randint(3, 5))
    await human_delay(civil_page, 600, 1200)
    await screenshot(civil_page, "05b_civil_landing", out)
    await save_html(civil_page, "05b_civil_landing", out)
    log.info(f"  URL: {civil_page.url}")
    log.info(f"  Titulo: {await civil_page.title()}")

    if "login" in civil_page.url.lower() or "pkmslogin" in civil_page.url:
        log.error("  Sigue en login -> las cookies no cubren portal.njcourts.gov")
        return False, None

    # En headless el hCaptcha tarda mas en auto-resolver — dar mas tiempo.
    # Reintentar navegacion si el formulario no aparece en el primer intento.
    form_timeout = 45000 if CONFIG.get("headless") else 15000
    for attempt in range(1, 4):
        try:
            await civil_page.wait_for_selector(
                "#civilCaseSearchForm\\:idDiv", timeout=form_timeout
            )
            content = await civil_page.content()
            if "Captcha verification has failed" in content:
                log.warning(
                    "  Captcha fallido de sesion anterior — ignorando, continuamos"
                )
            print("  Formulario civil cargado")
            return True, civil_page
        except Exception:
            if attempt < 3:
                log.warning(
                    f"  Formulario no encontrado (intento {attempt}/3) — renavegando..."
                )
                await safe_goto(
                    civil_page, CIVIL_SEARCH_URL, timeout=CONFIG["pw_timeout"]
                )
                await handle_captcha(
                    civil_page, sb, label=f"civil search retry {attempt}"
                )
                await civil_page.wait_for_timeout(5000)
            else:
                await screenshot(civil_page, "05b_civil_fail", out)
                await save_html(civil_page, "05b_civil_fail", out)
                print("  No cargo el formulario tras 3 intentos")
                return False, None


async def search_civil_case(
    page,
    out,
    docket_num="000035",
    docket_year="21",
    court_type="Civil Part",
    county="ATLANTIC",
    docket_type="L",
    solver=None,
):
    print(f"\n[6] county={county} docket={docket_num}/{docket_year}")

    try:
        s = await page.wait_for_selector("#civilCaseSearchForm\\:idDiv", timeout=15000)
        tv = COURT_VALUES.get(court_type, "LCV")
        if await s.input_value() != tv:
            await s.hover()
            await human_delay(page, 200, 500)
            await s.select_option(value=tv)
            await page.wait_for_load_state("networkidle", timeout=15000)
            await human_delay(page, 800, 1600)
        print(f"  [6a] Court: {court_type}")
    except Exception as e:
        print(f"  Error court: {e}")
        return []

    try:
        v = await page.wait_for_selector(
            "#searchByDocForm\\:idCivilVenue", timeout=10000
        )
        code = COUNTY_CODES.get(county.upper(), county)
        await v.hover()
        await human_delay(page, 150, 400)
        await v.select_option(value=code)
        await human_delay(page, 700, 1400)
        print(f"  [6b] County: {county} ({code})")
    except Exception as e:
        print(f"  Error county: {e}")
        return []

    try:
        d = await page.wait_for_selector("#searchByDocForm\\:docketType", timeout=10000)
        await d.hover()
        await human_delay(page, 150, 350)
        await d.select_option(value=docket_type)
        await human_delay(page, 500, 1000)
        print(f"  [6c] DocketType: {docket_type}")
    except Exception as e:
        print(f"  DocketType default: {e}")

    try:
        n = await page.wait_for_selector(
            "#searchByDocForm\\:idCivilDocketNum", timeout=10000
        )
        await n.hover()
        await human_delay(page, 200, 450)
        await n.click()
        await human_delay(page, 100, 250)
        await page.keyboard.press("Control+a")
        for ch in docket_num:
            await page.keyboard.type(ch)
            await page.wait_for_timeout(_rnd.randint(60, 180))
        await human_delay(page, 300, 600)
        print(f"  [6d] DocketNum: {docket_num}")
    except Exception as e:
        print(f"  Error docket num: {e}")
        return []

    try:
        y = await page.wait_for_selector(
            "#searchByDocForm\\:idCivilDocketYear", timeout=10000
        )
        await y.hover()
        await human_delay(page, 200, 450)
        await y.click()
        await human_delay(page, 100, 250)
        await page.keyboard.press("Control+a")
        for ch in docket_year:
            await page.keyboard.type(ch)
            await page.wait_for_timeout(_rnd.randint(60, 180))
        await human_delay(page, 300, 600)
        print(f"  [6e] DocketYear: {docket_year}")
    except Exception as e:
        print(f"  Error docket year: {e}")
        return []

    await screenshot(page, "06_form_filled", out)
    await save_html(page, "06_form_filled", out)

    # Simular comportamiento humano antes del reCAPTCHA para mejorar el score v3
    await simulate_human_behavior(page)

    # reCAPTCHA Enterprise — dos modos segun config:
    #   A) FreeCaptchaBypass (fcb_api_key configurado): resolucion externa, inyecta
    #      token en el campo oculto y hace click directo en btnSearch.
    #   B) playwright-recaptcha (fallback): captura pasiva del token desde /reload,
    #      usa searchBtnDummy para que el JS nativo gestione el submit.
    log.info("  [6f] Esperando grecaptcha.enterprise...")
    try:
        await page.wait_for_function("typeof grecaptcha !== 'undefined'", timeout=15000)
        await page.wait_for_function(
            "typeof grecaptcha.enterprise !== 'undefined'", timeout=10000
        )
        log.info("  grecaptcha.enterprise disponible")
    except Exception as e:
        log.warning(f"  grecaptcha no disponible: {e}")

    fcb_key = CONFIG.get("fcb_api_key", "")
    if fcb_key:
        # ── MODO A: FreeCaptchaBypass ─────────────────────────────────────────
        # pageAction DEBE ser 'CivilSearch' — confirmado en el JS de la pagina.
        # minScore=0.7 para asegurar que el servidor acepte el token.
        log.info(
            "  [6g] Modo FCB: obteniendo token (action=CivilSearch, minScore=0.7)..."
        )
        try:
            token = await solve_recaptcha_fcb(
                website_url=CIVIL_SEARCH_URL,
                website_key=RECAPTCHA_SITE_KEY,
                page_action="CivilSearch",
            )
            log.info(f"  Token FCB: {len(token)} chars")

            # Inyectar en el campo exacto que el formulario JSF envia al servidor
            # ID: searchByDocForm:recaptchaResponse (confirmado en el HTML)
            await page.evaluate(
                """(t) => {
                    const f = document.getElementById('searchByDocForm:recaptchaResponse');
                    if (f) f.value = t;
                }""",
                token,
            )
            log.info("  Token inyectado en searchByDocForm:recaptchaResponse")

            # btnSearch es display:none — Playwright no puede hacer hover/click normal.
            # JS click bypasea esa restriccion y dispara el submit del formulario.
            await human_delay(page, 200, 400)
            await page.evaluate(
                "document.getElementById('searchByDocForm:btnSearch').click();"
            )
            log.info("  btnSearch.click() via JS ejecutado")
        except Exception as e:
            log.warning(f"  FCB fallo: {e} — usando flujo nativo como fallback")
            fcb_key = ""  # fuerza el bloque de abajo

    if not fcb_key:
        # ── MODO B: playwright-recaptcha (captura pasiva del token nativo) ───
        # El JS de la pagina llama grecaptcha.enterprise.execute con action='CivilSearch'.
        # playwright-recaptcha intercepta la respuesta de /reload y captura el token.
        log.info("  [6g] Modo playwright-recaptcha: Submit via searchBtnDummy...")
        try:
            btn_dummy = await page.wait_for_selector(
                "#searchByDocForm\\:searchBtnDummy", timeout=5000
            )
            await btn_dummy.hover()
            await human_delay(page, 200, 400)
            await btn_dummy.click()

            if solver is not None:
                try:
                    token = await solver.solve_recaptcha()
                    log.info(f"  reCAPTCHA token capturado: {len(token)} chars")
                except Exception as e:
                    log.warning(f"  solver: {e} — continuando con flujo nativo")
        except Exception as e:
            print(f"  Error submit: {e}")
            await screenshot(page, "07_submit_error", out)
            return []

    # Esperar que el DOM refleje la pagina destino (evita crash en page.content)
    try:
        await page.wait_for_selector(
            "#caseSummaryDiv, #civilCaseSearchResults, table.result-table",
            timeout=20000,
        )
    except Exception:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)
    log.info("  Submit ejecutado")

    content = await page.content()
    if "Captcha verification has failed" in content:
        log.error("  Captcha rechazado. headless=False en CONFIG para ver el browser")
        await screenshot(page, "07_captcha_failed", out)
        await save_html(page, "07_captcha_failed", out)
        return []

    await screenshot(page, "07_search_results", out)
    await save_html(page, "07_search_results", out)
    log.info("  HTML guardado -> output/html/07_search_results.html")

    # Detectar tipo de pagina: summary individual vs lista de resultados
    if "caseSummaryDiv" in content or "idCaseTitle" in content:
        log.info("  Pagina de case summary detectada -> extrayendo campos")
        return [extract_case_summary(content)]
    return await extract_table_data(page, out)


def extract_case_summary(html: str) -> dict:
    """Extracts case summary fields from civilCaseSummary.faces HTML."""
    soup = BeautifulSoup(html, "html.parser")
    data = {}

    # Docket Number — compuesto por 4 spans separados
    # docVenueTitleDC (ATL) + docTypeCodeTitle (L) + docSeqNumTitle (000001) + docYeaerTitle (15)
    # Resultado: "ATL-L-000001-15"
    venue = (
        (soup.find(id="docVenueTitleDC") or {}).get_text(strip=True)
        if soup.find(id="docVenueTitleDC")
        else ""
    )
    dtype = (
        (soup.find(id="docTypeCodeTitle") or {}).get_text(strip=True)
        if soup.find(id="docTypeCodeTitle")
        else ""
    )
    seq = (
        (soup.find(id="docSeqNumTitle") or {}).get_text(strip=True)
        if soup.find(id="docSeqNumTitle")
        else ""
    )
    year = (
        (soup.find(id="docYeaerTitle") or {}).get_text(strip=True)
        if soup.find(id="docYeaerTitle")
        else ""
    )
    parts = [p for p in [venue, dtype, seq, year] if p]
    data["docket_number"] = "-".join(parts)

    # Case Caption — dedicated id
    el = soup.find(id="idCaseTitle")
    data["Case Caption"] = el.get_text(strip=True) if el else ""

    # Standard label→value pairs: <span class="ValueField">Label: </span>
    #                              <span class="LabelField">Value</span>
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
        # Match both exact "Label:" and "Label: " variants
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

    # Consolidated Case — ValueField with id containing "consolidatedCaseN"
    el = soup.find(id=lambda x: x and "consolidatedCaseN" in x)
    data["Consolidated Case"] = el.get_text(strip=True) if el else ""

    # Statewide Lien — ValueField with id containing "jdgmntStatewideLien"
    el = soup.find(id=lambda x: x and "jdgmntStatewideLien" in x)
    data["Statewide Lien"] = el.get_text(strip=True) if el else ""

    return data


async def extract_table_data(page, out):
    all_rows = []
    page_num = 1
    while True:
        print(f"  Extrayendo pagina {page_num}...")
        await page.wait_for_timeout(1000)
        headers = [
            (await th.inner_text()).strip()
            for th in await page.query_selector_all("table th")
            if (await th.inner_text()).strip()
        ]
        page_rows = []
        for row in await page.query_selector_all("tbody tr"):
            cells = await row.query_selector_all("td")
            if not cells:
                continue
            values = [(await c.inner_text()).strip() for c in cells]
            if not any(values):
                continue
            rd = (
                dict(zip(headers, values))
                if headers and len(headers) == len(values)
                else {f"col_{i}": v for i, v in enumerate(values)}
            )
            hrefs = [
                await lnk.get_attribute("href")
                for lnk in await row.query_selector_all("a")
                if await lnk.get_attribute("href")
            ]
            if hrefs:
                rd["_links"] = "; ".join(hrefs)
            page_rows.append(rd)
        if not page_rows:
            print(
                "  No se encontraron filas -> revisa output/html/07_search_results.html"
            )
            break
        print(f"    -> {len(page_rows)} filas")
        all_rows.extend(page_rows)
        next_btn = None
        for sel in [
            'a:has-text("Next")',
            'button:has-text("Next")',
            '[aria-label="Next page"]',
        ]:
            try:
                el = await page.query_selector(sel)
                if el and not await el.get_attribute("disabled"):
                    next_btn = el
                    break
            except Exception:
                continue
        if not next_btn:
            break
        await next_btn.click()
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1500)
        page_num += 1
    print(f"  Total: {len(all_rows)} filas")
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


async def shutdown_seleniumbase_cdp(sb, browser=None):
    """
    Cierra Playwright + SeleniumBase CDP sin dejar transports asyncio vivos.

    sb.quit()/sb.stop() programa el cierre del websocket con create_task()
    pero no espera al subprocess de Chrome. Si asyncio.run() cierra el loop
    antes de que ese transport se limpie, Python emite:
      RuntimeError: Event loop is closed
    """
    if browser is not None:
        try:
            await browser.close()
        except Exception as e:
            log.debug(f"  [shutdown] browser.close() fallo: {e}")

    if sb is None:
        return

    connection = getattr(sb, "connection", None)
    if connection is not None:
        try:
            await connection.aclose()
            log.debug("  [shutdown] CDP websocket cerrado")
        except Exception as e:
            log.debug(f"  [shutdown] connection.aclose() fallo: {e}")
        finally:
            try:
                sb.connection = None
            except Exception:
                pass

    proc = getattr(sb, "_process", None)
    if proc is not None:
        try:
            if getattr(proc, "returncode", None) is None:
                proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=10)
            log.debug("  [shutdown] Chrome CDP terminado")
        except Exception as e:
            log.debug(f"  [shutdown] terminate/wait fallo: {e}")
            try:
                if getattr(proc, "returncode", None) is None:
                    proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
                log.debug("  [shutdown] Chrome CDP forzado con kill()")
            except Exception as kill_err:
                log.debug(f"  [shutdown] kill()/wait() fallo: {kill_err}")
        finally:
            try:
                sb._process = None
                sb._process_pid = None
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
async def main(otp_code=""):
    out = make_output_dir()
    pw_cookies = []

    # ── Bootstrap Incapsula — debe ir ANTES de curl_cffi ──────────────────
    print("\n[=== BOOTSTRAP: Resolviendo challenge Incapsula con Playwright ===]")
    # Visitar ambos dominios: portal-cloud (SAML/ESSO) y portal (civil search)
    # Cada uno tiene su propio site-ID de Incapsula — necesitamos cookies de ambos
    incapsula_cookies = await bootstrap_incapsula_cookies(
        [
            CONFIG["portal_url"],  # portal-cloud.njcourts.gov
            "https://portal.njcourts.gov/",  # portal.njcourts.gov (civil search domain)
        ]
    )
    if not incapsula_cookies:
        log.warning(
            "  No se obtuvieron cookies Incapsula — curl_cffi puede ser bloqueado"
        )

    print("\n[=== CAPA 1: curl_cffi TLS Safari fingerprint ===]")
    try:
        http = cffi_login(out, otp_code=otp_code, incapsula_cookies=incapsula_cookies)
        pw_cookies = cffi_cookies_to_playwright(http)
        print(f"  {len(pw_cookies)} cookies transferidas a Playwright")
    except RuntimeError as e:
        print(f"\n curl_cffi fallo: {e}")
        print("  Revisa el target en CONFIG o ejecuta test_targets.py")
        return

    print(
        "\n[=== CAPA 2: SeleniumBase CDP + Playwright (portal civil + reCAPTCHA) ===]"
    )
    headless = CONFIG["headless"]
    log.info(f"  headless={headless}")

    # ── SeleniumBase cdp_driver lanza Chrome con stealth a nivel CDP ───────
    # connect_over_cdp() adjunta Playwright al browser ya corriendo.
    # A diferencia de pw.chromium.launch(), el browser NO tiene señales de
    # automatizacion (webdriver flag, TLS fingerprint de Chromium, etc.)
    # y reCAPTCHA Enterprise obtiene un score real en lugar del fallback ~0.1.
    sb = await cdp_driver.start_async(headless=headless)
    endpoint_url = sb.get_endpoint_url()
    log.info(f"  SeleniumBase CDP endpoint: {endpoint_url}")

    async with async_playwright() as pw:
        browser = None
        page = None
        # Playwright se adjunta al Chrome de SeleniumBase via CDP
        browser = await pw.chromium.connect_over_cdp(endpoint_url)
        context = browser.contexts[0]
        log.info("  Playwright conectado via connect_over_cdp()")

        # Cargar sesion anterior (reCAPTCHA da mayor score a browsers con historial)
        await load_session(context)

        # Inyectar cookies de la Capa 1 (Incapsula) y Capa 2 (ISAM curl_cffi)
        if pw_cookies:
            await context.add_cookies(pw_cookies)
            log.info(f"  {len(pw_cookies)} cookies inyectadas en contexto CDP")

        # Usar la pagina activa o crear una nueva si no hay ninguna
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            # go_to_civil_search abre nueva pestana (Ctrl+T / Cmd+T) y retorna
            # la pagina civil. El solver se inicia sobre esa nueva pagina ANTES
            # de que grecaptcha.enterprise haga cualquier peticion a /reload.
            ok, civil_page = await go_to_civil_search(page, context, out, sb=sb)
            if not ok:
                print("\n No se pudo cargar el portal civil.")
                return
            # Guardar sesion con cookies reales (Incapsula + ISAM + portal).
            # Se carga en la siguiente ejecucion para que reCAPTCHA vea historial.
            await save_session(context, out)
            async with recaptchav3.AsyncSolver(civil_page) as solver:
                data = await search_civil_case(civil_page, out, solver=solver)
            export_results(data, out)
        except Exception as e:
            print(f"\n Error: {e}")
            await screenshot(page, "error_final", out)
            raise
        finally:
            try:
                if page is not None and not page.is_closed():
                    await page.wait_for_timeout(3000)
            except Exception:
                pass
            await shutdown_seleniumbase_cdp(sb, browser)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="NJ Courts Scraper — curl_cffi + Playwright",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python scraper.py              # pide OTP en consola
  python scraper.py --otp 847291 # OTP directo por argumento

Para debug: cambia headless=False en CONFIG para ver el browser.
Si el login falla: ejecuta test_targets.py para encontrar un target valido.
        """,
    )
    parser.add_argument("--otp", default="", help="Codigo OTP 2FA")
    args = parser.parse_args()
    asyncio.run(main(otp_code=args.otp))
