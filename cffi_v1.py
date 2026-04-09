"""
NJ Courts Civil Case Scraper — HTTP puro con JSF ViewState
==========================================================

Flujo:
  1. Probar targets de impersonación hasta encontrar uno funcional.
  2. Login IBM ISAM (pkmslogin.form) con credenciales + 2FA OTP.
  3. GET formulario civil -> extraer campos JSF y javax.faces.ViewState.
  4. POST formulario de búsqueda civil.
  5. Parsear resultados con BeautifulSoup.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("njcourts")

USER = "CarlosOrtizL"
PASSWORD = "Abcd123456789@"

CONFIG = {
    "username": USER,
    "password": PASSWORD,
    "portal_url": "https://portal-cloud.njcourts.gov/prweb/PRAuth/CloudSAMLAuth?AppName=ESSO",
    "timeout": 30,
    "output_dir": "./output",
    "save_html": True,
    "fcb_api_key": "3d6be63fcc0b8a4482803636f872425f",
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

_IMPERSONATE_TARGETS = [
    "safari18_0",
    "safari15_5",
    "safari15_3",
    "safari17_0",
    "firefox133",
]


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


def get_http_session(probe_url: str | None = None) -> cffi_requests.Session:
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

    log.info(f"[HTTP] All probes failed. Falling back to {_IMPERSONATE_TARGETS[0]}")
    return cffi_requests.Session(impersonate=_IMPERSONATE_TARGETS[0])


def cffi_login(http: cffi_requests.Session, out, otp_code=""):
    log.info(f"[1] GET {CONFIG['portal_url']}")
    resp0 = http.get(
        CONFIG["portal_url"], timeout=CONFIG["timeout"], allow_redirects=True
    )
    log.info(f"  status={resp0.status_code}  url={resp0.url}")
    write_html("01_cffi_landing", resp0.text, out)

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
    if not login_form:
        login_form = find_login_form(soup0)
        if login_form:
            soup = soup0
            resp_login = resp0
        else:
            raise RuntimeError("No se encontro formulario de login")

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
        raise RuntimeError(f"Login fallo. URL: {resp2.url}")

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


def navigate_to_civil_search(http: cffi_requests.Session, out) -> BeautifulSoup:
    esso_url = "https://portal-cloud.njcourts.gov/prweb/PRAuth/app/ESSOPortal/"
    log.info(f"[4] GET {esso_url}")
    resp_esso = http.get(esso_url, timeout=CONFIG["timeout"], allow_redirects=True)
    log.info(f"  status={resp_esso.status_code}  url={resp_esso.url}")
    write_html("04_esso_portal", resp_esso.text, out)

    if "login" in resp_esso.url.lower() or "pkmslogin" in resp_esso.url:
        raise RuntimeError("Sesion invalida")

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
    docket_num="000009",
    docket_year="17",
    court_type="Civil Part",
    county="ATLANTIC",
    docket_type="L",
) -> list:
    log.info(f"\n[6] county={county} docket={docket_num}/{docket_year}")

    form = form_soup.find("form", id="searchByDocForm") or form_soup.find(
        "form", id="civilCaseSearchForm"
    )
    if not form:
        raise RuntimeError("No se encontro el form JSF de busqueda")

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

    for field_name, value in field_mapping.items():
        fields[field_name] = value

    submit_candidates = [
        "searchByDocForm:btnSearch",
        "civilCaseSearchForm:btnSearch",
        "btnSearch",
    ]
    for submit_name in submit_candidates:
        if submit_name in fields:
            fields[submit_name] = fields.get(submit_name) or "Search"
            break
    else:
        fields["searchByDocForm:btnSearch"] = "Search"

    if "javax.faces.ViewState" not in fields:
        raise RuntimeError("No se encontro javax.faces.ViewState en el formulario")

    log.info(f"  ViewState: {fields['javax.faces.ViewState'][:40]}...")
    log.info(f"  Court: {court_type} ({court_value})")
    log.info(f"  County: {county} ({county_code})")

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
    if "caseSummaryDiv" in content or "idCaseTitle" in content:
        return [extract_case_summary(content)]

    return extract_table_data_http(http, content, out)


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


def extract_table_data_http(http: cffi_requests.Session, html: str, out) -> list:
    all_rows = []
    page_num = 1

    while True:
        log.info(f"  Extrayendo pagina {page_num}...")
        soup = BeautifulSoup(html, "html.parser")

        headers = [
            th.get_text(strip=True)
            for th in soup.select("table th")
            if th.get_text(strip=True)
        ]

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

            hrefs = [
                a.get("href", "")
                for a in row.find_all("a", href=True)
                if a.get("href", "")
            ]
            if hrefs:
                rd["_links"] = "; ".join(hrefs)

            page_rows.append(rd)

        if not page_rows:
            break

        all_rows.extend(page_rows)

        next_link = None
        for a in soup.find_all("a"):
            if a.get_text(strip=True).lower() == "next" and not a.get("disabled"):
                next_link = a
                break

        if not next_link:
            break

        href = next_link.get("href", "")
        postback_match = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href)
        onclick = next_link.get("onclick", "")

        if postback_match:
            fields = extract_form_fields(soup)
            fields["__EVENTTARGET"] = postback_match.group(1)
            fields["__EVENTARGUMENT"] = postback_match.group(2)
        elif "mojarra.jsfcljs" in onclick or "jsf" in href.lower():
            fields = extract_form_fields(soup)
            link_id = next_link.get("id", "")
            if link_id:
                fields[link_id] = link_id
        else:
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
        print("\nNo hay datos para exportar.")
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_p = out / f"docket_{ts}.json"
    json_p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[EXPORT] JSON -> {json_p}")
    print(f"{len(data)} casos exportados")


def main(otp_code=""):
    out = make_output_dir()

    print("\n[=== curl_cffi: TLS fingerprint Safari ===]")
    http = get_http_session(probe_url=CONFIG["portal_url"])

    try:
        http = cffi_login(http, out, otp_code=otp_code)
    except RuntimeError as e:
        print(f"\nLogin fallo: {e}")
        return

    print("\n[=== Busqueda civil via HTTP ===]")
    try:
        form_soup = navigate_to_civil_search(http, out)
        data = search_civil_case(http, form_soup, out)
    except Exception as e:
        print(f"\nError en busqueda: {e}")
        return

    export_results(data, out)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="NJ Courts Scraper — HTTP + JSF ViewState"
    )
    parser.add_argument("--otp", default="", help="Codigo OTP 2FA")
    args = parser.parse_args()
    main(otp_code=args.otp)
