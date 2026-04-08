"""
INSPECTOR DE SELECTORES — NJ Courts Portal
===========================================
Corre esto PRIMERO para inspeccionar el portal y encontrar
los selectores correctos para tu cuenta.

Uso:
    python inspector.py
"""

import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

PORTAL_URL = "https://portal-cloud.njcourts.gov/prweb/PRAuth/CloudSAMLAuth?AppName=ESSO"
OUT = Path("./output/inspector")
OUT.mkdir(parents=True, exist_ok=True)


async def inspect():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=200)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = await context.new_page()

        print("Navegando al portal...")
        await page.goto(PORTAL_URL, wait_until="networkidle")

        # Guardar HTML completo
        html = await page.content()
        (OUT / "landing.html").write_text(html, encoding="utf-8")
        await page.screenshot(path=str(OUT / "landing.png"), full_page=True)
        print(f"HTML guardado → {OUT / 'landing.html'}")

        # Analizar formularios
        print("\n=== FORMULARIOS ENCONTRADOS ===")
        forms = await page.query_selector_all("form")
        for i, form in enumerate(forms):
            action = await form.get_attribute("action") or ""
            method = await form.get_attribute("method") or "GET"
            print(f"\nForm #{i}: action='{action}' method='{method}'")
            inputs = await form.query_selector_all("input, select, textarea, button")
            for inp in inputs:
                tag = await inp.evaluate("el => el.tagName.toLowerCase()")
                name = await inp.get_attribute("name") or ""
                id_ = await inp.get_attribute("id") or ""
                type_ = await inp.get_attribute("type") or ""
                text = (await inp.inner_text()).strip()[:40]
                print(
                    f"  <{tag}> name='{name}' id='{id_}' type='{type_}' text='{text}'"
                )

        # Analizar todos los inputs
        print("\n=== TODOS LOS INPUTS ===")
        inputs = await page.query_selector_all("input, select, textarea")
        for inp in inputs:
            name = await inp.get_attribute("name") or ""
            id_ = await inp.get_attribute("id") or ""
            type_ = await inp.get_attribute("type") or ""
            placeholder = await inp.get_attribute("placeholder") or ""
            print(
                f"  input: name='{name}' id='{id_}' type='{type_}' placeholder='{placeholder}'"
            )

        # Analizar botones
        print("\n=== BOTONES ===")
        buttons = await page.query_selector_all("button, input[type='submit']")
        for btn in buttons:
            text = (await btn.inner_text()).strip()
            id_ = await btn.get_attribute("id") or ""
            type_ = await btn.get_attribute("type") or ""
            print(f"  button: '{text}' id='{id_}' type='{type_}'")

        # Guardar reporte JSON
        report = {
            "url": page.url,
            "title": await page.title(),
            "forms": len(forms),
            "inputs": [],
            "buttons": [],
        }

        for inp in inputs:
            report["inputs"].append(
                {
                    "name": await inp.get_attribute("name"),
                    "id": await inp.get_attribute("id"),
                    "type": await inp.get_attribute("type"),
                    "placeholder": await inp.get_attribute("placeholder"),
                }
            )

        for btn in buttons:
            report["buttons"].append(
                {
                    "text": (await btn.inner_text()).strip(),
                    "id": await btn.get_attribute("id"),
                    "type": await btn.get_attribute("type"),
                }
            )

        (OUT / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False)
        )
        print(f"\nReporte JSON → {OUT / 'report.json'}")

        print("\n" + "=" * 50)
        print("INSTRUCCIONES:")
        print("1. Revisa output/inspector/landing.html en tu navegador")
        print(
            "2. Usa los nombres/ids de los inputs para actualizar SELECTORS en scraper.py"
        )
        print("3. Ejemplo: si el campo usuario tiene id='UserName', usa '#UserName'")
        print("=" * 50)

        print(
            "\nPuedes interactuar con el navegador abierto. Presiona Enter para cerrar."
        )
        input()

        await browser.close()


if __name__ == "__main__":
    asyncio.run(inspect())
