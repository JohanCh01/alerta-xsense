"""
notificar.py
Envía las alertas al celular (notificaciones push con Firebase Cloud Messaging).

Se ejecuta en GitHub Actions DESPUÉS de publicar la app, así la imagen del
gráfico ya está en línea cuando llega la notificación.

Reglas de aviso (por cámara):
  - Sale de rango (Alto/Bajo) o deja de reportar  -> aviso inmediato
  - Sigue fuera de rango                          -> recordatorio cada 30 min
  - Vuelve a su rango                             -> aviso de "volvió a la normalidad"
  - El monitor no pudo revisar Xsense (--error)   -> aviso, como máximo 1 por hora

El estado de las alertas (qué cámaras ya se avisaron y cuándo) se guarda en
Firestore, porque cada revisión de GitHub empieza "desde cero".

Credenciales: secreto FIREBASE_SERVICE_ACCOUNT (contenido del .json de la
cuenta de servicio de Firebase). Si no está configurado, no envía nada.

Uso:
    python monitor/notificar.py            -> procesa docs/data/estado.json
    python monitor/notificar.py --error    -> avisa que el monitor falló
    python monitor/notificar.py --prueba   -> envía una notificación de prueba
"""

import json
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

MONITOR_DIR = Path(__file__).resolve().parent
ARCHIVO_ESTADO = MONITOR_DIR.parent / "docs" / "data" / "estado.json"
URL_APP = "https://johanch01.github.io/alerta-xsense/"

# "Timbre": cuando hay algo que atender, el celular suena y vibra TIMBRES veces seguidas,
# sin importar cuántas cámaras estén desviadas. Las alertas de cada cámara llegan en silencio
# (quedan en la bandeja con su detalle) y un aviso-resumen es el que suena.
TIMBRES = 3
SEGUNDOS_ENTRE_TIMBRES = 4      # Android ignora sonidos de una misma app demasiado seguidos
TIPOS_QUE_SUENAN = ("desviacion", "recordatorio", "error", "prueba")

RECORDATORIO_MIN = 30          # cada cuánto se repite el aviso si sigue fuera de rango
AVISO_ERROR_MIN = 60           # como máximo un aviso de "monitor caído" por hora
ESTADOS_ALERTA = ("ALTO", "BAJO", "SIN_LECTURAS")

PROYECTO_FIREBASE = "alerta-xsense"
COLECCION_DISPOSITIVOS = "dispositivos"
DOC_CONTROL = ("control", "alertas")


# ---------------------------------------------------------------------------
# Textos de las notificaciones
# ---------------------------------------------------------------------------
def _r(v):
    return f"{v:g}" if isinstance(v, (int, float)) else "–"


def _t(v):
    return f"{v:.1f}" if isinstance(v, (int, float)) else "–"


def _duracion(ms):
    minutos = max(1, round(ms / 60000))
    if minutos < 60:
        return f"{minutos} min"
    h, m = divmod(minutos, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def _hora(c):
    return (c.get("ultima_lectura") or "")[-5:] or "–"


def _detalle_desvio(c):
    if c["estado"] == "ALTO":
        return f"{_t(c['promedio'] - c['max'])} °C por encima del máximo"
    if c["estado"] == "BAJO":
        return f"{_t(c['min'] - c['promedio'])} °C por debajo del mínimo"
    return ""


def _sensores_fuera(c):
    n, total = c.get("sensores_fuera", 0), len(c.get("sensores", []))
    return f" · {n} de {total} sensores fuera" if total > 1 and n else ""


def mensaje_nueva(c):
    if c["estado"] == "SIN_LECTURAS":
        return ("📡 Sin lecturas — " + c["nombre"],
                f"Los sensores no reportan desde las {_hora(c)}. Revisa el equipo o la conexión.")
    return ("⚠️ Desviación — " + c["nombre"],
            f"{_t(c['promedio'])} °C · rango {_r(c['min'])}–{_r(c['max'])} °C · lectura {_hora(c)}\n"
            f"{_detalle_desvio(c)}{_sensores_fuera(c)}")


def mensaje_recordatorio(c, fuera_ms):
    if c["estado"] == "SIN_LECTURAS":
        return ("📡 Sigue sin lecturas — " + c["nombre"],
                f"Sin datos desde las {_hora(c)} (hace {_duracion(fuera_ms)}).")
    return ("⏱ Sigue fuera de rango — " + c["nombre"],
            f"{_t(c['promedio'])} °C · rango {_r(c['min'])}–{_r(c['max'])} °C · lectura {_hora(c)}\n"
            f"Lleva {_duracion(fuera_ms)} fuera de rango{_sensores_fuera(c)}")


def mensaje_normal(c, fuera_ms):
    return ("✅ Volvió a la normalidad — " + c["nombre"],
            f"{_t(c['promedio'])} °C · rango {_r(c['min'])}–{_r(c['max'])} °C · lectura {_hora(c)}\n"
            f"Estuvo fuera de rango {_duracion(fuera_ms)}")


# ---------------------------------------------------------------------------
# Lógica de qué avisar (sin Firebase: se puede probar sola)
# ---------------------------------------------------------------------------
def decidir_avisos(estado: dict, previo: dict, ahora_ms: int):
    """Devuelve (avisos, nuevo_estado_de_alertas).
    previo / nuevo: {id_camara: {"estado", "desde_ms", "ultimo_aviso_ms"}}"""
    avisos, nuevo = [], {}
    base_img = URL_APP
    version = estado.get("generado_ms", ahora_ms)

    for c in estado.get("camaras", []):
        cid = c["id"]
        antes = previo.get(cid)
        url = f"{URL_APP}#c/{cid}"
        imagen = f"{base_img}{c['grafico']}?v={version}" if c.get("grafico") else ""

        if c["estado"] in ESTADOS_ALERTA:
            if not antes:
                titulo, cuerpo = mensaje_nueva(c)
                nuevo[cid] = {"estado": c["estado"], "desde_ms": ahora_ms, "ultimo_aviso_ms": ahora_ms}
                avisos.append(dict(tipo="desviacion", titulo=titulo, cuerpo=cuerpo, url=url, imagen=imagen, tag=cid))
            elif antes["estado"] != c["estado"]:
                # p. ej. pasó de ALTO a SIN_LECTURAS: se avisa como algo nuevo, conservando desde cuándo
                titulo, cuerpo = mensaje_nueva(c)
                nuevo[cid] = {"estado": c["estado"], "desde_ms": antes["desde_ms"], "ultimo_aviso_ms": ahora_ms}
                avisos.append(dict(tipo="desviacion", titulo=titulo, cuerpo=cuerpo, url=url, imagen=imagen, tag=cid))
            elif ahora_ms - antes["ultimo_aviso_ms"] >= RECORDATORIO_MIN * 60000 - 90000:  # 1.5 min de margen
                titulo, cuerpo = mensaje_recordatorio(c, ahora_ms - antes["desde_ms"])
                nuevo[cid] = {**antes, "ultimo_aviso_ms": ahora_ms}
                avisos.append(dict(tipo="recordatorio", titulo=titulo, cuerpo=cuerpo, url=url, imagen=imagen, tag=cid))
            else:
                nuevo[cid] = antes
        elif antes:
            titulo, cuerpo = mensaje_normal(c, ahora_ms - antes["desde_ms"])
            avisos.append(dict(tipo="normal", titulo=titulo, cuerpo=cuerpo, url=url, imagen="", tag=cid))
            # al no copiarlo a "nuevo", la cámara queda libre para una próxima alerta

    # Cámaras que ya no aparecen en Xsense: se olvidan sin avisar
    return avisos, nuevo


# ---------------------------------------------------------------------------
# Firebase
# ---------------------------------------------------------------------------
def conectar():
    credencial_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if not credencial_json:
        print("FIREBASE_SERVICE_ACCOUNT no está configurado: no se envían notificaciones.")
        return None
    import firebase_admin
    from firebase_admin import credentials, firestore
    try:
        datos = json.loads(credencial_json)
    except json.JSONDecodeError:
        _anotar("error", "FIREBASE_SERVICE_ACCOUNT no es un JSON válido: vuelve a pegar el contenido completo del .json.")
        raise SystemExit(1)
    proyecto = datos.get("project_id")
    print(f"Cuenta de servicio: {datos.get('client_email')} (proyecto {proyecto})")
    if datos.get("type") != "service_account":
        _anotar("error", "FIREBASE_SERVICE_ACCOUNT no es una clave de cuenta de servicio "
                         "(Configuración → Cuentas de servicio → Generar nueva clave privada).")
        raise SystemExit(1)
    if proyecto != PROYECTO_FIREBASE:
        _anotar("error", f"La clave es del proyecto '{proyecto}', no de '{PROYECTO_FIREBASE}'. "
                         "Genera la clave desde el proyecto alerta-xsense de Firebase.")
        raise SystemExit(1)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(datos))
    return firestore.client()


def _anotar(nivel: str, texto: str):
    """Mensaje que GitHub muestra en el resumen de la ejecución (nivel: notice / warning / error)."""
    print(f"::{nivel}::{texto}")


def leer_control(db) -> dict:
    doc = db.collection(DOC_CONTROL[0]).document(DOC_CONTROL[1]).get()
    return (doc.to_dict() or {}) if doc.exists else {}


def guardar_control(db, control: dict):
    db.collection(DOC_CONTROL[0]).document(DOC_CONTROL[1]).set(control)


def enviar(db, avisos: list) -> int:
    """Envía cada aviso a todos los celulares registrados. Devuelve cuántos se entregaron."""
    if not avisos:
        return 0
    from firebase_admin import messaging

    dispositivos = [(d.id, (d.to_dict() or {}).get("token")) for d in db.collection(COLECCION_DISPOSITIVOS).stream()]
    dispositivos = [(i, t) for i, t in dispositivos if t]
    if not dispositivos:
        _anotar("warning", "No hay celulares registrados todavía: nadie tocó 'Activar alertas' en la app.")
        return 0

    ahora = str(int(time.time() * 1000))
    mensajes, destinos = [], []
    for aviso in avisos:
        datos = {k: str(v) for k, v in aviso.items() if v not in (None, "")}
        datos["ts"] = ahora
        for doc_id, token in dispositivos:
            mensajes.append(messaging.Message(
                token=token,
                data=datos,
                webpush=messaging.WebpushConfig(headers={"Urgency": "high", "TTL": "3600"}),
            ))
            destinos.append(doc_id)

    entregados, invalidos = 0, set()
    for i in range(0, len(mensajes), 500):
        respuesta = messaging.send_each(mensajes[i:i + 500])
        for j, r in enumerate(respuesta.responses):
            if r.success:
                entregados += 1
            else:
                err = r.exception
                codigo = getattr(err, "code", "") or type(err).__name__
                if isinstance(err, messaging.UnregisteredError) or "not-registered" in str(codigo).lower() \
                        or "invalid-argument" in str(codigo).lower():
                    invalidos.add(destinos[i + j])
                else:
                    print(f"   envío fallido ({codigo}): {err}")

    for doc_id in invalidos:  # celulares que desinstalaron la app o bloquearon las notificaciones
        db.collection(COLECCION_DISPOSITIVOS).document(doc_id).delete()
    _anotar("notice", f"Notificaciones: {len(avisos)} aviso(s) × {len(dispositivos)} celular(es) → "
                      f"{entregados} entregadas, {len(invalidos)} celular(es) dado(s) de baja.")
    return entregados


def resumen_timbre(avisos: list) -> dict:
    """Un solo aviso que resume todo lo de esta revisión (es el que suena y vibra)."""
    camaras = [a for a in avisos if a["tipo"] in ("desviacion", "recordatorio")]
    nuevas = [a for a in camaras if a["tipo"] == "desviacion"]
    if any(a["tipo"] == "error" for a in avisos):
        titulo, cuerpo = "🚨 El monitor no pudo revisar Xsense", "Revisa Xsense directamente."
    elif any(a["tipo"] == "prueba" for a in avisos):
        titulo, cuerpo = "🚨 Prueba de timbre", "Así sonará y vibrará el celular cuando haya una desviación."
    else:
        nombres = [a["titulo"].split(" — ", 1)[-1] for a in camaras]
        n = len(camaras)
        titulo = f"🚨 {n} cámara{'s' if n != 1 else ''} fuera de rango" + (f" ({len(nuevas)} nueva{'s' if len(nuevas) != 1 else ''})" if nuevas and len(nuevas) < n else "")
        cuerpo = " · ".join(nombres[:4]) + (f" y {n - 4} más" if n > 4 else "")
    return dict(tipo="timbre", titulo=titulo, cuerpo=cuerpo, url=URL_APP, imagen="", tag="timbre")


def enviar_con_timbre(db, avisos: list):
    """Si algo requiere atención: alertas por cámara en silencio + resumen que suena TIMBRES veces."""
    if not any(a["tipo"] in TIPOS_QUE_SUENAN for a in avisos):
        enviar(db, avisos)                      # p. ej. solo "volvió a la normalidad": suena una vez
        return
    enviar(db, [{**a, "silencioso": "1"} for a in avisos])
    timbre = resumen_timbre(avisos)
    for i in range(TIMBRES):
        time.sleep(SEGUNDOS_ENTRE_TIMBRES if i else 2)
        enviar(db, [{**timbre, "repeticion": str(i + 1)}])


# ---------------------------------------------------------------------------
# Modos de ejecución
# ---------------------------------------------------------------------------
def procesar_estado(db):
    estado = json.loads(ARCHIVO_ESTADO.read_text(encoding="utf-8"))
    control = leer_control(db)
    ahora_ms = int(time.time() * 1000)

    avisos, alertas = decidir_avisos(estado, control.get("camaras", {}), ahora_ms)

    # Si el monitor había fallado antes, avisamos que ya se recuperó
    if control.get("error_desde_ms"):
        avisos.insert(0, dict(tipo="normal", titulo="✅ Monitor restablecido",
                              cuerpo=f"Xsense se revisa otra vez con normalidad "
                                     f"(estuvo sin datos {_duracion(ahora_ms - control['error_desde_ms'])}).",
                              url=URL_APP, imagen="", tag="monitor"))

    for a in avisos:
        print(f"-> {a['titulo']}")
    enviar_con_timbre(db, avisos)
    guardar_control(db, {"camaras": alertas, "actualizado_ms": ahora_ms})


def procesar_error(db):
    control = leer_control(db)
    ahora_ms = int(time.time() * 1000)
    desde = control.get("error_desde_ms") or ahora_ms
    ultimo = control.get("error_ultimo_aviso_ms", 0)
    if ahora_ms - ultimo >= AVISO_ERROR_MIN * 60000 - 90000:
        enviar_con_timbre(db, [dict(tipo="error", titulo="⚠️ El monitor no pudo revisar Xsense",
                         cuerpo="Las temperaturas no se están actualizando. Revisa Xsense directamente "
                                "mientras se resuelve.", url=URL_APP, imagen="", tag="monitor")])
        ultimo = ahora_ms
    else:
        print("El monitor sigue fallando; ya se avisó hace menos de una hora.")
    guardar_control(db, {**control, "error_desde_ms": desde, "error_ultimo_aviso_ms": ultimo})


def procesar_prueba(db):
    enviar_con_timbre(db, [dict(tipo="prueba", titulo="🔔 Prueba de Alerta Xsense",
                     cuerpo="Si ves este mensaje, las notificaciones funcionan en este celular.",
                     url=URL_APP, imagen="", tag="prueba")])


def main():
    db = conectar()
    if db is None:
        return
    try:
        if "--error" in sys.argv:
            procesar_error(db)
        elif "--prueba" in sys.argv:
            procesar_prueba(db)
        else:
            procesar_estado(db)
    except Exception as e:
        texto = str(e).splitlines()[0][:300]
        if "insufficient permissions" in texto.lower() or "403" in texto:
            texto += (" — La cuenta de servicio no tiene acceso a Firestore. Revisa que la clave sea del "
                      "proyecto alerta-xsense y que Firestore esté creado; si recién se creó, espera unos minutos.")
        _anotar("error", f"Notificaciones: {texto}")
        raise


if __name__ == "__main__":
    main()
