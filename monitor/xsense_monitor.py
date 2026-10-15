"""
xsense_monitor.py
Monitor de temperaturas Xsense — Packing Espárrago (Danper).

Qué hace en cada revisión:
  1. Inicia sesión en Xsense con un navegador oculto (Selenium):
       - en tu laptop (Windows) usa Edge,
       - en GitHub Actions (Linux) usa Chrome.
  2. Copia la sesión (cookies) a 'requests' y desde ahí pide los datos
     directamente a la API de Xsense en JSON: sin clics, sin XPaths.
  3. Para cada cámara: rango (mín/máx), promedio actual, última lectura de
     cada sensor con su hora, y el historial de las últimas 24 h.
  4. Para las cámaras FUERA DE RANGO genera la imagen del gráfico
     (docs/graficos/), que se usará en las notificaciones.
  5. Guarda todo en docs/data/estado.json: ese archivo es el que lee la app.

Requisitos (una sola vez):
    pip install -r monitor/requirements.txt

Credenciales (nunca se escriben en el código):
  - En tu laptop: archivo .env (en la raíz del repositorio o en monitor/) con
        XSENSE_EMAIL=correo@danper.com
        XSENSE_PASSWORD=la_contraseña
  - En GitHub: como "Secrets" del repositorio con esos mismos nombres.

Uso (desde la raíz del repositorio):
    python monitor/xsense_monitor.py            -> una sola revisión
    python monitor/xsense_monitor.py --bucle    -> revisa cada 10 min (Ctrl+C para detener)
    python monitor/xsense_monitor.py --ver      -> muestra el navegador (depurar login)
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Para que Git Bash / la consola de Windows muestre bien tildes y "°"
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
MONITOR_DIR = Path(__file__).resolve().parent
REPO_DIR = MONITOR_DIR.parent
DOCS_DIR = REPO_DIR / "docs"                  # carpeta publicada por GitHub Pages
ARCHIVO_ESTADO = DOCS_DIR / "data" / "estado.json"
CARPETA_GRAFICOS = DOCS_DIR / "graficos"

BASE_URL = "https://www.xsenseccm.com"
NODO_PACKING_ESPARRAGO = "5bf4c035-b3fe-41cb-9bdf-426dc08a425c"
NOMBRE_NODO = "Packing Espárrago"

ZONA_LIMA = timezone(timedelta(hours=-5))
HEADERS = {"Accept": "application/json"}  # sin esto Xsense responde en XML

INTERVALO_MINUTOS = 10      # cada cuánto revisa (en --bucle y lo que muestra la app como "Próxima")
MINUTOS_SIN_LECTURA = 30    # sin lecturas en este tiempo -> "SIN LECTURAS" (reportan cada ~8 min)
HORAS_HISTORIAL = 24        # horas de historial para los gráficos

# Botón "INICIAR SESIÓN" (o "SIGN IN"/"LOGIN" si la página sale en inglés), sin importar mayúsculas
_MAYUS = "ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚ"
_MINUS = "abcdefghijklmnopqrstuvwxyzáéíóú"
_TXT = f"translate(normalize-space(.), '{_MAYUS}', '{_MINUS}')"
XPATH_BOTON_LOGIN = (
    f"//*[self::button or self::a or self::span or self::div or self::input]"
    f"[contains({_TXT}, 'iniciar sesi') or contains({_TXT}, 'sign in') or "
    f"{_TXT}='login' or {_TXT}='log in' or {_TXT}='ingresar' or {_TXT}='entrar']"
    f"[not(.//*[contains({_TXT}, 'iniciar sesi') or contains({_TXT}, 'sign in')])]"
)

# Colores del gráfico PNG
COLOR_FONDO = "#fcfcfb"
COLOR_TEXTO = "#0b0b0b"
COLOR_TEXTO_2 = "#52514e"
COLOR_GRILLA = "#e6e5e0"
COLOR_RANGO = "#1baf7a"
COLOR_PROMEDIO = "#2a78d6"
COLOR_FUERA = "#e34948"
COLORES_SENSORES = ["#2a78d6", "#eb6834", "#6250d6"]


# ---------------------------------------------------------------------------
# Credenciales
# ---------------------------------------------------------------------------
def cargar_credenciales():
    """Lee XSENSE_EMAIL y XSENSE_PASSWORD del entorno o de un archivo .env."""
    for archivo_env in (REPO_DIR / ".env", MONITOR_DIR / ".env"):
        if archivo_env.exists():
            for linea in archivo_env.read_text(encoding="utf-8").splitlines():
                linea = linea.strip()
                if linea and not linea.startswith("#") and "=" in linea:
                    clave, valor = linea.split("=", 1)
                    os.environ.setdefault(clave.strip(), valor.strip())

    email = os.environ.get("XSENSE_EMAIL")
    password = os.environ.get("XSENSE_PASSWORD")
    if not email or not password:
        raise SystemExit(
            "Faltan las credenciales XSENSE_EMAIL / XSENSE_PASSWORD "
            "(archivo .env en tu laptop, o Secrets en GitHub)."
        )
    return email, password


# ---------------------------------------------------------------------------
# Login: Selenium solo para entrar; luego todo va por la API
# ---------------------------------------------------------------------------
def _crear_navegador(ver_navegador: bool):
    if sys.platform.startswith("win"):
        from selenium.webdriver.edge.options import Options
        opciones = Options()
        if not ver_navegador:
            opciones.add_argument("--headless=new")
        opciones.add_argument("--window-size=1600,1000")
        return webdriver.Edge(options=opciones)

    # Linux (GitHub Actions) u otros: Chrome
    from selenium.webdriver.chrome.options import Options
    opciones = Options()
    if not ver_navegador:
        opciones.add_argument("--headless=new")
    opciones.add_argument("--window-size=1600,1000")
    opciones.add_argument("--no-sandbox")
    opciones.add_argument("--disable-dev-shm-usage")
    # El servidor de GitHub está en inglés: forzamos español para que Xsense
    # muestre la misma página que en la laptop
    opciones.add_argument("--lang=es-PE")
    opciones.add_experimental_option("prefs", {"intl.accept_languages": "es-PE,es"})
    return webdriver.Chrome(options=opciones)


def _sesion_funciona(sesion: requests.Session) -> bool:
    try:
        r = sesion.get(f"{BASE_URL}/rest/item/list/{NODO_PACKING_ESPARRAGO}",
                       params={"scenario": "FM"}, timeout=15)
        return (r.ok and r.headers.get("Content-Type", "").startswith("application/json")
                and r.json().get("result") == "OK")
    except Exception:
        return False


def _escribir(wait, xpath, texto, intentos=5):
    ultimo_error = None
    for _ in range(intentos):
        try:
            campo = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
            campo.click()
            campo.clear()
            campo.send_keys(texto)
            return campo
        except Exception as e:  # el formulario a veces tarda en "activarse"
            ultimo_error = e
            time.sleep(1)
    raise RuntimeError(f"No se pudo escribir en el campo de login: {ultimo_error}")


def iniciar_sesion(email: str, password: str, ver_navegador: bool = False) -> requests.Session:
    driver = _crear_navegador(ver_navegador)
    try:
        wait = WebDriverWait(driver, 30)
        driver.get(BASE_URL + "/")
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(1.5)

        _escribir(wait, "//input[@type='email' or contains(@placeholder,'correo')]", email)
        campo_clave = _escribir(wait, "//input[@type='password']", password)

        # Botón de ingreso: en español o en inglés (según el idioma del navegador).
        # Si no aparece, se envía el formulario con Enter desde la contraseña.
        try:
            boton = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH, XPATH_BOTON_LOGIN)))
            boton.click()
        except Exception:
            campo_clave.send_keys(Keys.ENTER)

        sesion = requests.Session()
        sesion.headers.update(HEADERS)
        for _ in range(30):
            time.sleep(1)
            sesion.cookies.clear()
            for c in driver.get_cookies():
                sesion.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
            if _sesion_funciona(sesion):
                return sesion
        raise RuntimeError("Se envió el login pero la API no respondió "
                           "(usuario/contraseña incorrectos o Xsense cambió su página).")
    except Exception:
        _guardar_diagnostico(driver)
        raise
    finally:
        driver.quit()


def _guardar_diagnostico(driver):
    """Si el login falla, guarda una captura y el HTML de la página para revisar qué pasó."""
    try:
        driver.save_screenshot(str(MONITOR_DIR / "diagnostico_login.png"))
        (MONITOR_DIR / "diagnostico_login.html").write_text(driver.page_source, encoding="utf-8")
        print("   (se guardó diagnostico_login.png / .html para revisar el error)")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Lectura de datos
# ---------------------------------------------------------------------------
def obtener_camaras(sesion) -> list:
    r = sesion.get(f"{BASE_URL}/rest/item/list/{NODO_PACKING_ESPARRAGO}", params={
        "scenario": "FM", "includeEnhancements": "false", "includeAlerts": "true",
        "includeThresholds": "true", "showArchived": "false",
        "includeTimezone": "true", "includeAverages": "true"}, timeout=30)
    r.raise_for_status()
    return r.json()["data"]["items"]


def obtener_detalle(sesion, item_id: str) -> dict:
    r = sesion.get(f"{BASE_URL}/rest/item/details", params={
        "itemId": item_id, "scenario": "FM", "readings": "true",
        "rules": "true", "includeAverages": "true"}, timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def obtener_lecturas(sesion, item_id: str, promedio: bool, horas: int = HORAS_HISTORIAL) -> dict:
    """Historial de temperatura (lo mismo que dibuja el gráfico de Xsense)."""
    fin = int(time.time() * 1000)
    colores = {"MI_AVG": "#465666", "MI_MIN": "#F08040", "MI_MAX": "#F08040",
               "min": "#00CC00", "max": "#00CC00"}
    r = sesion.get(f"{BASE_URL}/rest/readings/item/sensor", params={
        "nodeId": NODO_PACKING_ESPARRAGO, "itemId": item_id,
        "startTime": fin - horas * 3600 * 1000, "endTime": fin,
        "sensorType": "TEMP", "average": "true" if promedio else "false",
        "includeAlerts": "true", "scenario": "FM", "formatAsChartData": "true",
        "usedColorsJson": json.dumps(colores, separators=(",", ":")),
        "populateDoorSensors": "false", "trendsWindowSize": 0}, timeout=60)
    r.raise_for_status()
    return r.json()["data"]


def serie_promedio(data: dict) -> list:
    """[[ms, valor], ...] con el promedio de la cámara."""
    puntos = [[p["x"], p["MI_AVG"]] for p in data.get("sensorReadings") or []
              if isinstance(p.get("MI_AVG"), (int, float)) and p.get("x")]
    return sorted(puntos)


def series_por_sensor(data: dict) -> dict:
    """{id_sensor: [[ms, valor], ...]}"""
    ids = [t["id"] for t in data.get("tags") or []
           if t.get("id") not in ("ALERTS", "MI_AVG", "MI_MIN", "MI_MAX")]
    series = {i: [] for i in ids}
    for p in data.get("sensorReadings") or []:
        for i in ids:
            if isinstance(p.get(i), (int, float)) and p.get("x"):
                series[i].append([p["x"], p[i]])
    return {i: sorted(v) for i, v in series.items() if v}


# ---------------------------------------------------------------------------
# Evaluación
# ---------------------------------------------------------------------------
def _hora_lima(ms):
    return datetime.fromtimestamp(ms / 1000, ZONA_LIMA) if ms else None


def _fuera(valor, t_min, t_max, tol=0.0):
    return (valor is not None and t_min is not None and t_max is not None
            and (valor > t_max + tol + 1e-9 or valor < t_min - tol - 1e-9))


def tolerancia_xsense(detalle: dict):
    """Tolerancia (°C) de la regla 'Temp. fuera del intervalo' que la cámara tiene en Xsense.
    Devuelve None si la cámara no tiene esa regla activa (entonces no se aplica tolerancia)."""
    for regla in detalle.get("rules") or []:
        params = regla.get("params") or {}
        if (regla.get("type") == "THRESHOLD_BREACH" and params.get("SENSOR_TYPE", "TEMP") == "TEMP"
                and regla.get("active", True) and regla.get("applied", True) and not regla.get("archived")):
            try:
                return abs(float(params.get("OFFSET") or 0))
            except (TypeError, ValueError):
                return 0.0
    return None


def evaluar(item: dict, detalle: dict) -> dict:
    umbral = item.get("currentThreshold") or detalle.get("currentThreshold") or {}
    t_min = (umbral.get("min") or {}).get("1")  # "1" = temperatura, "2" = humedad
    t_max = (umbral.get("max") or {}).get("1")
    promedio = (detalle.get("averages") or item.get("averages") or {}).get("TEMP")
    regla_tol = tolerancia_xsense(detalle)      # None = la cámara no tiene regla de alerta en Xsense
    tol = regla_tol or 0.0

    sensores = []
    for tag in detalle.get("tags") or []:
        for lectura in tag.get("lastSensorReadings") or []:
            if lectura.get("readingType") == "TEMP":
                ms = lectura.get("readingDate")
                valor = lectura.get("readingValue")
                sensores.append({
                    "id": tag.get("id"),
                    "descripcion": tag.get("linkDescription") or f"Sensor {tag.get('id')}",
                    "valor": valor,
                    "fecha_ms": ms,
                    "hora": _hora_lima(ms).strftime("%Y-%m-%d %H:%M") if ms else None,
                    "fuera_de_rango": _fuera(valor, t_min, t_max, tol),
                    "fuera_segun_xsense": bool(lectura.get("breach")),
                })

    ultima_ms = max((s["fecha_ms"] for s in sensores if s["fecha_ms"]), default=None)
    ultima = _hora_lima(ultima_ms)

    if t_min is None or t_max is None:
        estado = "SIN_RANGO"
    elif ultima is None or datetime.now(ZONA_LIMA) - ultima > timedelta(minutes=MINUTOS_SIN_LECTURA):
        estado = "SIN_LECTURAS"
    elif promedio is None:
        estado = "SIN_DATO"
    elif promedio > t_max + tol + 1e-9:
        estado = "ALTO"
    elif promedio < t_min - tol - 1e-9:
        estado = "BAJO"
    elif promedio > t_max or promedio < t_min:
        estado = "LIMITE"       # pasó el rango pero está dentro de la tolerancia de Xsense: no se alerta
    else:
        estado = "OK"

    return {
        "id": item.get("id"),
        "nombre": item.get("name"),
        "tipo": item.get("type"),
        "min": t_min,
        "max": t_max,
        "tolerancia": tol,
        "regla_xsense": regla_tol is not None,
        "promedio": promedio,
        "humedad": (detalle.get("averages") or item.get("averages") or {}).get("RH"),
        "ultima_lectura": ultima.strftime("%Y-%m-%d %H:%M") if ultima else None,
        "ultima_lectura_ms": ultima_ms,
        "estado": estado,
        "sensores_fuera": sum(1 for s in sensores if s["fuera_de_rango"]),
        "sensores": sensores,
        "grafico": None,
        "serie": [],
        "series_sensores": {},
    }


# ---------------------------------------------------------------------------
# Gráfico PNG (para la notificación)
# ---------------------------------------------------------------------------
def _nombre_archivo(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", texto).strip("_")


def generar_grafico(camara: dict, ruta: Path) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    t_min, t_max = camara["min"], camara["max"]
    a_fecha = lambda ms: datetime.fromtimestamp(ms / 1000, ZONA_LIMA)  # noqa: E731
    nombres = {s["id"]: s["descripcion"] for s in camara["sensores"]}
    sensores = camara["series_sensores"]
    varios = len(sensores) > 1

    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=150)
    fig.patch.set_facecolor(COLOR_FONDO)
    ax.set_facecolor(COLOR_FONDO)
    ax.axhspan(t_min, t_max, color=COLOR_RANGO, alpha=0.12, lw=0, label=f"Rango {t_min:g}–{t_max:g} °C")
    for limite in (t_min, t_max):
        ax.axhline(limite, color=COLOR_RANGO, lw=1, ls=(0, (4, 3)))
    valores = [t_min, t_max]

    if varios:
        for (sid, puntos), color in zip(sensores.items(), COLORES_SENSORES):
            ax.plot([a_fecha(x) for x, _ in puntos], [v for _, v in puntos],
                    color=color, lw=1.2, alpha=0.85, label=nombres.get(sid, sid)[:40])
            valores += [v for _, v in puntos]

    serie = camara["serie"]
    if serie:
        xs = [a_fecha(x) for x, _ in serie]
        ys = [v for _, v in serie]
        valores += ys
        ax.plot(xs, ys, color=COLOR_TEXTO if varios else COLOR_PROMEDIO, lw=2.2, label="Promedio")
        tol = camara.get("tolerancia") or 0
        fuera = [(x, y) for x, y in zip(xs, ys) if y > t_max + tol or y < t_min - tol]
        if fuera:
            ax.scatter([x for x, _ in fuera], [y for _, y in fuera], s=12, color=COLOR_FUERA,
                       zorder=3, label="Fuera de rango")
        ax.scatter([xs[-1]], [ys[-1]], s=40, color=COLOR_FUERA, zorder=4,
                   edgecolors=COLOR_FONDO, linewidths=1.5)
        ax.annotate(f"{ys[-1]:.1f} °C · {xs[-1]:%H:%M}", (xs[-1], ys[-1]), xytext=(-8, 10),
                    textcoords="offset points", ha="right", fontsize=10,
                    fontweight="bold", color=COLOR_TEXTO)

    margen = max(0.5, (max(valores) - min(valores)) * 0.15)
    ax.set_ylim(min(valores) - margen, max(valores) + margen)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=ZONA_LIMA))
    ax.grid(axis="y", color=COLOR_GRILLA, lw=0.8)
    ax.set_axisbelow(True)
    for lado in ("top", "right", "left"):
        ax.spines[lado].set_visible(False)
    ax.spines["bottom"].set_color(COLOR_GRILLA)
    ax.tick_params(colors=COLOR_TEXTO_2, labelsize=8, length=0)
    ax.set_ylabel("°C", color=COLOR_TEXTO_2, fontsize=9)

    estado = {"ALTO": "por ENCIMA del máximo", "BAJO": "por DEBAJO del mínimo"}.get(
        camara["estado"], "dentro del rango")
    ax.set_title(f"{camara['nombre']}\n", loc="left", fontsize=12, fontweight="bold", color=COLOR_TEXTO)
    ax.text(0, 1.02, f"Promedio {camara['promedio']:.1f} °C — {estado} · últimas {HORAS_HISTORIAL} h",
            transform=ax.transAxes, fontsize=9, color=COLOR_TEXTO_2)
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.1), ncol=3, fontsize=8,
              frameon=False, labelcolor=COLOR_TEXTO_2)

    fig.tight_layout()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(ruta, facecolor=COLOR_FONDO)
    plt.close(fig)
    return ruta


# ---------------------------------------------------------------------------
# Consola
# ---------------------------------------------------------------------------
ICONOS = {"OK": "OK ", "ALTO": "ALTO", "BAJO": "BAJO", "LIMITE": "LÍMITE", "SIN_RANGO": "----",
          "SIN_LECTURAS": "SIN LECT.", "SIN_DATO": "?"}


def imprimir(resultados: list):
    print()
    print(f"{'Cámara':<34} {'Rango °C':<12} {'Prom.':>6}  {'Hora':<6}  Estado")
    print("-" * 75)
    for r in resultados:
        rango = f"{r['min']}–{r['max']}" if r["min"] is not None else "sin rango"
        if r.get("tolerancia"):
            rango += f" ±{r['tolerancia']:g}"
        prom = f"{r['promedio']:.1f}" if r["promedio"] is not None else "-"
        hora = r["ultima_lectura"][-5:] if r["ultima_lectura"] else "-"
        print(f"{r['nombre'][:34]:<34} {rango:<12} {prom:>6}  {hora:<6}  {ICONOS.get(r['estado'], r['estado'])}")
        if len(r["sensores"]) > 1:
            for s in r["sensores"]:
                marca = " <- fuera de rango" if s["fuera_de_rango"] else ""
                print(f"     · {s['descripcion'][:40]:<40} {s['valor']:>5} °C  {(s['hora'] or '')[-5:]}{marca}")
    desviadas = [r for r in resultados if r["estado"] in ("ALTO", "BAJO", "SIN_LECTURAS")]
    print()
    if desviadas:
        print(f"⚠  {len(desviadas)} cámara(s) con desviación:")
        for r in desviadas:
            print(f"   - {r['nombre']}: {r['promedio']} °C (rango {r['min']}–{r['max']}) "
                  f"a las {r['ultima_lectura']}  [{r['estado']}]")
    else:
        print("✓ Todas las cámaras con rango definido están dentro de su rango.")


# ---------------------------------------------------------------------------
# Una revisión completa
# ---------------------------------------------------------------------------
def revisar(sesion) -> dict:
    CARPETA_GRAFICOS.mkdir(parents=True, exist_ok=True)
    for viejo in CARPETA_GRAFICOS.glob("*.png"):
        viejo.unlink()

    resultados = []
    for item in sorted(obtener_camaras(sesion), key=lambda i: i.get("name", "")):
        res = evaluar(item, obtener_detalle(sesion, item["id"]))
        try:
            res["serie"] = serie_promedio(obtener_lecturas(sesion, item["id"], promedio=True))
            if len(res["sensores"]) > 1:
                res["series_sensores"] = series_por_sensor(
                    obtener_lecturas(sesion, item["id"], promedio=False))
        except Exception as e:
            print(f"   (no se pudo leer el historial de {res['nombre']}: {e})")

        if res["estado"] in ("ALTO", "BAJO") and res["serie"]:
            try:
                ruta = CARPETA_GRAFICOS / f"{_nombre_archivo(res['nombre'])}.png"
                generar_grafico(res, ruta)
                res["grafico"] = ruta.relative_to(DOCS_DIR).as_posix()
            except Exception as e:
                print(f"   (no se pudo generar el gráfico de {res['nombre']}: {e})")
        resultados.append(res)

    imprimir(resultados)
    if os.environ.get("GITHUB_ACTIONS"):
        tol = "; ".join(f"{r['nombre'][:2]}:{('±%g' % r['tolerancia']) if r['regla_xsense'] else 'sin regla'}"
                        f"→{r['estado']}" for r in resultados)
        print(f"::notice::Tolerancias Xsense y estado: {tol}")
    estado = {
        "generado": datetime.now(ZONA_LIMA).strftime("%Y-%m-%d %H:%M:%S"),
        "generado_ms": int(time.time() * 1000),
        "nodo": NOMBRE_NODO,
        "intervalo_minutos": INTERVALO_MINUTOS,
        "horas_historial": HORAS_HISTORIAL,
        "camaras": resultados,
    }
    ARCHIVO_ESTADO.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVO_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, separators=(",", ":")),
                              encoding="utf-8")
    print(f"\nEstado guardado en: {ARCHIVO_ESTADO.relative_to(REPO_DIR).as_posix()}")
    return estado


def main():
    ver_navegador = "--ver" in sys.argv
    en_bucle = "--bucle" in sys.argv
    email, password = cargar_credenciales()

    sesion = None
    try:
        while True:
            inicio = time.time()
            print(f"\n===== Revisión {datetime.now(ZONA_LIMA):%Y-%m-%d %H:%M} =====")
            try:
                if sesion is None or not _sesion_funciona(sesion):
                    print("Iniciando sesión en Xsense...")
                    sesion = iniciar_sesion(email, password, ver_navegador=ver_navegador)
                    print("Sesión OK.")
                print(f"Leyendo las cámaras de {NOMBRE_NODO}...")
                revisar(sesion)
            except Exception as e:
                if not en_bucle:
                    raise
                print(f"ERROR en esta revisión: {e}")
                sesion = None

            if not en_bucle:
                break
            espera = INTERVALO_MINUTOS * 60 - (time.time() - inicio)
            print(f"Próxima revisión en {INTERVALO_MINUTOS} min (Ctrl+C para detener)...")
            time.sleep(max(espera, 30))
    except KeyboardInterrupt:
        print("\nMonitoreo detenido.")


if __name__ == "__main__":
    main()
