"""
Test de targets curl_cffi para el portal NJ Courts.

El portal usa IBM ISAM. El flujo correcto es:
  GET portal-cloud (SAML) -> 200 con HTML que tiene JS redirect al IBM ISAM
  GET portal.njcourts.gov/pkmslogin.form -> deberia dar el LoginEntryForm

Este script prueba cada target y reporta que contiene la respuesta.

Uso:
    pip install curl-cffi beautifulsoup4
    python test_targets.py
"""

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
import curl_cffi

print(f"curl_cffi version: {curl_cffi.__version__}\n")

PORTAL_URL = "https://portal-cloud.njcourts.gov/prweb/PRAuth/CloudSAMLAuth?AppName=ESSO"
IDP_URL = "https://portal.njcourts.gov/pkmslogin.form"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/15.5 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

TARGETS = [
    "safari15_5",
    "safari15_3",
    "safari18_0",
    "safari17_0",
    "firefox133",
    "chrome131",
    "chrome124",
    "chrome120",
    "chrome116",
    "chrome110",
    "chrome107",
]


def diagnose(target):
    http = cffi_requests.Session(impersonate=target)
    http.headers.update(HEADERS)
    try:
        # Paso 1: GET portal SAML (inicializa cookies Incapsula)
        r0 = http.get(PORTAL_URL, timeout=15, allow_redirects=True)
        soup0 = BeautifulSoup(r0.text, "html.parser")
        has_form_0 = bool(
            soup0.find("form", {"name": "LoginEntryForm"})
            or soup0.find("input", {"name": "username"})
        )
        has_form_action_0 = bool(
            soup0.find("form", action=lambda a: a and "pkmslogin" in str(a))
        )
        title0 = soup0.title.string.strip() if soup0.title else "(no title)"

        # Paso 2: GET directo al IBM ISAM login page
        r1 = http.get(
            IDP_URL, timeout=15, allow_redirects=True, headers={"Referer": r0.url}
        )
        soup1 = BeautifulSoup(r1.text, "html.parser")
        has_form_1 = bool(
            soup1.find("form", {"name": "LoginEntryForm"})
            or soup1.find("input", {"name": "username"})
        )
        has_form_action_1 = bool(
            soup1.find("form", action=lambda a: a and "pkmslogin" in str(a))
        )
        title1 = soup1.title.string.strip() if soup1.title else "(no title)"

        login_found = has_form_0 or has_form_action_0 or has_form_1 or has_form_action_1
        where = []
        if has_form_0 or has_form_action_0:
            where.append("portal-cloud")
        if has_form_1 or has_form_action_1:
            where.append("pkmslogin.form")

        icon = "+" if login_found else "-"
        loc = f"FORM en: {where}" if where else "sin form de login"
        print(
            f"  [{icon}] {target:15} | "
            f"SAML {r0.status_code} title='{title0[:30]}' | "
            f"IDP {r1.status_code} title='{title1[:30]}' | "
            f"{loc}"
        )

        return login_found, where

    except Exception as e:
        print(f"  [!] {target:15} | ERR: {e}")
        return False, []


if __name__ == "__main__":
    print(f"Diagnosticando {len(TARGETS)} targets\n")
    print(f"  SAML URL : {PORTAL_URL}")
    print(f"  IDP URL  : {IDP_URL}\n")

    working = []
    for target in TARGETS:
        ok, where = diagnose(target)
        if ok:
            working.append((target, where))

    print(f"\n{'=' * 60}")
    if working:
        print("Targets con formulario de login encontrado:")
        for t, w in working:
            print(f"  {t} -> form en: {w}")
        best = working[0][0]
        print(f'\nUsar en CONFIG: "cffi_target": "{best}"')
    else:
        print("Ningun target encontro el formulario de login.")
        print("\nDiagnostico adicional: guardando HTML de la respuesta...")
        # Save HTML for manual inspection
        from pathlib import Path

        Path("debug").mkdir(exist_ok=True)
        http = cffi_requests.Session(impersonate="safari15_5")
        http.headers.update(HEADERS)
        r0 = http.get(PORTAL_URL, timeout=15)
        Path("debug/portal_saml.html").write_text(r0.text, encoding="utf-8")
        r1 = http.get(IDP_URL, timeout=15, headers={"Referer": r0.url})
        Path("debug/pkmslogin.html").write_text(r1.text, encoding="utf-8")
        print("  -> debug/portal_saml.html")
        print("  -> debug/pkmslogin.html")
        print("\nAbre esos archivos para ver que devuelve el servidor.")
        print("Posibles causas:")
        print("  1. El portal bloquea IPs de data centers (usa una IP residencial/VPN)")
        print("  2. Incapsula/Imperva esta bloqueando todos los targets")
        print("  3. El portal cambio su sistema de autenticacion")
