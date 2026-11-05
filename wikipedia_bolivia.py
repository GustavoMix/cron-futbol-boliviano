#!/usr/bin/env python3
"""
Fuente principal: Wikipedia en español.

Lee el artículo de la temporada (por ejemplo "Primera División de Bolivia 2026")
y extrae la tabla de posiciones, todas las fechas con sus partidos, goleadores,
asistencias y la lista de equipos.

Uso directo para probar:
    py wikipedia_bolivia.py            -> imprime un resumen
    py wikipedia_bolivia.py --json     -> imprime el JSON completo
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag

WIKI_BASE = "https://es.wikipedia.org/wiki/"
# Wikipedia pide un User-Agent que identifique al programa.
USER_AGENT = "FutbolBolivianoApp/2.0 (https://github.com/GustavoMix/cron-futbol-boliviano)"
TITULOS_TEMPORADA = (
    "Primera División de Bolivia {anio}",
    "División Profesional {anio} (Bolivia)",
)

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def limpiar(texto: str) -> str:
    texto = re.sub(r"\[\s*\d+\s*\]", "", texto or "")  # quita notas [1]
    texto = texto.replace("\xa0", " ")
    return re.sub(r"\s+", " ", texto).strip()


def texto_celda(celda: Tag) -> str:
    for sup in celda.find_all("sup"):
        sup.decompose()
    return limpiar(celda.get_text(" ", strip=True))


def a_entero(valor: str) -> int | None:
    valor = (valor or "").replace("−", "-").replace("–", "-").replace("+", "").strip()
    return int(valor) if re.fullmatch(r"-?\d+", valor) else None


def grilla(tabla: Tag) -> list[list[Tag | None]]:
    """Expande rowspan/colspan para que cada fila tenga todas sus celdas."""
    filas: list[list[Tag | None]] = []
    pendientes: dict[int, tuple[Tag, int]] = {}
    for tr in tabla.find_all("tr"):
        if tr.find_parent("table") is not tabla:
            continue
        fila: list[Tag | None] = []
        celdas = list(tr.find_all(["th", "td"], recursive=False))
        col = 0
        while celdas or col in pendientes:
            if col in pendientes:
                celda, restantes = pendientes[col]
                fila.append(celda)
                if restantes <= 1:
                    del pendientes[col]
                else:
                    pendientes[col] = (celda, restantes - 1)
                col += 1
                continue
            celda = celdas.pop(0)
            colspan = int(re.sub(r"\D", "", celda.get("colspan", "1")) or 1)
            rowspan = int(re.sub(r"\D", "", celda.get("rowspan", "1")) or 1)
            for _ in range(colspan):
                fila.append(celda)
                if rowspan > 1:
                    pendientes[col] = (celda, rowspan - 1)
                col += 1
        filas.append(fila)
    return filas


def tabla_despues_de(contenido: Tag, titulo: str) -> Tag | None:
    for encabezado in contenido.find_all(["h2", "h3", "h4"]):
        if limpiar(encabezado.get_text()).lower() == titulo.lower():
            return encabezado.find_next("table")
    return None


def color_fondo(celda: Tag | None) -> str | None:
    if celda is None:
        return None
    estilo = celda.get("style", "")
    m = re.search(r"background(?:-color)?\s*:\s*(#[0-9A-Fa-f]{3,6})", estilo)
    if m:
        return m.group(1).upper()
    return celda.get("bgcolor")


# --------------------------------------------------------------------------- #
# Secciones
# --------------------------------------------------------------------------- #

def parsear_posiciones(contenido: Tag) -> list[dict[str, Any]]:
    tabla = tabla_despues_de(contenido, "Clasificación")
    if tabla is None:
        return []
    filas = grilla(tabla)
    if not filas:
        return []
    encabezados = [texto_celda(c).lower() if c else "" for c in filas[0]]

    def idx(*nombres: str) -> int | None:
        for nombre in nombres:
            for i, e in enumerate(encabezados):
                if e.rstrip(".") == nombre:
                    return i
        return None

    i_pos, i_eq = idx("pos"), idx("equipo")
    i_pts, i_pj, i_g, i_e, i_p = idx("pts"), idx("pj"), idx("g"), idx("e"), idx("p")
    i_gf, i_gc = idx("gf"), idx("gc")
    i_zona = next((i for i, e in enumerate(encabezados) if e.startswith("clasificaci")), None)

    resultado = []
    for fila in filas[1:]:
        def val(i: int | None) -> str:
            return texto_celda(fila[i]) if i is not None and i < len(fila) and fila[i] else ""

        equipo = val(i_eq)
        pos = a_entero(val(i_pos))
        if not equipo or pos is None:
            continue
        resultado.append({
            "pos": pos,
            "equipo": equipo,
            "pts": a_entero(val(i_pts)) or 0,
            "pj": a_entero(val(i_pj)) or 0,
            "g": a_entero(val(i_g)) or 0,
            "e": a_entero(val(i_e)) or 0,
            "p": a_entero(val(i_p)) or 0,
            "gf": a_entero(val(i_gf)) or 0,
            "gc": a_entero(val(i_gc)) or 0,
            "dif": (a_entero(val(i_gf)) or 0) - (a_entero(val(i_gc)) or 0),
            "zona": val(i_zona) or None,
            "color": color_fondo(fila[i_eq] if i_eq is not None else None),
        })
    return resultado


def fecha_iso(texto: str, anio: int) -> str | None:
    m = re.search(r"(\d{1,2})\s+de\s+([a-záéíóú]+)", texto.lower())
    if not m or m.group(2) not in MESES:
        return None
    try:
        return datetime(anio, MESES[m.group(2)], int(m.group(1))).date().isoformat()
    except ValueError:
        return None


def parsear_resultado(texto: str) -> tuple[int | None, int | None]:
    m = re.search(r"(\d+)\s*[–\-:]\s*(\d+)", texto)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def parsear_jornadas(contenido: Tag, anio: int) -> list[dict[str, Any]]:
    jornadas = []
    for tabla in contenido.find_all("table"):
        primera = tabla.find("tr")
        if primera is None:
            continue
        titulo = texto_celda(primera)
        m = re.fullmatch(r"Fecha\s+(\d+)", titulo)
        if not m:
            continue
        filas = grilla(tabla)
        encabezados = [texto_celda(c).lower() if c else "" for c in filas[1]] if len(filas) > 1 else []

        def idx(nombre: str, defecto: int) -> int:
            return encabezados.index(nombre) if nombre in encabezados else defecto

        i_loc, i_res, i_vis = idx("local", 0), idx("resultado", 1), idx("visitante", 2)
        i_est, i_fec, i_hora = idx("estadio", 3), idx("fecha", 4), idx("hora", 5)

        partidos = []
        for fila in filas[2:]:
            def val(i: int) -> str:
                return texto_celda(fila[i]) if i < len(fila) and fila[i] else ""

            local, visitante = val(i_loc), val(i_vis)
            if not local or not visitante or local.lower() == "local":
                continue
            gl, gv = parsear_resultado(val(i_res))
            texto_fecha = val(i_fec)
            partidos.append({
                "local": local,
                "visitante": visitante,
                "goles_local": gl,
                "goles_visitante": gv,
                "jugado": gl is not None,
                "estadio": val(i_est) or None,
                "fecha": fecha_iso(texto_fecha, anio),
                "fecha_texto": texto_fecha or None,
                "hora": val(i_hora) or None,
            })
        if partidos:
            jornadas.append({"numero": int(m.group(1)), "partidos": partidos})
    jornadas.sort(key=lambda j: j["numero"])
    return jornadas


def parsear_ranking(contenido: Tag, titulo: str) -> list[dict[str, Any]]:
    tabla = tabla_despues_de(contenido, titulo)
    if tabla is None:
        return []
    filas = grilla(tabla)
    resultado = []
    for fila in filas[1:]:
        valores = [texto_celda(c) if c else "" for c in fila]
        if len(valores) < 3 or not valores[0]:
            continue
        cantidad = a_entero(valores[2])
        if cantidad is None:
            continue
        resultado.append({
            "jugador": valores[0],
            "equipo": valores[1],
            "cantidad": cantidad,
            "partidos": a_entero(valores[3]) if len(valores) > 3 else None,
        })
    return resultado[:20]


def parsear_equipos(contenido: Tag) -> list[dict[str, Any]]:
    tabla = tabla_despues_de(contenido, "Información")
    if tabla is None:
        return []
    filas = grilla(tabla)
    if not filas:
        return []
    encabezados = [texto_celda(c).lower() if c else "" for c in filas[0]]
    equipos = []
    for fila in filas[1:]:
        datos = {
            encabezados[i]: texto_celda(c)
            for i, c in enumerate(fila)
            if c is not None and i < len(encabezados)
        }
        if datos.get("equipo"):
            equipos.append({
                "equipo": datos.get("equipo"),
                "ciudad": datos.get("ciudad") or None,
                "entrenador": datos.get("entrenador") or None,
                "estadio": datos.get("estadio") or None,
            })
    return equipos


def parsear_resumen(soup: BeautifulSoup) -> dict[str, Any]:
    info = soup.select_one("table.infobox")
    if info is None:
        return {}
    datos: dict[str, str] = {}
    for tr in info.find_all("tr"):
        th, td = tr.find("th"), tr.find("td")
        if th and td:
            datos[limpiar(th.get_text(" ")).lower()] = texto_celda(td)
    return {
        "nombre": texto_celda(info.find("tr")) if info.find("tr") else None,
        "campeon_anterior": datos.get("def. título"),
        "participantes": datos.get("participantes"),
        "partidos": datos.get("partidos"),
        "goles": datos.get("goles"),
        "tv": datos.get("tv oficial"),
    }


# --------------------------------------------------------------------------- #
# Descarga
# --------------------------------------------------------------------------- #

def descargar_temporada(
    sesion: requests.Session | None = None,
    anio: int | None = None,
    timeout: int = 40,
) -> dict[str, Any]:
    sesion = sesion or requests.Session()
    anio = anio or datetime.now(ZoneInfo("America/La_Paz")).year
    errores = []
    # Prueba el año actual y, si aún no existe el artículo, el anterior.
    for anio_prueba in (anio, anio - 1):
        for plantilla in TITULOS_TEMPORADA:
            titulo = plantilla.format(anio=anio_prueba)
            url = WIKI_BASE + quote(titulo.replace(" ", "_"))
            try:
                respuesta = sesion.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
            except requests.RequestException as error:
                errores.append(f"{titulo}: {error}")
                continue
            if respuesta.status_code != 200:
                errores.append(f"{titulo}: HTTP {respuesta.status_code}")
                continue
            datos = parsear_html(respuesta.text, anio_prueba)
            if datos["posiciones"] or datos["jornadas"]:
                datos["titulo"] = titulo
                datos["url"] = url
                datos["anio"] = anio_prueba
                return datos
            errores.append(f"{titulo}: sin tablas reconocibles")
    raise RuntimeError("Wikipedia no disponible: " + " | ".join(errores))


def parsear_html(html: str, anio: int) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    contenido = soup.select_one("#mw-content-text") or soup
    return {
        "resumen": parsear_resumen(soup),
        "posiciones": parsear_posiciones(contenido),
        "jornadas": parsear_jornadas(contenido, anio),
        "goleadores": parsear_ranking(contenido, "Goleadores"),
        "asistencias": parsear_ranking(contenido, "Asistencias"),
        "equipos": parsear_equipos(contenido),
    }


def main() -> int:
    datos = descargar_temporada()
    if "--json" in sys.argv:
        print(json.dumps(datos, ensure_ascii=False, indent=2))
        return 0
    print(datos["titulo"], datos["url"])
    print("Posiciones:", len(datos["posiciones"]))
    for fila in datos["posiciones"][:5]:
        print("  ", fila["pos"], fila["equipo"], fila["pts"], "pts")
    print("Jornadas:", len(datos["jornadas"]),
          "| jugados:", sum(p["jugado"] for j in datos["jornadas"] for p in j["partidos"]))
    print("Goleadores:", [(g["jugador"], g["cantidad"]) for g in datos["goleadores"][:3]])
    print("Equipos:", len(datos["equipos"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
