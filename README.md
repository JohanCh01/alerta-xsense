# Alerta Xsense — Temperaturas de cámaras

Monitoreo automático de las temperaturas de las cámaras de **Packing Espárrago** (Danper)
registradas en [Xsense](https://www.xsenseccm.com/). Revisa cada 12 minutos, detecta las
cámaras fuera de su rango y las muestra en una app web instalable en el celular.

## Cómo funciona

```
GitHub Actions (cada 12 min)
   └─ monitor/xsense_monitor.py
        1. inicia sesión en Xsense (navegador oculto)
        2. lee por la API las 12 cámaras: rango, promedio, sensores y 24 h de historial
        3. guarda docs/data/estado.json  (+ gráficos PNG de las cámaras desviadas)
   └─ publica la carpeta docs/ en GitHub Pages  →  la app
```

| Carpeta / archivo | Qué es |
|---|---|
| `monitor/xsense_monitor.py` | Script que lee Xsense y genera los datos |
| `docs/index.html` | La app (tarjetas de las cámaras + detalle con gráfico) |
| `docs/manifest.webmanifest`, `docs/sw.js`, `docs/icons/` | Lo que permite instalarla en el celular |
| `monitor/notificar.py` | Envía las alertas al celular |
| `.github/workflows/monitor.yml` | La automatización que corre cada 12 minutos |

Los datos (`estado.json`, gráficos) **no se guardan en el historial del repositorio**: se
generan en cada revisión y se publican directamente en la app.

## Configuración (una sola vez)

1. **Credenciales de Xsense** → *Settings → Secrets and variables → Actions → New repository secret*:
   - `XSENSE_EMAIL`
   - `XSENSE_PASSWORD`
2. **Publicación de la app** → *Settings → Pages → Build and deployment → Source:* **GitHub Actions**.
3. **Primera revisión** → pestaña *Actions → Monitor Xsense → Run workflow*.

La app queda en: **https://johanch01.github.io/alerta-xsense/**

## Probar en la laptop

```bash
pip install -r monitor/requirements.txt
python monitor/xsense_monitor.py          # una revisión
python monitor/xsense_monitor.py --bucle  # cada 12 min (Ctrl+C para detener)
python monitor/xsense_monitor.py --ver    # muestra el navegador (para revisar el login)
```

Las credenciales van en un archivo `.env` en la raíz (está en `.gitignore`, nunca se sube):

```
XSENSE_EMAIL=correo@danper.com
XSENSE_PASSWORD=la_contraseña
```

Para ver la app con los datos locales: `cd docs && python -m http.server 8000` y abrir
`http://localhost:8000`.

## Estados de una cámara

| Estado | Significa |
|---|---|
| Alto / Bajo | El promedio está por encima del máximo o por debajo del mínimo |
| En rango | El promedio está dentro del rango configurado en Xsense |
| Sin lecturas | Ningún sensor reportó en los últimos 30 min |
| Sin rango | La cámara no tiene mínimo/máximo configurado en Xsense |

En cámaras con varios sensores, la tarjeta avisa además cuántos sensores están fuera de rango
aunque el promedio esté bien.

## Notificaciones al celular

Después de publicar la app, `monitor/notificar.py` envía notificaciones push (Firebase Cloud Messaging)
a los celulares que tocaron **Activar alertas** en la app:

| Cuándo | Aviso |
|---|---|
| Una cámara sale de su rango o deja de reportar | ⚠️ Desviación (con el gráfico) |
| Sigue fuera de rango | ⏱ Recordatorio cada 30 min |
| Vuelve a su rango | ✅ Volvió a la normalidad |
| El monitor no pudo revisar Xsense | ⚠️ Aviso (máximo 1 por hora) |

Al tocar la notificación se abre la app directo en la cámara.

Requiere el secreto `FIREBASE_SERVICE_ACCOUNT` (contenido del `.json` de la cuenta de servicio de
Firebase) y Firestore creado. Para probar: *Actions → Monitor Xsense → Run workflow* marcando
**"Enviar además una notificación de prueba"**.
