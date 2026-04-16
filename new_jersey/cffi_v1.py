"""
NJ Courts Civil Case Scraper — HTTP puro con JSF ViewState + 2Captcha + IPRoyal
=====================================================================

Flujo:
  1. Verificar proxy IPRoyal residencial USA.
  2. Probar targets de impersonación curl_cffi hasta encontrar uno funcional.
  3. Login IBM ISAM (pkmslogin.form) con credenciales + 2FA OTP.
  4. GET formulario civil -> extraer campos JSF y javax.faces.ViewState.
  5. Resolver reCAPTCHA v3 Enterprise con 2Captcha:
       - type=RecaptchaV3TaskProxyless
       - isEnterprise=true
       - pageAction='CivilSearch'
       - minScore=0.9
  6. Inyectar el token en searchByDocForm:recaptchaResponse.
  7. POST formulario de búsqueda civil con ViewState + token reCAPTCHA.
  8. Parsear resultados con BeautifulSoup.
"""

import os
import json
import logging
import random
import re
import time
import requests
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

# ── Logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("njcourts")

# ── IPRoyal — credenciales via variables de entorno ──────────────────
# Configura antes de correr:
#   export IPROYAL_USER="tu_usuario"
#   export IPROYAL_PASS="tu_password"
IPROYAL_HOST = "geo.iproyal.com"
IPROYAL_PORT = "12322"
IPROYAL_USER = os.getenv("IPROYAL_USER", "")
IPROYAL_PASS = os.getenv("IPROYAL_PASS", "")
IP_SERVICE = "https://api.ipify.org"


def get_proxy_url(rotate: bool = False) -> str:
    user = IPROYAL_USER
    if rotate:
        user = f"{IPROYAL_USER}_session-{random.randint(1, 999999)}"
    return f"socks5://{user}:{IPROYAL_PASS}@{IPROYAL_HOST}:{IPROYAL_PORT}"


def verificar_ip_proxy() -> str | None:
    """Verifica que el proxy funciona y muestra la IP activa."""
    proxy_url = get_proxy_url()
    try:
        resp = requests.get(
            IP_SERVICE,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=15,
            verify=False,  # IPRoyal usa cert propio
        )
        ip = resp.text.strip()
        print(f"[PROXY] IP activa: {ip} ✅")
        return ip
    except Exception as e:
        print(f"[PROXY] Error conectando al proxy: {e} ❌")
        print("[PROXY] Verifica: export IPROYAL_USER=... && export IPROYAL_PASS=...")
        return None


# ────────────────────────────────────────────────────────────────────

# ── Config general ───────────────────────────────────────────────────
CONFIG = {
    "username": os.getenv("NJ_USERNAME", ""),
    "password": os.getenv("NJ_PASSWORD", ""),
    "portal_url": "https://portal-cloud.njcourts.gov/prweb/PRAuth/CloudSAMLAuth?AppName=ESSO",
    "timeout": 30,
    "output_dir": "./output",
    "save_html": True,
    "captcha_api_key": os.getenv("TWOCAPTCHA_API_KEY", ""),
    "max_retries_docket": 3,
}

CIVIL_SEARCH_URL = "https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/civilCaseSearch.faces"

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

_IMPERSONATE_TARGETS = ["safari15_3", "safari15_5", "safari18_0"]

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

CAPTCHA_API_URL = "https://api.2captcha.com"
RECAPTCHA_SITE_KEY = "6LeSprIqAAAAACbw4xnAsXH42Q4mfXk6t2MB09dq"


# ── Helpers de archivos ──────────────────────────────────────────────
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


def write_bytes(name, content, out, suffix):
    path = out / f"{name}{suffix}"
    path.write_bytes(content)
    log.info(f"  [FILE] -> {path}")
    return path


# ── Helpers HTML ─────────────────────────────────────────────────────
def has_bot_block(content: str) -> bool:
    return "Pardon Our Interruption" in content


def page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.title.string.strip() if soup.title and soup.title.string else "?"


def extract_form_fields(node):
    fields = {}
    for inp in node.find_all("input", {"name": True}):
        itype = inp.get("type", "text").lower()
        if itype in ("image", "submit", "button"):
            continue
        if itype == "checkbox":
            if inp.has_attr("checked"):
                fields[inp["name"]] = inp.get("value", "on")
        else:
            fields[inp["name"]] = inp.get("value", "")
    for sel in node.find_all("select", {"name": True}):
        selected = sel.find("option", {"selected": True})
        fields[sel["name"]] = selected.get("value", "") if selected else ""
    for ta in node.find_all("textarea", {"name": True}):
        fields[ta["name"]] = ta.get_text() or ""
    return fields


# ── Sesión HTTP con proxy IPRoyal ────────────────────────────────────
def make_http_session(target: str) -> cffi_requests.Session:
    """Crea sesión curl_cffi con proxy IPRoyal."""
    # proxy_url = get_proxy_url()
    session = cffi_requests.Session(impersonate=target)
    session.headers.update(SAFARI_HEADERS)
    session.proxies = {
        "http": get_proxy_url(),
        "https": get_proxy_url(),
    }
    session.verify = False  # IPRoyal usa cert propio
    return session


def get_http_session(probe_url: str | None = None) -> cffi_requests.Session:
    """Prueba targets de impersonación con proxy IPRoyal."""
    if probe_url:
        for target in _IMPERSONATE_TARGETS:
            try:
                session = make_http_session(target)
                resp = session.get(probe_url, timeout=15, allow_redirects=True)
                if resp.status_code == 200 and not has_bot_block(resp.text):
                    log.info(f"[HTTP] Target funcional: {target}")
                    return session
            except Exception as e:
                log.debug(f"  [HTTP] Probe falló para {target}: {e}")
                continue

    log.info(f"[HTTP] Todos los probes fallaron. Usando {_IMPERSONATE_TARGETS[0]}")
    return make_http_session(_IMPERSONATE_TARGETS[0])


# ── CAPTCHA ──────────────────────────────────────────────────────────
def solve_recaptcha_enterprise(api_key, page_url, action="CivilSearch"):
    log.info("[CAPTCHA] Solicitando token reCAPTCHA v3 2captcha...")
    create_resp = cffi_requests.post(
        f"{CAPTCHA_API_URL}/createTask",
        json={
            "clientKey": api_key,
            "task": {
                "type": "RecaptchaV3TaskProxyless",
                "websiteURL": page_url,
                "websiteKey": RECAPTCHA_SITE_KEY,
                "pageAction": action,
                "minScore": 0.9,
                "isEnterprise": True,
            },
        },
        timeout=30,
    )
    log.info(f"[CAPTCHA] Response status: {create_resp.status_code}")
    log.debug(f"[CAPTCHA] Response body: {create_resp.text[:500]}")

    try:
        result = create_resp.json()
    except Exception:
        raise RuntimeError(
            f"[CAPTCHA] Respuesta no es JSON. Status: {create_resp.status_code}, "
            f"Body: {create_resp.text[:300]}"
        )

    if result.get("errorId", 0) != 0:
        raise RuntimeError(f"[CAPTCHA] Error creando task: {result}")

    task_id = result["taskId"]
    log.info(f"[CAPTCHA] Task creado: {task_id}")

    for attempt in range(60):
        time.sleep(3)
        poll_resp = cffi_requests.post(
            f"{CAPTCHA_API_URL}/getTaskResult",
            json={"clientKey": api_key, "taskId": task_id},
            timeout=30,
        )
        poll_result = poll_resp.json()
        if poll_result.get("errorId", 0) != 0:
            raise RuntimeError(f"[CAPTCHA] Error polling: {poll_result}")
        if poll_result.get("status") == "ready":
            token = poll_result["solution"]["gRecaptchaResponse"]
            log.info(f"[CAPTCHA] Token obtenido ({len(token)} chars)")
            return token
        log.debug(
            f"[CAPTCHA] Polling {attempt + 1}/60 — status: {poll_result.get('status')}"
        )

    raise RuntimeError("[CAPTCHA] Timeout esperando resolución")


# ── Login ────────────────────────────────────────────────────────────
def cffi_login(http: cffi_requests.Session, out, otp_code=""):
    log.info(f"[1] GET {CONFIG['portal_url']}")
    resp0 = http.get(
        CONFIG["portal_url"], timeout=CONFIG["timeout"], allow_redirects=True
    )
    log.info(f"  status={resp0.status_code}  url={resp0.url}")
    write_html("01_cffi_landing", resp0.text, out)

    if has_bot_block(resp0.text):
        raise RuntimeError(
            "Imperva bloqueó portal-cloud.\n"
            "Revisa: output/html/01_cffi_landing.html\n"
            "Verifica credenciales IPRoyal y zona USA."
        )

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

    if has_bot_block(resp_login.text):
        raise RuntimeError(
            "Imperva bloqueó pkmslogin.form.\n"
            "Revisa: output/html/01b_cffi_idp_login.html\n"
            "Verifica que el proxy sea residencial USA."
        )

    soup = BeautifulSoup(resp_login.text, "html.parser")
    soup0 = BeautifulSoup(resp0.text, "html.parser")

    def find_login_form(s):
        return (
            s.find("form", {"name": "LoginEntryForm"})
            or s.find("form", action=lambda a: a and "pkmslogin" in str(a))
            or (s.find("input", {"name": "username"}) and s.find("form"))
        )

    login_form = find_login_form(soup)
    if not login_form:
        login_form = find_login_form(soup0)
        if login_form:
            soup = soup0
            resp_login = resp0
        else:
            raise RuntimeError(
                "No se encontró formulario de login.\n"
                f"SAML title: {page_title(resp0.text)}\n"
                f"IDP title: {page_title(resp_login.text)}\n"
                "Revisa: output/html/01_cffi_landing.html y 01b_cffi_idp_login.html"
            )

    form_action = login_form.get("action", "")
    if not form_action:
        raise RuntimeError("El form no tiene atributo action")
    if not form_action.startswith("http"):
        form_action = urljoin(resp_login.url, form_action)

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

    fields = extract_form_fields(login_form)
    fields[user_inp["name"]] = CONFIG["username"]
    fields[pass_inp["name"]] = CONFIG["password"]

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
            raise RuntimeError("Credenciales incorrectas")
        raise RuntimeError(f"Login falló. URL: {resp2.url}")

    is_2fa = (
        "choiceSelect" in resp2.text
        or "OTP" in resp2.text
        or "Two-Factor" in resp2.text
    )
    if is_2fa:
        log.info("[2FA] Pantalla 2FA detectada")
        resp2 = cffi_handle_2fa(http, resp2.url, soup2, otp_code, out)
        write_html("03_cffi_post_2fa", resp2.text, out)

    log.info("  Autenticacion exitosa")
    return http


def cffi_handle_2fa(http, url, soup, otp_code, out):
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

    selection_form = soup.find("form", action=re.compile(r"StateId="))
    choice_select = soup.find("select", {"id": "choiceSelect"})

    if choice_select and selection_form:
        selection_url = urljoin(url, selection_form.get("action"))
        fields = extract_form_fields(selection_form)
        fields["choice"] = "0"
        fields["operation"] = "verify"
        resp = http.post(
            selection_url,
            data=fields,
            headers=navigation_headers,
            timeout=CONFIG["timeout"],
        )
        write_html("02b_cffi_2fa_method_selected", resp.text, out)
        soup = BeautifulSoup(resp.text, "html.parser")
        url = resp.url

    verify_form = soup.find("form", action=re.compile(r"StateId="))
    if not verify_form:
        raise RuntimeError("No se encontró el formulario OTP")

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

    fields["otp.user.otp"] = otp_code
    fields["operation"] = "verify"

    return http.post(
        action_url,
        data=fields,
        headers=navigation_headers,
        timeout=CONFIG["timeout"],
    )


# ── Navegación y búsqueda civil ──────────────────────────────────────
def navigate_to_civil_search(http: cffi_requests.Session, out) -> BeautifulSoup:
    esso_url = "https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/"
    log.info(f"[4] GET {esso_url}")
    resp_esso = http.get(esso_url, timeout=CONFIG["timeout"], allow_redirects=True)
    log.info(f"  status={resp_esso.status_code}  url={resp_esso.url}")
    write_html("04_esso_portal", resp_esso.text, out)

    if "login" in resp_esso.url.lower() or "pkmslogin" in resp_esso.url:
        raise RuntimeError("Sesión inválida")

    log.info(f"[5] GET {CIVIL_SEARCH_URL}")
    resp_civil = http.get(
        CIVIL_SEARCH_URL,
        timeout=CONFIG["timeout"],
        allow_redirects=True,
        headers={"Referer": esso_url},
    )
    log.info(f"  status={resp_civil.status_code}  url={resp_civil.url}")
    write_html("05_civil_search_form", resp_civil.text, out)

    soup = BeautifulSoup(resp_civil.text, "html.parser")
    form = soup.find("form", id="searchByDocForm") or soup.find(
        "form", id="civilCaseSearchForm"
    )
    if not form:
        raise RuntimeError("Formulario civil no encontrado")

    log.info("  Formulario civil cargado via HTTP")
    return soup


def search_civil_case(
    http: cffi_requests.Session,
    form_soup: BeautifulSoup,
    out,
    docket_num="000222",
    docket_year="21",
    court_type="Civil Part",
    county="ATLANTIC",
    docket_type="L",
) -> list:
    log.info(f"\n[6] county={county} docket={docket_num}/{docket_year}")

    form = form_soup.find("form", id="searchByDocForm") or form_soup.find(
        "form", id="civilCaseSearchForm"
    )
    if not form:
        raise RuntimeError("No se encontró el form JSF de búsqueda")

    fields = extract_form_fields(form)

    court_value = COURT_VALUES.get(court_type, "LCV")
    county_code = COUNTY_CODES.get(county.upper(), county)

    field_mapping = {
        "civilCaseSearchForm:idDiv": court_value,
        "searchByDocForm:idCivilVenue": county_code,
        "searchByDocForm:docketType": docket_type,
        "searchByDocForm:idCivilDocketNum": docket_num,
        "searchByDocForm:idCivilDocketYear": docket_year,
    }
    for k, v in field_mapping.items():
        fields[k] = v

    fields.pop("searchByDocForm:searchBtnDummy", None)
    fields.pop("civilCaseSearchForm:searchBtnDummy", None)
    fields["searchByDocForm:btnSearch"] = "Search"
    fields["javax.faces.source"] = "searchByDocForm:btnSearch"

    if "javax.faces.ViewState" not in fields:
        raise RuntimeError("No se encontró javax.faces.ViewState en el formulario")

    log.info(f"  ViewState: {fields['javax.faces.ViewState'][:40]}...")
    log.info(f"  Court: {court_type} ({court_value})")
    log.info(f"  County: {county} ({county_code})")

    if CONFIG.get("captcha_api_key"):
        captcha_token = solve_recaptcha_enterprise(
            CONFIG["captcha_api_key"], CIVIL_SEARCH_URL
        )
        fields["searchByDocForm:recaptchaResponse"] = captcha_token
    else:
        log.warning("[CAPTCHA] No captcha_api_key — enviando sin token válido")

    form_action = form.get("action", "")
    if form_action and not form_action.startswith("http"):
        post_url = urljoin(CIVIL_SEARCH_URL, form_action)
    else:
        post_url = form_action or CIVIL_SEARCH_URL

    log.info(f"  POST URL: {post_url}")
    resp = http.post(
        post_url,
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
    write_html(f"07_search_results_{docket_num}_{docket_year}", resp.text, out)

    content = resp.text
    log.info(f"  Response title: {page_title(content)}")
    log.info(f"  Response length: {len(content)} chars")
    log.info(f"  Contains caseSummaryDiv: {'caseSummaryDiv' in content}")

    if has_bot_block(content):
        raise RuntimeError("Bloqueado por anti-bot: 'Pardon Our Interruption'")

    resp_soup = BeautifulSoup(content, "html.parser")
    doc_venue = resp_soup.find(id="docVenueTitleDC")

    if not doc_venue:
        log.warning("[RESULTADO] No se encontró 'docVenueTitleDC'. Se reintentará.")
        return None

    log.info(f"  docVenueTitleDC: {doc_venue.get_text(strip=True)}")

    print_form = resp_soup.find("form", id="j_id_2s")
    print_btn = resp_soup.find("input", {"name": "j_id_2s:printBtn"})
    if not print_form or not print_btn:
        log.warning("[PRINT] Sin botón 'Create Summary Report'. Se reintentará.")
        return None

    data = extract_case_summary(content)
    log.info(f"  Docket:  {data.get('docket_number', '?')}")
    log.info(f"  Caption: {data.get('Case Caption', '?')[:80]}")

    pdf_path = maybe_download_summary_pdf(http, resp.url, content, out, data)
    if pdf_path:
        data["pdf"] = str(pdf_path)

    return [data]


def maybe_download_summary_pdf(
    http: cffi_requests.Session,
    page_url: str,
    html: str,
    out,
    summary_data: dict | None = None,
):
    soup = BeautifulSoup(html, "html.parser")
    print_form = soup.find("form", id="j_id_2s")
    print_btn = soup.find("input", {"name": "j_id_2s:printBtn"})

    if not print_form or not print_btn:
        log.info("  Print summary button no encontrado; no se genera PDF")
        return None

    action = print_form.get("action", "")
    if not action:
        log.warning("  El formulario de print no tiene action")
        return None

    action_url = urljoin(page_url, action)
    fields = extract_form_fields(print_form)
    fields["j_id_2s:printBtn"] = print_btn.get("value", " Create Summary Report ")

    log.info(f"[7] POST {action_url} (summary PDF)")
    resp = http.post(
        action_url,
        data=fields,
        timeout=CONFIG["timeout"],
        allow_redirects=True,
        headers={
            "Referer": page_url,
            "Origin": "https://portal.njcourts.gov",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    log.info(
        "  status=%s  url=%s  content-type=%s",
        resp.status_code,
        resp.url,
        resp.headers.get("Content-Type", ""),
    )

    content_type = resp.headers.get("Content-Type", "").lower()
    if "application/pdf" not in content_type:
        text = resp.text
        if has_bot_block(text):
            raise RuntimeError("Bloqueado por anti-bot al generar el PDF")
        write_html("08_summary_report_unexpected", text, out)
        log.warning("  La respuesta del printBtn no fue PDF")
        return None

    docket_number = ""
    if summary_data:
        docket_number = summary_data.get("docket_number", "")
    docket_number = docket_number.replace("/", "-").replace(" ", "_")
    docket_number = re.sub(r"[^A-Za-z0-9._-]+", "_", docket_number).strip("_")
    if not docket_number:
        docket_number = datetime.now().strftime("%Y%m%d_%H%M%S")

    return write_bytes(docket_number, resp.content, out, ".pdf")


# ── Extracción de datos ──────────────────────────────────────────────
def extract_case_summary(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    data = {}

    venue = soup.find(id="docVenueTitleDC")
    dtype = soup.find(id="docTypeCodeTitle")
    seq = soup.find(id="docSeqNumTitle")
    year = soup.find(id="docYeaerTitle")

    parts = [
        venue.get_text(strip=True) if venue else "",
        dtype.get_text(strip=True) if dtype else "",
        seq.get_text(strip=True) if seq else "",
        year.get_text(strip=True) if year else "",
    ]
    data["docket_number"] = "-".join([p for p in parts if p])

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
        ("Current Trial Date", "Current Trial Date:"),
        ("Disposition Date", "Disposition Date:"),
        ("Case Disposition", "Case Disposition:"),
    ]
    for field_name, label_text in label_map:
        vf = soup.find(
            "span",
            class_="ValueField",
            string=lambda s, lt=label_text: s and s.strip() == lt,
        )
        lf = vf.find_next_sibling("span", class_="LabelField") if vf else None
        data[field_name] = lf.get_text(strip=True) if lf else ""

    el = soup.find(id=lambda x: x and "consolidatedCaseN" in x)
    data["Consolidated Case"] = el.get_text(strip=True) if el else ""

    el = soup.find(id=lambda x: x and "jdgmntStatewideLien" in x)
    data["Statewide Lien"] = el.get_text(strip=True) if el else ""

    return data


def export_results(data, out, docket_num="", docket_year=""):
    if not data:
        print("\nNo hay datos para exportar.")
        return

    if docket_num and docket_year:
        safe_name = f"docket_{docket_num}_{docket_year}"
    elif docket_num:
        safe_name = f"docket_{docket_num}"
    else:
        safe_name = f"docket_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", safe_name).strip("_")
    json_p = out / f"{safe_name}.json"
    json_p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[EXPORT] JSON -> {json_p}")
    print(f"{len(data)} casos exportados")


# ── Generador de docket numbers ──────────────────────────────────────
def generate_docket_numbers(start: int = 1, end: int = 10):
    """Genera docket numbers con zero-padding: 000001, 000002, …"""
    for number in range(start, end + 1):
        yield str(number).zfill(6)


# ── Checkpoint ───────────────────────────────────────────────────────
CHECKPOINT_FILENAME = "checkpoint.json"


def checkpoint_path(out: Path) -> Path:
    return out / CHECKPOINT_FILENAME


def save_checkpoint(out: Path, docket_num: str, docket_year: str) -> None:
    data = {
        "last_docket_num": docket_num,
        "last_docket_int": int(docket_num),
        "docket_year": docket_year,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    cp = checkpoint_path(out)
    cp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"[CHECKPOINT] Guardado -> {cp}  (último: {docket_num}/{docket_year})")


def load_checkpoint(out: Path) -> dict | None:
    cp = checkpoint_path(out)
    if not cp.exists():
        return None
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
        log.info(
            f"[CHECKPOINT] Encontrado -> último procesado: "
            f"{data.get('last_docket_num')} / {data.get('docket_year')}  "
            f"(guardado: {data.get('saved_at')})"
        )
        return data
    except Exception as e:
        log.warning(f"[CHECKPOINT] No se pudo leer {cp}: {e}. Se ignora.")
        return None


# ── Main ─────────────────────────────────────────────────────────────
def main(
    otp_code="",
    docket_start: int = 1,
    docket_end: int = 10,
    docket_year: str = "21",
):
    # 1. Verificar credenciales
    if not IPROYAL_USER or not IPROYAL_PASS:
        print("[ERROR] Faltan credenciales IPRoyal.")
        print("  export IPROYAL_USER='tu_usuario'")
        print("  export IPROYAL_PASS='tu_password'")
        return

    # 2. Verificar proxy
    ip = verificar_ip_proxy()
    if not ip:
        print("\n[ERROR] Proxy no disponible. Verifica credenciales IPRoyal.")
        return

    out = make_output_dir()

    # 3. Checkpoint
    cp = load_checkpoint(out)
    if cp:
        last_int = cp.get("last_docket_int", 0)
        resume_from = last_int + 1
        if resume_from > docket_end:
            print(
                f"\n[CHECKPOINT] Ya se procesaron todos los dockets hasta "
                f"{str(last_int).zfill(6)}. Nada por hacer."
            )
            return
        if resume_from > docket_start:
            print(
                f"\n[CHECKPOINT] Retomando desde {str(resume_from).zfill(6)} "
                f"(último completado: {cp['last_docket_num']})."
            )
            docket_start = resume_from

    # 4. Sesión HTTP con proxy IPRoyal
    print("\n[=== curl_cffi: TLS fingerprint Safari + IPRoyal ===]")
    http = get_http_session(probe_url=CONFIG["portal_url"])

    # 5. Login — sesión autenticada se mantiene durante todo el loop
    try:
        http = cffi_login(http, out, otp_code=otp_code)
    except RuntimeError as e:
        print(f"\nLogin falló: {e}")
        return

    print(
        f"\n[=== Búsqueda civil — dockets {str(docket_start).zfill(6)} "
        f"a {str(docket_end).zfill(6)} / año {docket_year} ===]"
    )

    max_retries = CONFIG["max_retries_docket"]

    # 6. Loop de dockets — reutiliza la misma sesión autenticada
    for docket_num in generate_docket_numbers(docket_start, docket_end):
        print(f"\n{'─' * 60}")
        print(f"[DOCKET] Procesando: {docket_num} / {docket_year}")
        print(f"{'─' * 60}")

        data = None

        for attempt in range(1, max_retries + 1):
            try:
                log.info(f"[INTENTO {attempt}/{max_retries}] docket={docket_num}")

                form_soup = navigate_to_civil_search(http, out)
                result = search_civil_case(
                    http,
                    form_soup,
                    out,
                    docket_num=docket_num,
                    docket_year=docket_year,
                )

                if result is None:
                    log.warning(
                        f"[INTENTO {attempt}/{max_retries}] Sin datos. Reintentando..."
                    )
                    time.sleep(2)
                    continue

                data = result
                break

            except Exception as e:
                log.error(f"[INTENTO {attempt}/{max_retries}] Error: {e}")
                if attempt < max_retries:
                    time.sleep(2)
                else:
                    log.error(
                        f"[DOCKET {docket_num}] Falló tras {max_retries} intentos."
                    )

        if data:
            export_results(data, out, docket_num=docket_num, docket_year=docket_year)
            save_checkpoint(out, docket_num, docket_year)
        else:
            log.warning(f"[DOCKET {docket_num}] Sin resultado. Se omite checkpoint.")

    print(
        f"\n[FIN] Proceso completado: dockets "
        f"{str(docket_start).zfill(6)}–{str(docket_end).zfill(6)}."
    )


# ── Entry point ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="NJ Courts Scraper — curl_cffi + IPRoyal + JSF + 2Captcha"
    )
    parser.add_argument("--otp", default="", help="Código OTP 2FA")
    parser.add_argument(
        "--start", type=int, default=1, help="Primer docket (default: 1)"
    )
    parser.add_argument(
        "--end", type=int, default=10, help="Último docket (default: 10)"
    )
    parser.add_argument("--year", default="21", help="Año del docket (default: 21)")
    args = parser.parse_args()

    main(
        otp_code=args.otp,
        docket_start=args.start,
        docket_end=args.end,
        docket_year=args.year,
    )
