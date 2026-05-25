"""
Scraper AFP Capital – Login y obtención del saldo total
Fuente: https://nueva-2.afpcapital.cl/

Usa Playwright (Chromium) para autenticarse y extraer el saldo total
de todas las cuentas. Guarda el resultado diario en data.db (SQLite).

Uso:
    python scraper/scraper.py

Depuración (abre el navegador y guarda screenshots):
    DEBUG_SCRAPER=true python scraper/scraper.py
"""

import asyncio
import logging
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import holidays
from dotenv import load_dotenv
from playwright.async_api import Page, async_playwright

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

load_dotenv()

AFP_RUT: str = os.getenv("AFP_RUT", "")
AFP_PASSWORD: str = os.getenv("AFP_PASSWORD", "")
DB_PATH: Path = Path(os.getenv("DB_PATH", Path(__file__).parent.parent / "data.db"))
DEBUG: bool = os.getenv("DEBUG_SCRAPER", "false").lower() == "true"
# Si está definido, envía el dato a Railway en vez de guardar local
RAILWAY_URL: str = os.getenv("RAILWAY_URL", "")       # ej: https://afp-xxx.up.railway.app
SCRAPER_SECRET: str = os.getenv("SCRAPER_SECRET", "")
SCREENSHOT_DIR: Path = Path(__file__).parent.parent / "logs" / "screenshots"
URL_AFP = "https://nueva-2.afpcapital.cl/"

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS registros (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha       TEXT    UNIQUE NOT NULL,
    valor_cuota REAL,               -- NULL: no disponible en esta fuente
    num_cuotas  REAL,               -- NULL: no disponible en esta fuente
    valor_total REAL    NOT NULL,
    creado_en   TEXT    NOT NULL
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    logger.info("Base de datos lista en %s", DB_PATH)


def guardar_registro(conn: sqlite3.Connection, fecha: date, valor_total: float) -> None:
    ahora = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO registros (fecha, valor_cuota, num_cuotas, valor_total, creado_en)
        VALUES (?, NULL, NULL, ?, ?)
        ON CONFLICT(fecha) DO UPDATE SET
            valor_total = excluded.valor_total,
            creado_en   = excluded.creado_en
        """,
        (fecha.isoformat(), valor_total, ahora),
    )
    conn.commit()
    logger.info("Guardado: %s | Total: $%s CLP", fecha, f"{valor_total:,.0f}")


# ---------------------------------------------------------------------------
# Días hábiles
# ---------------------------------------------------------------------------

def es_dia_habil(fecha: date) -> bool:
    """Retorna True si la fecha es día hábil chileno (lun–vie, no feriado)."""
    if fecha.weekday() >= 5:
        logger.info("%s es fin de semana – no se ejecuta.", fecha)
        return False
    feriados = holidays.Chile(years=fecha.year)
    if fecha in feriados:
        logger.info("%s es feriado (%s) – no se ejecuta.", fecha, feriados[fecha])
        return False
    return True


# ---------------------------------------------------------------------------
# Utilidades Playwright
# ---------------------------------------------------------------------------

async def screenshot(page: Page, nombre: str) -> None:
    """Guarda un screenshot (solo en modo DEBUG)."""
    if not DEBUG:
        return
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ruta = SCREENSHOT_DIR / f"{datetime.now().strftime('%H%M%S')}_{nombre}.png"
    await page.screenshot(path=str(ruta), full_page=True)
    logger.debug("Screenshot guardado: %s", ruta)


def parsear_clp(texto: str) -> Optional[float]:
    """
    Convierte un string con monto CLP a float.
    Acepta: '$12.345.678', '12.345.678', '12.345.678,00'
    """
    limpio = re.sub(r"[^\d.,]", "", texto.strip())
    if not limpio:
        return None
    # Formato chileno: punto = miles, coma = decimal
    limpio = limpio.replace(".", "").replace(",", ".")
    try:
        return float(limpio)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def esperar_formulario(page: Page) -> bool:
    """
    Espera a que la SPA de AFP Capital termine de cargar y muestre el formulario.
    Retorna True si el formulario es visible, False si se agotó el tiempo.
    """
    # 1. Esperar que desaparezca la pantalla "Cargando, por favor espera"
    logger.info("Esperando que la SPA termine de cargar…")
    try:
        await page.wait_for_selector(
            "text=Cargando",
            state="hidden",
            timeout=25_000,
        )
        logger.info("Pantalla de carga desapareció.")
    except Exception:
        logger.info("No se detectó pantalla de carga, continuando…")

    # 2. Margen extra para que React/Angular termine de renderizar
    await page.wait_for_timeout(2_000)
    await screenshot(page, "02_spa_cargada")

    # 3. Esperar a que aparezca al menos un <input> en la página
    try:
        await page.wait_for_selector("input", timeout=15_000)
        logger.info("Formulario de login detectado.")
        return True
    except Exception:
        logger.error(
            "No apareció ningún campo de formulario después de esperar. "
            "Revisa el screenshot 02_spa_cargada para ver el estado actual."
        )
        await screenshot(page, "error_sin_formulario")
        return False


async def _rellenar_campo(page: Page, locator, valor: str) -> None:
    """
    Rellena un campo en SPAs Angular/React de forma robusta.

    Estrategia:
      1. click(force=True) — bypasea overlays y checks de cobertura
      2. JS con native setter — bypasea la protección de React/Angular
         sobre eventos sintéticos y dispara input/change/keyup
      3. Fallback: press_sequentially carácter a carácter
    """
    # 1. Forzar foco ignorando si hay overlay encima
    try:
        await locator.click(force=True, timeout=5_000)
    except Exception as e:
        logger.debug("click(force=True) falló: %s", e)

    # 2. Setear valor por JS con el native setter (React/Angular lo detecta)
    try:
        handle = await locator.element_handle(timeout=3_000)
        if handle:
            await page.evaluate(
                """([el, val]) => {
                    el.focus();
                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value'
                    ).set;
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input',  { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
                }""",
                [handle, valor],
            )
            # Verificar que el valor quedó
            actual = await locator.input_value()
            if actual == valor:
                return
            logger.debug("JS setter: valor en campo='%s', esperado='%s'", actual, valor[:3] + "…")
    except Exception as e:
        logger.debug("JS setter falló: %s", e)

    # 3. Fallback: simular teclas reales
    logger.debug("Fallback: press_sequentially")
    await locator.press_sequentially(valor, delay=80)


async def hacer_login(page: Page) -> bool:
    """
    Navega al portal AFP Capital e inicia sesión con RUT y contraseña.
    Retorna True si el login fue exitoso.

    Nota: usa wait_until="load" (no "networkidle") porque el sitio
    es una SPA Angular/React que nunca alcanza networkidle.
    """
    logger.info("Navegando a %s", URL_AFP)
    # "load" espera el evento window.load, suficiente para SPAs
    await page.goto(URL_AFP, wait_until="load", timeout=30_000)
    await screenshot(page, "01_pagina_inicio")

    # Esperar a que la SPA renderice el formulario
    if not await esperar_formulario(page):
        return False

    await screenshot(page, "03_formulario_visible")

    # ── Campo RUT ──────────────────────────────────────────────────────────
    # Probar selectores de más específico a más genérico
    selectores_rut = [
        'input[name="rut"]',
        'input[id*="rut" i]',
        'input[placeholder*="rut" i]',
        'input[placeholder*="RUT" i]',
        'input[autocomplete="username"]',
        'input[type="text"]',   # sin `:visible` — no es CSS estándar
    ]
    campo_rut = None
    for sel in selectores_rut:
        try:
            campo = page.locator(sel).first
            if await campo.count() > 0 and await campo.is_visible():
                campo_rut = campo
                logger.info("Campo RUT encontrado (selector: %s)", sel)
                break
        except Exception:
            continue

    if campo_rut is None:
        logger.error(
            "No se encontró el campo de RUT. "
            "Revisa el screenshot 03_formulario_visible para ver qué hay en pantalla."
        )
        await screenshot(page, "error_campo_rut")
        return False

    await _rellenar_campo(page, campo_rut, AFP_RUT)
    logger.info("RUT ingresado.")

    # ── Campo contraseña ───────────────────────────────────────────────────
    selectores_pass = [
        'input[type="password"]',
        'input[placeholder*="Clave" i]',
        'input[placeholder*="clave" i]',
        'input[placeholder*="contraseña" i]',
        'input[placeholder*="password" i]',
    ]
    campo_pass = None
    for sel in selectores_pass:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                campo_pass = loc
                logger.info("Campo contraseña encontrado (selector: %s)", sel)
                break
        except Exception:
            continue

    # Fallback: segundo <input> de la página
    if campo_pass is None:
        try:
            loc = page.locator("input").nth(1)
            if await loc.count() > 0:
                campo_pass = loc
                logger.info("Campo contraseña encontrado como segundo input (fallback).")
        except Exception:
            pass

    if campo_pass is None:
        logger.error(
            "No se encontró el campo de contraseña. "
            "Revisa el screenshot 03_formulario_visible."
        )
        await screenshot(page, "error_campo_password")
        return False

    await _rellenar_campo(page, campo_pass, AFP_PASSWORD)
    logger.info("Contraseña ingresada.")

    # Esperar a que Angular valide el formulario (habilita el botón submit)
    await page.wait_for_timeout(1_500)
    await screenshot(page, "04_formulario_completo")

    # ── Botón de envío ─────────────────────────────────────────────────────
    # Usar JS click para bypassear estado disabled de Angular
    selectores_submit = [
        'button[type="submit"]',
        'button:has-text("Iniciar sesión")',
        'button:has-text("Iniciar Sesión")',
        'button:has-text("Ingresar")',
        'button:has-text("Iniciar")',
        'input[type="submit"]',
    ]
    enviado = False
    for sel in selectores_submit:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0:
                # JS click bypasea el disabled de Angular
                await btn.evaluate("el => el.click()")
                enviado = True
                logger.info("Botón presionado vía JS (selector: %s)", sel)
                break
        except Exception:
            continue

    if not enviado:
        # Último recurso: submit del formulario via JS
        logger.info("Botón no encontrado, haciendo submit del form vía JS…")
        await page.evaluate("""
            const form = document.querySelector('form');
            if (form) form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
        """)

    # ── Verificar éxito por cambio de URL (no por keywords) ───────────────
    logger.info("Esperando navegación post-login…")
    try:
        await page.wait_for_url(
            lambda url: "/login" not in url,
            timeout=12_000,
        )
        logger.info("Login exitoso. URL: %s", page.url)
        return True
    except Exception:
        await screenshot(page, "error_login_url_no_cambio")
        html = (await page.content()).lower()
        for err in ["clave incorrecta", "rut inválido", "datos incorrectos",
                    "intente nuevamente", "credenciales"]:
            if err in html:
                logger.error("Credenciales incorrectas ('%s'). Revisa AFP_RUT y AFP_PASSWORD.", err)
                return False
        logger.error(
            "Login falló: la URL no cambió de /login tras 12 s. "
            "Credenciales inválidas o el sitio bloqueó el acceso."
        )
        return False


# ---------------------------------------------------------------------------
# Extracción del saldo
# ---------------------------------------------------------------------------

async def obtener_saldo_total(page: Page) -> Optional[float]:
    """
    Extrae el saldo total de todas las cuentas desde el dashboard.

    Estrategia 1: busca etiquetas conocidas ('saldo total', 'total fondos', etc.)
                  y extrae el monto asociado.
    Estrategia 2: recoge todos los montos CLP de la página y retorna el mayor
                  (que corresponde al total acumulado).
    """
    # Esperar que carguen los datos vía AJAX (sin networkidle, que cuelga en SPAs)
    await page.wait_for_timeout(5_000)
    await screenshot(page, "06_dashboard")

    # ── Estrategia 1: por etiquetas ────────────────────────────────────────
    etiquetas = [
        r"saldo total",
        r"total fondos",
        r"total acumulado",
        r"mis fondos",
        r"total mis cuentas",
        r"valor total",
        r"mis ahorros",
        r"fondo acumulado",
    ]
    for etiqueta in etiquetas:
        try:
            elemento = page.get_by_text(re.compile(etiqueta, re.I)).first
            if await elemento.count() == 0 or not await elemento.is_visible():
                continue

            # Buscar monto en el contenedor padre (1 y 2 niveles hacia arriba)
            for niveles in range(1, 4):
                ancestro = elemento
                for _ in range(niveles):
                    ancestro = ancestro.locator("..")
                texto = await ancestro.inner_text()
                montos = re.findall(r"\$?\s*[\d]{1,3}(?:\.[\d]{3})+(?:,\d+)?", texto)
                for m_str in montos:
                    monto = parsear_clp(m_str)
                    if monto and monto > 500_000:  # mínimo razonable para AFP
                        logger.info(
                            "Saldo encontrado con etiqueta '%s': $%s CLP",
                            etiqueta, f"{monto:,.0f}",
                        )
                        return monto
        except Exception:
            continue

    # ── Estrategia 2: mayor monto en la página ─────────────────────────────
    logger.info("Etiquetas no encontradas. Buscando el mayor monto en la página…")
    html = await page.content()

    patron = re.compile(r"\$?\s*([\d]{1,3}(?:\.[\d]{3})+)(?:,\d{1,2})?")
    candidatos: list[float] = []
    for match in patron.finditer(html):
        monto = parsear_clp(match.group(0))
        if monto and monto > 500_000:
            candidatos.append(monto)

    if candidatos:
        total = max(candidatos)
        logger.info(
            "Saldo total (mayor monto detectado): $%s CLP (%d montos candidatos)",
            f"{total:,.0f}", len(candidatos),
        )
        if not DEBUG:
            logger.warning(
                "Recomendación: ejecuta con DEBUG_SCRAPER=true la primera vez "
                "para verificar visualmente que el monto es correcto."
            )
        return total

    logger.error(
        "No se pudo extraer el saldo. "
        "URL actual: %s  |  Activa DEBUG_SCRAPER=true para depurar.",
        page.url,
    )
    return None


# ---------------------------------------------------------------------------
# Orquestador principal (async)
# ---------------------------------------------------------------------------

async def ejecutar_scraper() -> Optional[float]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            slow_mo=300 if DEBUG else 0,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                # Ocultar que es un browser automatizado
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,800",
            ],
        )
        contexto = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="es-CL",
            timezone_id="America/Santiago",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            # No incluir headers que delatan automatización
            extra_http_headers={
                "Accept-Language": "es-CL,es;q=0.9",
            },
        )
        page = await contexto.new_page()

        # Ocultar navigator.webdriver (lo que detectan los sitios anti-bot)
        await contexto.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['es-CL', 'es'] });
            window.chrome = { runtime: {} };
        """)

        try:
            login_ok = await hacer_login(page)
            if not login_ok:
                return None
            return await obtener_saldo_total(page)
        except Exception as exc:
            logger.error("Error inesperado: %s", exc, exc_info=DEBUG)
            await screenshot(page, "error_inesperado")
            return None
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def main() -> None:
    hoy = date.today()

    if not es_dia_habil(hoy):
        return

    if not AFP_RUT or not AFP_PASSWORD:
        logger.error(
            "AFP_RUT y AFP_PASSWORD deben estar configurados en el archivo .env\n"
            "  Copia .env.example → .env y completa los valores."
        )
        return

    logger.info("Iniciando scraper para %s…", hoy)
    valor_total = asyncio.run(ejecutar_scraper())

    if valor_total is None:
        logger.warning(
            "No se obtuvo el saldo para %s. "
            "Ejecuta con DEBUG_SCRAPER=true para ver qué ocurre.",
            hoy,
        )
        return

    if RAILWAY_URL:
        import json as _json
        import urllib.request

        url = f"{RAILWAY_URL.rstrip('/')}/registros?valor_total={valor_total}"
        req = urllib.request.Request(url, method="POST")
        req.add_header("X-Scraper-Secret", SCRAPER_SECRET)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = _json.loads(resp.read())
            logger.info("Enviado a Railway: %s", data.get("valor_formateado"))
        except Exception as exc:
            logger.error("Error enviando a Railway: %s", exc)
    else:
        with sqlite3.connect(DB_PATH) as conn:
            init_db(conn)
            guardar_registro(conn, hoy, valor_total)


if __name__ == "__main__":
    main()
