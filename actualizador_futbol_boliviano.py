#!/usr/bin/env python3
"""
ACTUALIZADOR DE FÚTBOL BOLIVIANO - V3 (SIN SOFASCORE)

Fuentes:
- 4 páginas oficiales de torneos de la FBF.
- 16 fuentes adicionales: FBF, clubes, medios bolivianos y JSON de Red Uno.

Ejecutar una vez:
    py actualizador_futbol_boliviano.py

Ejecutar cada 3 horas:
    py actualizador_futbol_boliviano.py --loop --minutes 180

Probar una fuente:
    py actualizador_futbol_boliviano.py --source reduno_estadisticas_equipos

Listar fuentes:
    py actualizador_futbol_boliviano.py --list-sources
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import supabase_sync


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
FUENTES_PATH = BASE_DIR / "fuentes.json"
DATA_PATH = BASE_DIR / "data" / "futbol_boliviano.json"
LOG_PATH = BASE_DIR / "logs" / "actualizador.log"

PALABRAS_SECCION = (
    "tabla", "posiciones", "clasificación", "clasificacion",
    "goleadores", "goleadoras", "resultados", "fixture",
    "partidos", "calendario", "asistencias", "tarjetas",
    "amarillas", "rojas"
)

TITULOS_BLOQUEADOS = {
    "inicio", "home", "contacto", "privacidad", "términos", "terminos",
    "leer más", "leer mas", "ver más", "ver mas", "read more",
    "suscríbete", "suscribete", "publicidad"
}


def configurar_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formato = "%(asctime)s | %(levelname)s | %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=formato,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
        ],
    )


def cargar_json(ruta: Path, valor_default: Any) -> Any:
    if not ruta.exists():
        return valor_default
    try:
        with ruta.open("r", encoding="utf-8") as archivo:
            return json.load(archivo)
    except (OSError, json.JSONDecodeError) as error:
        logging.warning("No se pudo leer %s: %s", ruta.name, error)
        return valor_default


def guardar_json_atomico(ruta: Path, datos: Any) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(ruta.suffix + ".tmp")
    with temporal.open("w", encoding="utf-8") as archivo:
        json.dump(datos, archivo, ensure_ascii=False, indent=2)
    temporal.replace(ruta)


def texto_limpio(valor: Any) -> str:
    if valor is None:
        return ""
    return re.sub(r"\s+", " ", str(valor)).strip()


def ahora_iso(zona_horaria: ZoneInfo) -> str:
    return datetime.now(zona_horaria).isoformat(timespec="seconds")


def crear_sesion(config: dict[str, Any]) -> requests.Session:
    reintentos = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    adaptador = HTTPAdapter(max_retries=reintentos)
    sesion = requests.Session()
    sesion.mount("http://", adaptador)
    sesion.mount("https://", adaptador)
    sesion.headers.update(
        {
            "User-Agent": config["user_agent"],
            "Accept": "application/json,text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-BO,es;q=0.9,en;q=0.5",
            "Cache-Control": "no-cache",
        }
    )
    return sesion


def coincide_futbol(texto: str, config: dict[str, Any]) -> bool:
    valor = texto.lower()
    return any(palabra.lower() in valor for palabra in config["palabras_futbol"])


def url_permitida(url: str, base_url: str) -> bool:
    try:
        absoluta = urljoin(base_url, url)
        parsed = urlparse(absoluta)
        if parsed.scheme not in {"http", "https"}:
            return False

        ruta = parsed.path.lower()
        bloqueadas = (
            "/contact", "/contacto", "/privacy", "/privacidad",
            "/terms", "/terminos", "/login", "/registro",
            "/author/", "/autor/", "/page/", "/tag/",
            "/category/", "/categoria/"
        )
        return not any(x in ruta for x in bloqueadas)
    except Exception:
        return False


def buscar_fecha(contenedor: Tag) -> str | None:
    etiqueta_time = contenedor.find("time")
    if etiqueta_time:
        valor = etiqueta_time.get("datetime") or etiqueta_time.get_text(" ", strip=True)
        return texto_limpio(valor) or None

    for selector in (
        ".date", ".fecha", ".published", ".post-date", ".entry-date",
        "[class*='date']", "[class*='fecha']"
    ):
        elemento = contenedor.select_one(selector)
        if elemento:
            valor = texto_limpio(elemento.get_text(" ", strip=True))
            if valor:
                return valor
    return None


def crear_noticia(
    titulo: str,
    href: str,
    base_url: str,
    fecha: str | None,
    resumen: str | None,
    solo_futbol: bool,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    titulo = texto_limpio(titulo)
    resumen = texto_limpio(resumen)
    absoluta = urljoin(base_url, href)

    if len(titulo) < 15 or len(titulo) > 260:
        return None
    if titulo.lower() in TITULOS_BLOQUEADOS:
        return None
    if not url_permitida(absoluta, base_url):
        return None
    if solo_futbol and not coincide_futbol(f"{titulo} {resumen}", config):
        return None

    return {
        "id": hashlib.sha1(absoluta.encode("utf-8")).hexdigest()[:16],
        "titulo": titulo,
        "url": absoluta,
        "fecha": texto_limpio(fecha) or None,
        "resumen": resumen[:600] or None,
    }


def recorrer_json_ld(objeto: Any) -> list[dict[str, Any]]:
    salida: list[dict[str, Any]] = []

    if isinstance(objeto, list):
        for elemento in objeto:
            salida.extend(recorrer_json_ld(elemento))
        return salida

    if not isinstance(objeto, dict):
        return salida

    tipo = objeto.get("@type")
    tipos = set(tipo if isinstance(tipo, list) else [tipo])

    if tipos.intersection({"NewsArticle", "Article", "BlogPosting"}):
        url = objeto.get("url") or objeto.get("mainEntityOfPage")
        if isinstance(url, dict):
            url = url.get("@id")

        salida.append({
            "titulo": objeto.get("headline") or objeto.get("name"),
            "url": url,
            "fecha": objeto.get("datePublished"),
            "resumen": objeto.get("description"),
        })

    for valor in objeto.values():
        if isinstance(valor, (dict, list)):
            salida.extend(recorrer_json_ld(valor))

    return salida


def extraer_noticias_json_ld(
    html: str,
    base_url: str,
    solo_futbol: bool,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    noticias: list[dict[str, Any]] = []

    for script in soup.select("script[type='application/ld+json']"):
        contenido = script.string or script.get_text()
        if not contenido.strip():
            continue
        try:
            objeto = json.loads(contenido)
        except json.JSONDecodeError:
            continue

        for item in recorrer_json_ld(objeto):
            if not item.get("titulo") or not item.get("url"):
                continue
            noticia = crear_noticia(
                titulo=item["titulo"],
                href=item["url"],
                base_url=base_url,
                fecha=item.get("fecha"),
                resumen=item.get("resumen"),
                solo_futbol=solo_futbol,
                config=config,
            )
            if noticia:
                noticias.append(noticia)

    return noticias


def extraer_noticias_html(
    soup: BeautifulSoup,
    base_url: str,
    solo_futbol: bool,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    noticias: list[dict[str, Any]] = []
    contenedores = list(soup.find_all("article"))

    if len(contenedores) < 3:
        for selector in (
            ".post", ".news-item", ".article", ".nota", ".card", ".entry",
            "[class*='news']", "[class*='noticia']", "[class*='story']"
        ):
            contenedores.extend(soup.select(selector))

    # Respaldo: títulos enlazados.
    contenedores.extend(soup.select("h1, h2, h3, h4"))

    vistos_contenedores: set[int] = set()
    for contenedor in contenedores:
        if id(contenedor) in vistos_contenedores:
            continue
        vistos_contenedores.add(id(contenedor))

        if contenedor.name in {"h1", "h2", "h3", "h4"}:
            enlace = contenedor.find("a", href=True)
            resumen = None
        else:
            enlace = contenedor.select_one("h1 a[href], h2 a[href], h3 a[href], h4 a[href], a[href]")
            p = contenedor.select_one("p")
            resumen = p.get_text(" ", strip=True) if p else None

        if not enlace:
            continue

        titulo = texto_limpio(enlace.get_text(" ", strip=True))
        if len(titulo) < 15 and contenedor.name not in {"h1", "h2", "h3", "h4"}:
            encabezado = contenedor.select_one("h1, h2, h3, h4")
            if encabezado:
                titulo = texto_limpio(encabezado.get_text(" ", strip=True))

        noticia = crear_noticia(
            titulo=titulo,
            href=enlace.get("href", ""),
            base_url=base_url,
            fecha=buscar_fecha(contenedor),
            resumen=resumen,
            solo_futbol=solo_futbol,
            config=config,
        )
        if noticia:
            noticias.append(noticia)

    return noticias


def deduplicar_noticias(
    noticias: list[dict[str, Any]],
    limite: int,
) -> list[dict[str, Any]]:
    salida: list[dict[str, Any]] = []
    vistos: set[str] = set()

    for noticia in noticias:
        clave = noticia["url"].rstrip("/").lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        salida.append(noticia)
        if len(salida) >= limite:
            break

    return salida


def extraer_tablas(
    soup: BeautifulSoup,
    max_tablas: int,
    max_filas: int,
) -> list[dict[str, Any]]:
    tablas: list[dict[str, Any]] = []

    for indice, tabla in enumerate(soup.find_all("table")[:max_tablas], start=1):
        caption = tabla.find("caption")
        nombre = texto_limpio(caption.get_text(" ", strip=True)) if caption else ""

        if not nombre:
            encabezado = tabla.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
            if encabezado:
                nombre = texto_limpio(encabezado.get_text(" ", strip=True))

        filas_html = tabla.find_all("tr")
        if not filas_html:
            continue

        encabezados: list[str] = []
        primera = filas_html[0]
        if primera.find_all("th"):
            encabezados = [
                texto_limpio(celda.get_text(" ", strip=True)) or f"columna_{i+1}"
                for i, celda in enumerate(primera.find_all(["th", "td"]))
            ]
            filas_html = filas_html[1:]

        filas: list[Any] = []
        for fila_html in filas_html[:max_filas]:
            valores = [
                texto_limpio(celda.get_text(" ", strip=True))
                for celda in fila_html.find_all(["th", "td"])
            ]
            if not valores or not any(valores):
                continue

            if encabezados:
                if len(valores) < len(encabezados):
                    valores.extend([""] * (len(encabezados) - len(valores)))
                filas.append({
                    encabezados[i]: valores[i]
                    for i in range(min(len(encabezados), len(valores)))
                })
            else:
                filas.append(valores)

        if filas:
            tablas.append({
                "indice": indice,
                "nombre": nombre or f"Tabla {indice}",
                "columnas": encabezados,
                "filas": filas,
            })

    return tablas


def extraer_secciones(soup: BeautifulSoup) -> list[dict[str, str]]:
    secciones: list[dict[str, str]] = []

    for encabezado in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        titulo = texto_limpio(encabezado.get_text(" ", strip=True))
        if not any(p in titulo.lower() for p in PALABRAS_SECCION):
            continue

        fragmentos: list[str] = []
        actual = encabezado.find_next_sibling()

        while actual is not None and len(" ".join(fragmentos)) < 4000:
            if isinstance(actual, Tag) and actual.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                break
            if isinstance(actual, Tag):
                valor = texto_limpio(actual.get_text(" ", strip=True))
                if valor:
                    fragmentos.append(valor)
            actual = actual.find_next_sibling()

        contenido = texto_limpio(" ".join(fragmentos))
        if contenido:
            secciones.append({"titulo": titulo, "contenido": contenido[:4000]})

        if len(secciones) >= 15:
            break

    return secciones


def scrapear_html(
    sesion: requests.Session,
    fuente: dict[str, Any],
    config: dict[str, Any],
    zona_horaria: ZoneInfo,
) -> dict[str, Any]:
    inicio = time.monotonic()
    respuesta = sesion.get(
        fuente["url"],
        timeout=int(config["timeout_segundos"]),
        allow_redirects=True,
    )
    respuesta.raise_for_status()

    content_type = respuesta.headers.get("content-type", "")
    if "html" not in content_type.lower():
        raise ValueError(f"Se esperaba HTML y llegó: {content_type}")

    soup = BeautifulSoup(respuesta.text, "html.parser")
    noticias = deduplicar_noticias(
        extraer_noticias_json_ld(
            respuesta.text,
            respuesta.url,
            fuente.get("solo_futbol", False),
            config,
        )
        + extraer_noticias_html(
            soup,
            respuesta.url,
            fuente.get("solo_futbol", False),
            config,
        ),
        int(config["max_noticias_por_fuente"]),
    )
    tablas = extraer_tablas(
        soup,
        int(config["max_tablas_por_fuente"]),
        int(config["max_filas_por_tabla"]),
    )
    secciones = extraer_secciones(soup)
    titulo_pagina = texto_limpio(soup.title.get_text(" ", strip=True)) if soup.title else None

    return {
        "id": fuente["id"],
        "nombre": fuente["nombre"],
        "modo": fuente["modo"],
        "categoria": fuente.get("categoria"),
        "url": fuente["url"],
        "url_final": respuesta.url,
        "estado": "ok",
        "codigo_http": respuesta.status_code,
        "actualizado_en": ahora_iso(zona_horaria),
        "duracion_ms": round((time.monotonic() - inicio) * 1000),
        "titulo_pagina": titulo_pagina,
        "cantidad_tablas": len(tablas),
        "cantidad_noticias": len(noticias),
        "cantidad_equipos": 0,
        "tablas": tablas,
        "secciones": secciones,
        "noticias": noticias,
        "datos_estructurados": None,
        "error": None,
        "datos_desde_cache": False,
    }


def solicitar_json(
    sesion: requests.Session,
    url: str,
    timeout: int,
) -> dict[str, Any]:
    respuesta = sesion.get(url, timeout=timeout, allow_redirects=True)
    respuesta.raise_for_status()
    data = respuesta.json()
    if not isinstance(data, dict):
        raise ValueError("La respuesta JSON no es un objeto.")
    return data


def scrapear_reduno_bundle(
    sesion: requests.Session,
    fuente: dict[str, Any],
    config: dict[str, Any],
    zona_horaria: ZoneInfo,
) -> dict[str, Any]:
    inicio = time.monotonic()
    timeout = int(config["timeout_segundos"])
    lista = solicitar_json(sesion, fuente["url"], timeout)
    equipos_mapa = lista.get("teams") or {}

    if not isinstance(equipos_mapa, dict):
        raise ValueError("teamsList.json no contiene el objeto 'teams'.")

    equipos: list[dict[str, Any]] = []
    errores_detalle: list[dict[str, str]] = []
    max_equipos = int(config.get("max_equipos_reduno", 30))

    for indice, (team_id, metadatos) in enumerate(equipos_mapa.items()):
        if indice >= max_equipos:
            break

        detalle_url = fuente["url_equipos"].format(team_id=team_id)

        try:
            detalle = solicitar_json(sesion, detalle_url, timeout)
            equipos.append({
                "id": team_id,
                "nombre": (metadatos or {}).get("name"),
                "metadatos": metadatos,
                "detalle": detalle,
            })
        except Exception as error:
            errores_detalle.append({
                "id": team_id,
                "nombre": texto_limpio((metadatos or {}).get("name")),
                "error": f"{type(error).__name__}: {error}",
            })

        time.sleep(0.15)

    return {
        "id": fuente["id"],
        "nombre": fuente["nombre"],
        "modo": fuente["modo"],
        "categoria": fuente.get("categoria"),
        "url": fuente["url"],
        "url_final": fuente["url"],
        "estado": "parcial" if errores_detalle else "ok",
        "codigo_http": 200,
        "actualizado_en": ahora_iso(zona_horaria),
        "duracion_ms": round((time.monotonic() - inicio) * 1000),
        "titulo_pagina": "Datos estructurados del fútbol boliviano",
        "cantidad_tablas": 0,
        "cantidad_noticias": 0,
        "cantidad_equipos": len(equipos),
        "tablas": [],
        "secciones": [],
        "noticias": [],
        "datos_estructurados": {
            "coverage_level": lista.get("coverageLevel"),
            "competicion": lista.get("competition"),
            "equipos": equipos,
            "errores_detalle": errores_detalle,
        },
        "error": None,
        "datos_desde_cache": False,
    }


def scrapear_fuente(
    sesion: requests.Session,
    fuente: dict[str, Any],
    config: dict[str, Any],
    zona_horaria: ZoneInfo,
) -> dict[str, Any]:
    modo = fuente.get("modo", "html")
    if modo == "reduno_bundle":
        return scrapear_reduno_bundle(sesion, fuente, config, zona_horaria)
    return scrapear_html(sesion, fuente, config, zona_horaria)


def resultado_error(
    fuente: dict[str, Any],
    error: Exception,
    anterior: dict[str, Any] | None,
    zona_horaria: ZoneInfo,
) -> dict[str, Any]:
    mensaje = f"{type(error).__name__}: {error}"

    if anterior:
        salida = dict(anterior)
        salida.update({
            "estado": "error",
            "ultimo_intento_en": ahora_iso(zona_horaria),
            "error": mensaje,
            "datos_desde_cache": True,
        })
        return salida

    return {
        "id": fuente["id"],
        "nombre": fuente["nombre"],
        "modo": fuente.get("modo", "html"),
        "categoria": fuente.get("categoria"),
        "url": fuente["url"],
        "url_final": None,
        "estado": "error",
        "codigo_http": None,
        "actualizado_en": None,
        "ultimo_intento_en": ahora_iso(zona_horaria),
        "duracion_ms": None,
        "titulo_pagina": None,
        "cantidad_tablas": 0,
        "cantidad_noticias": 0,
        "cantidad_equipos": 0,
        "tablas": [],
        "secciones": [],
        "noticias": [],
        "datos_estructurados": None,
        "error": mensaje,
        "datos_desde_cache": False,
    }


def sincronizar_supabase(
    documento: dict[str, Any],
    fuentes: list[dict[str, Any]],
    config: dict[str, Any],
    dry_run: bool = False,
) -> None:
    """
    Sube el documento a Supabase si está configurado.

    Se llama después de guardar el JSON en disco, así que un problema con
    Supabase nunca hace perder los datos scrapeados: el archivo ya está escrito
    y el commit del workflow igual se ejecuta.
    """
    opciones = config.get("supabase") or {}

    if not opciones.get("habilitado", True):
        logging.info("Supabase deshabilitado en config.json. Se omite la subida.")
        return

    if not dry_run and not supabase_sync.hay_credenciales():
        logging.info(
            "Sin SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY en el entorno. "
            "Se omite la subida a Supabase."
        )
        return

    supabase_sync.sincronizar(
        documento,
        fuentes_config=fuentes,
        retencion_dias=int(opciones.get("retencion_dias", 0)),
        dry_run=dry_run,
        timeout=int(opciones.get("timeout_segundos", 60)),
    )


def actualizar(
    source_id: str | None = None,
    subir_supabase: bool = True,
    supabase_dry_run: bool = False,
) -> dict[str, Any]:
    config = cargar_json(CONFIG_PATH, {})
    fuentes = cargar_json(FUENTES_PATH, [])

    if not config:
        raise RuntimeError("No se encontró config.json o está vacío.")
    if not fuentes:
        raise RuntimeError("No se encontró fuentes.json o está vacío.")

    zona_horaria = ZoneInfo(config.get("zona_horaria", "America/La_Paz"))
    activas = [
        fuente for fuente in fuentes
        if fuente.get("habilitada", True)
        and (source_id is None or fuente["id"] == source_id)
    ]

    if source_id and not activas:
        disponibles = ", ".join(f["id"] for f in fuentes)
        raise ValueError(
            f"No existe o está deshabilitada la fuente '{source_id}'. "
            f"Disponibles: {disponibles}"
        )

    anterior = cargar_json(DATA_PATH, {})
    anteriores_por_id = {
        item["id"]: item
        for item in anterior.get("fuentes", [])
        if isinstance(item, dict) and item.get("id")
    }
    resultados_por_id = dict(anteriores_por_id)
    sesion = crear_sesion(config)

    logging.info("Iniciando actualización de %d fuente(s).", len(activas))

    for indice, fuente in enumerate(activas, start=1):
        logging.info(
            "[%d/%d] %s | modo=%s",
            indice, len(activas), fuente["nombre"], fuente.get("modo", "html")
        )

        try:
            resultado = scrapear_fuente(sesion, fuente, config, zona_horaria)
            logging.info(
                "%s: %s | tablas=%d | noticias=%d | equipos=%d",
                resultado["estado"].upper(),
                fuente["nombre"],
                resultado.get("cantidad_tablas", 0),
                resultado.get("cantidad_noticias", 0),
                resultado.get("cantidad_equipos", 0),
            )
        except Exception as error:
            logging.error("ERROR en %s: %s", fuente["nombre"], error)
            resultado = resultado_error(
                fuente,
                error,
                anteriores_por_id.get(fuente["id"]),
                zona_horaria,
            )

        resultados_por_id[fuente["id"]] = resultado

        if indice < len(activas):
            time.sleep(float(config.get("espera_entre_fuentes_segundos", 1.2)))

    resultados = [
        resultados_por_id[fuente["id"]]
        for fuente in fuentes
        if fuente.get("habilitada", True) and fuente["id"] in resultados_por_id
    ]

    documento = {
        "schema_version": 3,
        "generado_en": ahora_iso(zona_horaria),
        "zona_horaria": str(zona_horaria),
        "resumen": {
            "fuentes_configuradas": len([f for f in fuentes if f.get("habilitada", True)]),
            "fuentes_correctas": sum(1 for x in resultados if x.get("estado") == "ok"),
            "fuentes_parciales": sum(1 for x in resultados if x.get("estado") == "parcial"),
            "fuentes_con_error": sum(1 for x in resultados if x.get("estado") == "error"),
            "tablas_extraidas": sum(int(x.get("cantidad_tablas", 0)) for x in resultados),
            "noticias_extraidas": sum(int(x.get("cantidad_noticias", 0)) for x in resultados),
            "equipos_estructurados": sum(int(x.get("cantidad_equipos", 0)) for x in resultados),
        },
        "fuentes": resultados,
    }

    guardar_json_atomico(DATA_PATH, documento)
    logging.info("JSON guardado en: %s", DATA_PATH)

    if subir_supabase or supabase_dry_run:
        sincronizar_supabase(documento, fuentes, config, dry_run=supabase_dry_run)

    return documento


def bucle(
    intervalo_minutos: int,
    source_id: str | None = None,
    subir_supabase: bool = True,
) -> None:
    if intervalo_minutos < 15:
        raise ValueError("Para 20 fuentes usa un intervalo mínimo de 15 minutos.")

    logging.info(
        "Modo continuo activo. Intervalo: %d minutos. Ctrl+C para detener.",
        intervalo_minutos,
    )

    while True:
        inicio = time.monotonic()

        try:
            actualizar(source_id=source_id, subir_supabase=subir_supabase)
        except Exception:
            logging.exception("La actualización general terminó con error.")

        duracion = time.monotonic() - inicio
        espera = max(1, intervalo_minutos * 60 - duracion)
        proxima = datetime.fromtimestamp(datetime.now().timestamp() + espera)

        logging.info(
            "Próxima ejecución aproximada: %s",
            proxima.strftime("%Y-%m-%d %H:%M:%S"),
        )
        time.sleep(espera)


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Actualizador de 20 fuentes del fútbol boliviano sin SofaScore."
    )
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--minutes", type=int, default=None)
    parser.add_argument("--source", type=str, default=None)
    parser.add_argument("--list-sources", action="store_true")
    parser.add_argument(
        "--sin-supabase",
        action="store_true",
        help="Genera el JSON pero no lo sube a Supabase.",
    )
    parser.add_argument(
        "--supabase-dry-run",
        action="store_true",
        help="Muestra qué se subiría a Supabase, sin escribir nada.",
    )
    return parser


def main() -> int:
    configurar_logging()
    args = construir_parser().parse_args()

    try:
        config = cargar_json(CONFIG_PATH, {})
        fuentes = cargar_json(FUENTES_PATH, [])

        if args.list_sources:
            for fuente in fuentes:
                estado = "ON" if fuente.get("habilitada", True) else "OFF"
                print(f"[{estado}] {fuente['id']} -> {fuente['nombre']}")
            return 0

        intervalo = args.minutes or int(config.get("intervalo_minutos", 180))
        subir_supabase = not args.sin_supabase

        if args.loop:
            bucle(intervalo, source_id=args.source, subir_supabase=subir_supabase)
        else:
            actualizar(
                source_id=args.source,
                subir_supabase=subir_supabase,
                supabase_dry_run=args.supabase_dry_run,
            )

        return 0

    except KeyboardInterrupt:
        logging.info("Proceso detenido por el usuario.")
        return 0
    except Exception:
        logging.exception("No se pudo completar el proceso.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
