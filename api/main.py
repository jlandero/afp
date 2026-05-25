"""
API REST + Scheduler – AFP Capital Fondo A

Endpoints:
    GET /           → sirve el frontend (index.html)
    GET /today      → registro más reciente
    GET /history    → historial completo (params: desde, hasta)
    POST /scraper/run → dispara el scraper manualmente (requiere SCRAPER_SECRET)

El scraper se ejecuta automáticamente L-V a las 01:00 hora Chile.
"""

import logging
import os
import sqlite3
import sys
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from pathlib import Path
from typing import Generator, Optional

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

load_dotenv()

DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).parent.parent / "data.db"))
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
SCRAPER_SECRET = os.getenv("SCRAPER_SECRET", "")  # para proteger /scraper/run

# Agregar raíz del proyecto al path para importar el scraper
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler()


async def tarea_scraper_diaria() -> None:
    """
    Tarea ejecutada por APScheduler L-V a las 01:00 hora Chile.
    Importa el scraper en tiempo de ejecución para evitar carga innecesaria.
    """
    from scraper.scraper import (
        es_dia_habil,
        ejecutar_scraper,
        init_db,
        guardar_registro,
    )

    hoy = date.today()
    logger.info("▶ Tarea programada: scraper iniciado para %s", hoy)

    if not es_dia_habil(hoy):
        logger.info("Hoy no es día hábil – scraper omitido.")
        return

    valor_total = await ejecutar_scraper()

    if valor_total is None:
        logger.warning("Tarea programada: no se obtuvo el saldo.")
        return

    with sqlite3.connect(DB_PATH) as conn:
        init_db(conn)
        guardar_registro(conn, hoy, valor_total)

    logger.info("✔ Tarea programada completada: $%s CLP", f"{valor_total:,.0f}")


# ---------------------------------------------------------------------------
# Ciclo de vida de la app (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────
    scheduler.add_job(
        tarea_scraper_diaria,
        CronTrigger(
            hour=1, minute=0,
            day_of_week="mon-fri",
            timezone=pytz.timezone("America/Santiago"),
        ),
        id="scraper_diario",
        replace_existing=True,
        misfire_grace_time=3_600,   # 1 h de gracia si el contenedor estaba caído
    )
    scheduler.start()
    logger.info("Scheduler iniciado – próxima ejecución: L-V 01:00 hora Chile.")

    # Crear la DB en startup para que /today no falle antes del primer scraper
    with sqlite3.connect(DB_PATH) as conn:
        from scraper.scraper import init_db
        init_db(conn)

    yield
    # ── Shutdown ─────────────────────────────────────────────────────────
    scheduler.shutdown(wait=False)
    logger.info("Scheduler detenido.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    lifespan=lifespan,
    title="AFP Capital Fondo A – Seguimiento de ahorros",
    version="1.0.0",
)

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ---------------------------------------------------------------------------
# Acceso a base de datos
# ---------------------------------------------------------------------------

@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    if not DB_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "La base de datos no existe todavía. "
                "Ejecuta el scraper manualmente: POST /scraper/run"
            ),
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def fila_a_dict(fila: sqlite3.Row) -> dict:
    return {
        "fecha":       fila["fecha"],
        "valor_cuota": fila["valor_cuota"],
        "num_cuotas":  fila["num_cuotas"],
        "valor_total": fila["valor_total"],
        "creado_en":   fila["creado_en"],
    }


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def raiz():
    html = FRONTEND_DIR / "index.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="Frontend no encontrado.")
    return FileResponse(str(html))


@app.get("/today", summary="Registro más reciente")
async def hoy():
    """Retorna el registro más reciente con variación respecto al día anterior y a 30 días."""
    with get_db() as conn:
        filas = conn.execute(
            "SELECT * FROM registros ORDER BY fecha DESC LIMIT 31"
        ).fetchall()

    if not filas:
        raise HTTPException(status_code=404, detail="No hay registros en la base de datos.")

    ultimo = fila_a_dict(filas[0])

    if len(filas) >= 2:
        anterior = filas[1]["valor_total"]
        ultimo["variacion_dia_pct"] = round(
            (ultimo["valor_total"] - anterior) / anterior * 100, 4
        )
    else:
        ultimo["variacion_dia_pct"] = None

    if len(filas) >= 31:
        hace_30 = filas[30]["valor_total"]
        ultimo["variacion_30d_pct"] = round(
            (ultimo["valor_total"] - hace_30) / hace_30 * 100, 4
        )
    else:
        ultimo["variacion_30d_pct"] = None

    return ultimo


@app.get("/history", summary="Historial de registros")
async def historial(
    desde: Optional[str] = Query(None, description="Fecha inicio YYYY-MM-DD"),
    hasta: Optional[str] = Query(None, description="Fecha fin YYYY-MM-DD"),
):
    """Retorna todos los registros ordenados por fecha ascendente."""
    for nombre, valor in [("desde", desde), ("hasta", hasta)]:
        if valor is not None:
            try:
                date.fromisoformat(valor)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"El parámetro '{nombre}' debe tener formato YYYY-MM-DD.",
                )

    with get_db() as conn:
        if desde and hasta:
            filas = conn.execute(
                "SELECT * FROM registros WHERE fecha BETWEEN ? AND ? ORDER BY fecha ASC",
                (desde, hasta),
            ).fetchall()
        elif desde:
            filas = conn.execute(
                "SELECT * FROM registros WHERE fecha >= ? ORDER BY fecha ASC", (desde,)
            ).fetchall()
        elif hasta:
            filas = conn.execute(
                "SELECT * FROM registros WHERE fecha <= ? ORDER BY fecha ASC", (hasta,)
            ).fetchall()
        else:
            filas = conn.execute(
                "SELECT * FROM registros ORDER BY fecha ASC"
            ).fetchall()

    return {"total": len(filas), "registros": [fila_a_dict(f) for f in filas]}


@app.post("/scraper/run", summary="Ejecutar scraper manualmente")
async def ejecutar_manualmente(x_scraper_secret: str = Header(default="")):
    """
    Dispara el scraper y espera el resultado (puede tardar ~60 segundos).
    Requiere el header X-Scraper-Secret con el valor de la variable SCRAPER_SECRET.
    """
    if SCRAPER_SECRET and x_scraper_secret != SCRAPER_SECRET:
        raise HTTPException(status_code=403, detail="Secret inválido.")

    from scraper.scraper import ejecutar_scraper, es_dia_habil, init_db, guardar_registro

    hoy = date.today()
    logger.info("▶ Scraper manual iniciado para %s", hoy)

    try:
        valor_total = await ejecutar_scraper()
    except Exception as exc:
        logger.error("Error en scraper manual: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error en el scraper: {exc}")

    if valor_total is None:
        raise HTTPException(
            status_code=502,
            detail=(
                "El scraper no obtuvo el saldo. "
                "Revisa los logs del servidor para más detalles. "
                "Causas comunes: credenciales incorrectas, sitio AFP no disponible."
            ),
        )

    with sqlite3.connect(DB_PATH) as conn:
        init_db(conn)
        guardar_registro(conn, hoy, valor_total)

    return {
        "ok": True,
        "fecha": hoy.isoformat(),
        "valor_total": valor_total,
        "valor_formateado": f"${valor_total:,.0f} CLP",
    }


# ---------------------------------------------------------------------------
# Arranque local
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
