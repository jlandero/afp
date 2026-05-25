#!/usr/bin/env bash
# setup_mac.sh — Configura el scraper AFP para ejecutarse automáticamente en macOS
# Ejecutar una sola vez desde la raíz del proyecto:
#   bash macos/setup_mac.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_SRC="$PROJECT_DIR/macos/com.afpcapital.scraper.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.afpcapital.scraper.plist"
LOG_DIR="$PROJECT_DIR/logs"

echo "=== Setup scraper AFP Capital ==="
echo "Proyecto: $PROJECT_DIR"

# 1. Crear directorio de logs
mkdir -p "$LOG_DIR"
echo "✔ Directorio de logs: $LOG_DIR"

# 2. Crear virtualenv e instalar dependencias (sin Playwright headless para Mac)
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    echo "→ Creando virtualenv..."
    python3 -m venv "$PROJECT_DIR/.venv"
fi
echo "→ Instalando dependencias..."
"$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"
echo "→ Instalando Playwright Chromium..."
"$PROJECT_DIR/.venv/bin/playwright" install chromium
echo "✔ Dependencias instaladas."

# 3. Copiar y cargar el plist en LaunchAgents
cp "$PLIST_SRC" "$PLIST_DST"
echo "✔ Plist copiado a $PLIST_DST"

# Si ya estaba cargado, descargarlo primero
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
echo "✔ LaunchAgent cargado. Se ejecutará L-V a las 18:15."

# 4. Configurar wake automático del Mac a las 18:10 (5 min antes del scraper)
#    pmset repeat: wake todos los días de semana a las 18:10
echo "→ Configurando wake automático (requiere contraseña de admin)..."
sudo pmset repeat wake MTWRF 18:10:00
echo "✔ Wake configurado: Mac se despertará L-V a las 18:10."

echo ""
echo "=== Listo ==="
echo "El scraper correrá automáticamente L-V a las 18:15."
echo "Logs en: $LOG_DIR/scraper.log"
echo ""
echo "IMPORTANTE: Asegúrate de que el plist tenga tus credenciales reales."
echo "Edita: $PLIST_DST"
echo "  AFP_RUT, AFP_PASSWORD, RAILWAY_URL, SCRAPER_SECRET"
echo ""
echo "Para probar manualmente:"
echo "  cd $PROJECT_DIR && .venv/bin/python scraper/scraper.py"
