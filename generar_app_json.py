#!/usr/bin/env python3
"""
Genera data/app.json: un archivo pequeño y simple para la app Android.

- Wikipedia es la fuente principal (tabla, fechas, resultados, goleadores).
- Las noticias se juntan de los sitios web que sí responden (FBF, Unitel, DIEZ,
  Red Uno, Opinión, etc.), sin duplicados y ordenadas de la más nueva a la más vieja.
- Si Wikipedia o un sitio falla, se conservan los últimos datos buenos.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
APP_PATH = BASE_DIR / "data" / "app.json"
MAX_NOTICIAS = 80
DIAS_NOTICIA = 21

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# Títulos de menú o de secciones que no son noticias.
TITULOS_RUIDO = (
    "liga nacional sub", "selección absoluta", "seleccion absoluta",
    "selección femenina", "seleccion femenina", "prepárate para el juego",
)

# Para medios generales: además de la palabra "fútbol", debe sonar a fútbol boliviano.
PALABRAS_BOLIVIA = (
    "bolivia", "boliviano", "la verde", "fbf", "bolívar", "bolivar", "strongest",
    "always ready", "blooming", "oriente", "wilstermann", "aurora", "guabirá",
    "guabira", "nacional potosí", "nacional potosi", "real potosí", "real potosi",
    "tomayapo", "san josé", "san jose", "real oruro", "vinto", "independiente",
    "abb", "bulo bulo", "división profesional", "division profesional",
    "copa simón bolívar", "copa simon bolivar", "copa paceña", "copa pacena",
    "copa bolivia", "tigre", "academia", "celeste", "atigrado", "refinero",
)


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


def reparar_mojibake(texto: str | None) -> str | None:
    """Corrige textos tipo 'BolÃ­var' que llegan con la codificación equivocada."""
    if not texto or not re.search(r"Ã.|Â.|â€", texto):
        return texto
    try:
        return texto.encode("latin-1", errors="strict").decode("utf-8")
    except UnicodeError:
        try:
            return texto.encode("cp1252", errors="strict").decode("utf-8")
        except UnicodeError:
            return texto


def fecha_noticia(texto: str | None, ahora: datetime) -> datetime | None:
    """Convierte las fechas que publican los medios a datetime."""
    if not texto:
        return None
    t = texto.lower().strip()

    m = re.search(r"hace\s+(\d+)\s+(minuto|hora|día|dia)", t)
    if m:
        n = int(m.group(1))
        unidad = m.group(2)
        delta = timedelta(minutes=n) if unidad == "minuto" else (
            timedelta(hours=n) if unidad == "hora" else timedelta(days=n))
        return ahora - delta

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:[t ](\d{2}):(\d{2}))?", t)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4) or 12), int(m.group(5) or 0), tzinfo=ahora.tzinfo)
        except ValueError:
            return None

    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", t)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                            int(m.group(4) or 12), int(m.group(5) or 0), tzinfo=ahora.tzinfo)
        except ValueError:
            return None

    # "noviembre 3, 2026 16:42"
    m = re.search(r"([a-záéíóú]+)\s+(\d{1,2}),?\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", t)
    if m and m.group(1) in MESES:
        try:
            return datetime(int(m.group(3)), MESES[m.group(1)], int(m.group(2)),
                            int(m.group(4) or 12), int(m.group(5) or 0), tzinfo=ahora.tzinfo)
        except ValueError:
            return None

    # "3 de noviembre de 2026"
    m = re.search(r"(\d{1,2})\s+de\s+([a-záéíóú]+)(?:\s+de)?\s+(\d{4})", t)
    if m and m.group(2) in MESES:
        try:
            return datetime(int(m.group(3)), MESES[m.group(2)], int(m.group(1)), 12, 0,
                            tzinfo=ahora.tzinfo)
        except ValueError:
            return None
    return None


def es_noticia_util(titulo: str, fuente: dict[str, Any]) -> bool:
    t = titulo.lower()
    if any(ruido in t for ruido in TITULOS_RUIDO):
        return False
    if len(titulo) < 25:
        return False
    # Los medios generales publican mucho fútbol internacional: pedimos algo boliviano.
    if fuente.get("categoria") in {"medio", "medio_regional"} and fuente.get("id") not in {
        "unitel_futbol", "diez_futbol", "reduno_futbol_boliviano", "opinion_futbol_boliviano",
    }:
        return any(p in t for p in PALABRAS_BOLIVIA)
    return True


def nombre_corto_fuente(nombre: str) -> str:
    return nombre.split(" - ")[0].strip()


def juntar_noticias(
    documento: dict[str, Any],
    anteriores: list[dict[str, Any]],
    ahora: datetime,
) -> list[dict[str, Any]]:
    vistas_antes = {n["url"]: n for n in anteriores if n.get("url")}
    por_titulo: dict[str, dict[str, Any]] = {}
    limite = ahora - timedelta(days=DIAS_NOTICIA)

    for fuente in documento.get("fuentes", []):
        for orden, noticia in enumerate(fuente.get("noticias", [])):
            titulo = reparar_mojibake(noticia.get("titulo") or "") or ""
            url = noticia.get("url")
            if not titulo or not url or not es_noticia_util(titulo, fuente):
                continue
            clave = normalizar(titulo)
            if clave in por_titulo:
                continue

            anterior = vistas_antes.get(url)
            primera_vez = (anterior or {}).get("visto_en") or (
                ahora - timedelta(seconds=orden)).isoformat(timespec="seconds")
            fecha = fecha_noticia(noticia.get("fecha"), ahora)
            # "Hace 1 hora" cambia en cada corrida: usamos la primera fecha calculada.
            if anterior and anterior.get("fecha") and re.search(
                    r"hace", (noticia.get("fecha") or "").lower()):
                fecha = datetime.fromisoformat(anterior["fecha"])
            orden_fecha = fecha or datetime.fromisoformat(primera_vez)
            if orden_fecha < limite:
                continue

            resumen = reparar_mojibake(noticia.get("resumen"))
            if resumen and (resumen.lower().startswith("read more") or "redacción" in resumen.lower()):
                resumen = None

            por_titulo[clave] = {
                "id": hashlib.sha1(url.encode("utf-8")).hexdigest()[:12],
                "titulo": titulo,
                "url": url,
                "fuente": nombre_corto_fuente(fuente.get("nombre", "")),
                "fecha": fecha.isoformat(timespec="seconds") if fecha else None,
                "visto_en": primera_vez,
                "resumen": resumen,
                "_orden": orden_fecha,
            }

    noticias = sorted(por_titulo.values(), key=lambda n: n["_orden"], reverse=True)
    for n in noticias:
        n.pop("_orden", None)
    return noticias[:MAX_NOTICIAS]


def estado_fuentes(documento: dict[str, Any], wiki_ok: bool, wiki_error: str | None) -> list[dict[str, Any]]:
    salida = [{
        "nombre": "Wikipedia",
        "ok": wiki_ok,
        "detalle": None if wiki_ok else wiki_error,
    }]
    for fuente in documento.get("fuentes", []):
        salida.append({
            "nombre": fuente.get("nombre"),
            "ok": fuente.get("estado") == "ok",
            "detalle": None if fuente.get("estado") == "ok" else (fuente.get("error") or "")[:160],
        })
    return salida


def construir(
    documento: dict[str, Any],
    wiki: dict[str, Any] | None,
    wiki_error: str | None,
    zona_horaria: ZoneInfo,
) -> dict[str, Any]:
    anterior: dict[str, Any] = {}
    if APP_PATH.exists():
        try:
            anterior = json.loads(APP_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            anterior = {}

    ahora = datetime.now(zona_horaria)
    liga = None
    if wiki:
        liga = {
            "nombre": (wiki.get("resumen") or {}).get("nombre") or wiki.get("titulo"),
            "temporada": wiki.get("anio"),
            "url_wikipedia": wiki.get("url"),
            "actualizado_en": ahora.isoformat(timespec="seconds"),
            "info": wiki.get("resumen") or {},
            "posiciones": wiki.get("posiciones", []),
            "jornadas": wiki.get("jornadas", []),
            "goleadores": wiki.get("goleadores", []),
            "asistencias": wiki.get("asistencias", []),
            "equipos": wiki.get("equipos", []),
        }
    else:
        liga = anterior.get("liga")  # último dato bueno

    return {
        "version": 1,
        "generado_en": ahora.isoformat(timespec="seconds"),
        "liga": liga,
        "noticias": juntar_noticias(documento, anterior.get("noticias", []), ahora),
        "fuentes": estado_fuentes(documento, wiki is not None, wiki_error),
    }


def guardar(datos: dict[str, Any]) -> None:
    APP_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporal = APP_PATH.with_suffix(".tmp")
    temporal.write_text(json.dumps(datos, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporal.replace(APP_PATH)
