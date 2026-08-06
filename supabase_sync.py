#!/usr/bin/env python3
"""
SINCRONIZACIÓN CON SUPABASE

Toma el documento que genera actualizador_futbol_boliviano.py y lo escribe en
Supabase usando la API REST (PostgREST). No agrega dependencias: usa requests,
que ya estaba en el proyecto.

El esquema de las tablas está en supabase/schema.sql.

Variables de entorno:
    SUPABASE_URL                 https://xxxxxxxx.supabase.co
    SUPABASE_SERVICE_ROLE_KEY    clave service_role (nunca la anon)

Uso directo, para subir el JSON que ya está en disco:
    py supabase_sync.py
    py supabase_sync.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_DIR = Path(__file__).resolve().parent

# Columnas que resuelven el conflicto en cada upsert. Son las claves únicas del
# esquema y además definen cómo se deduplica cada lote antes de mandarlo.
CLAVES_CONFLICTO = {
    "ejecuciones": "generado_en",
    "fuentes": "id",
    "equipos": "id",
    "jugadores": "id",
    "noticias": "id",
    "ejecuciones_fuentes": "ejecucion_id,fuente_id",
    "tablas": "ejecucion_id,fuente_id,indice",
    "posiciones": "ejecucion_id,fuente_id,tabla_indice,equipo",
    "secciones": "ejecucion_id,fuente_id,orden",
    "equipos_estadisticas": "ejecucion_id,equipo_id",
    "jugadores_estadisticas": "ejecucion_id,jugador_id,equipo_id",
}

# Cuántas filas mandar por request. Las tablas con jsonb grande van de a menos
# para no armar cuerpos de varios megabytes.
LOTE_PREDETERMINADO = 200
LOTES_POR_TABLA = {
    "tablas": 25,
    "equipos": 10,
    "equipos_estadisticas": 50,
    "jugadores_estadisticas": 200,
}

# Red Uno no publica la leyenda de posnId. La proporción de jugadores por valor
# (1 es el grupo más chico, del tamaño de un plantel de arqueros) hace que este
# mapeo sea el razonable. posicion_id se guarda igual por si cambia.
POSICIONES_REDUNO = {
    1: "arquero",
    2: "defensa",
    3: "mediocampista",
    4: "delantero",
}

# Nombre de columna normalizado -> columna de public.posiciones.
COLUMNAS_POSICIONES = {
    "pos": "pos",
    "puesto": "pos",
    "#": "pos",
    "equipo": "equipo",
    "club": "equipo",
    "equipos": "equipo",
    "pj": "pj",
    "j": "pj",
    "jj": "pj",
    "pg": "ganados",
    "g": "ganados",
    "pe": "empatados",
    "e": "empatados",
    "pp": "perdidos",
    "p": "perdidos",
    "gf": "goles_favor",
    "gc": "goles_contra",
    "ge": "goles_contra",
    "dg": "diferencia",
    "df": "diferencia",
    "dif": "diferencia",
    "pts": "puntos",
    "puntos": "puntos",
}

# Clave del resumen de Red Uno -> columna nuestra.
ESTADISTICAS_EQUIPO = {
    "goals": "goles",
    "assists": "asistencias",
    "yellowCards": "tarjetas_amarillas",
    "redCards": "tarjetas_rojas",
    "shots": "tiros",
    "shotsOnTarget": "tiros_al_arco",
    "shotsOffTarget": "tiros_fuera",
    "shotsOnWoodwork": "tiros_al_palo",
    "fouls": "faltas",
    "foulsReceived": "faltas_recibidas",
    "cornerKicks": "corners",
    "offsides": "offsides",
    "saves": "atajadas",
    "stealings": "robos",
    "allPasses": "pases",
    "correctPasses": "pases_correctos",
    "incorrectPasses": "pases_incorrectos",
}

ESTADISTICAS_JUGADOR = {
    "matches": "partidos",
    "minutesPlayed": "minutos",
    **{k: v for k, v in ESTADISTICAS_EQUIPO.items() if k != "saves"},
}

MESES = {
    "enero": 1, "ene": 1,
    "febrero": 2, "feb": 2,
    "marzo": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "mayo": 5, "may": 5,
    "junio": 6, "jun": 6,
    "julio": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "septiembre": 9, "setiembre": 9, "sept": 9, "sep": 9, "set": 9,
    "octubre": 10, "oct": 10,
    "noviembre": 11, "nov": 11,
    "diciembre": 12, "dic": 12,
}

# Para fechas relativas tipo "3 horas ago" o "hace 2 días". Mes y año son
# aproximaciones: a esa distancia el dato exacto ya no importa.
UNIDADES_RELATIVAS = {
    "segundo": 1,
    "minuto": 60,
    "min": 60,
    "hora": 3600,
    "hr": 3600,
    "dia": 86400,
    "semana": 604800,
    "mes": 2592000,
    "ano": 31536000,
}


class SupabaseError(RuntimeError):
    """Falló una llamada a Supabase."""


# ----------------------------------------------------------------------------
# Utilidades de conversión
# ----------------------------------------------------------------------------

def _texto(valor: Any) -> str | None:
    if valor is None:
        return None
    limpio = re.sub(r"\s+", " ", str(valor)).strip()
    return limpio or None


def _entero(valor: Any) -> int | None:
    """Convierte '13', '+8', '-3', '4 (2)' a int. Devuelve None si no se puede."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    if isinstance(valor, float):
        return int(valor)

    coincidencia = re.search(r"-?\d+", str(valor).replace("+", ""))
    if not coincidencia:
        return None
    try:
        return int(coincidencia.group())
    except ValueError:
        return None


def _sin_acentos(valor: str) -> str:
    reemplazos = {"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ü": "u", "ñ": "n"}
    for original, destino in reemplazos.items():
        valor = valor.replace(original, destino)
    return valor


def _con_zona(fecha: datetime, zona_horaria: ZoneInfo | None) -> str:
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=zona_horaria or ZoneInfo("America/La_Paz"))
    return fecha.isoformat()


def _fecha_hora(valor: Any, zona_horaria: ZoneInfo | None = None) -> str | None:
    """
    Devuelve un ISO 8601 con zona a partir de los formatos que usan las fuentes.

    Reconoce ISO 8601, dd/mm/aaaa [hh:mm], "5 de agosto de 2026", "03 Ago 2026",
    "agosto 5, 2026" y relativas tipo "3 horas ago". Si no reconoce nada devuelve
    None: el texto original igual se guarda en la columna *_texto.
    """
    texto = _texto(valor)
    if not texto:
        return None

    # 1. ISO 8601, eventualmente con basura pegada atrás.
    iso = re.match(
        r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?([.,]\d+)?([+-]\d{2}:?\d{2})?)?",
        texto.replace("Z", "+00:00"),
    )
    if iso:
        try:
            return _con_zona(datetime.fromisoformat(iso.group().replace(" ", "T")), zona_horaria)
        except ValueError:
            pass

    plano = _sin_acentos(texto.lower())

    # 2. dd/mm/aaaa u dd-mm-aaaa, con hora opcional. En Bolivia el día va primero.
    numerica = re.search(
        r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b(?:\D{0,3}(\d{1,2}):(\d{2}))?",
        plano,
    )
    if numerica:
        dia, mes, anio, hora, minuto = numerica.groups()
        try:
            return _con_zona(
                datetime(int(anio), int(mes), int(dia), int(hora or 0), int(minuto or 0)),
                zona_horaria,
            )
        except ValueError:
            pass

    nombres_mes = "|".join(sorted(MESES, key=len, reverse=True))

    # 3. "5 de agosto de 2026" y "03 Ago 2026".
    dia_primero = re.search(
        rf"\b(\d{{1,2}})\s*(?:de\s+)?({nombres_mes})\.?\s*(?:de\s+|del\s+)?(\d{{4}})\b",
        plano,
    )
    if dia_primero:
        dia, mes, anio = dia_primero.groups()
        try:
            return _con_zona(datetime(int(anio), MESES[mes], int(dia)), zona_horaria)
        except ValueError:
            pass

    # 4. "agosto 5, 2026".
    mes_primero = re.search(rf"\b({nombres_mes})\.?\s+(\d{{1,2}})\s*,?\s*(\d{{4}})\b", plano)
    if mes_primero:
        mes, dia, anio = mes_primero.groups()
        try:
            return _con_zona(datetime(int(anio), MESES[mes], int(dia)), zona_horaria)
        except ValueError:
            pass

    # 5. "hace 3 horas" / "3 horas ago".
    unidades = "|".join(sorted(UNIDADES_RELATIVAS, key=len, reverse=True))
    relativa = re.search(rf"\b(?:hace\s+)?(\d{{1,3}})\s*({unidades})s?\b", plano)
    if relativa and ("hace" in plano or "ago" in plano):
        cantidad, unidad = relativa.groups()
        referencia = datetime.now(zona_horaria or ZoneInfo("America/La_Paz"))
        return (referencia - timedelta(seconds=int(cantidad) * UNIDADES_RELATIVAS[unidad])).isoformat()

    return None


def _fecha(valor: Any) -> str | None:
    """Devuelve YYYY-MM-DD para columnas date."""
    texto = _texto(valor)
    if not texto:
        return None
    coincidencia = re.match(r"\d{4}-\d{2}-\d{2}", texto)
    return coincidencia.group() if coincidencia else None


def _normalizar_columna(nombre: str) -> str:
    return _sin_acentos((_texto(nombre) or "").lower()).strip(" .:")


def _cantidad(resumen: dict[str, Any], clave: str) -> int | None:
    """Red Uno guarda cada métrica como {"qty": n}."""
    dato = resumen.get(clave)
    if isinstance(dato, dict):
        return _entero(dato.get("qty"))
    return _entero(dato)


def _lotes(filas: list[dict[str, Any]], tamano: int) -> Iterator[list[dict[str, Any]]]:
    for inicio in range(0, len(filas), tamano):
        yield filas[inicio:inicio + tamano]


def _deduplicar(filas: list[dict[str, Any]], claves: list[str]) -> list[dict[str, Any]]:
    """
    Deja una sola fila por combinación de claves, quedándose con la última.

    Postgres rechaza el lote entero ("ON CONFLICT DO UPDATE command cannot
    affect row a second time") si un mismo INSERT trae dos filas con la misma
    clave de conflicto. Pasa de verdad: Red Uno lista a un jugador transferido
    en el plantel de sus dos equipos, y una misma noticia aparece en más de una
    fuente.
    """
    unicas: dict[tuple, dict[str, Any]] = {}
    for fila in filas:
        unicas[tuple(fila.get(clave) for clave in claves)] = fila

    descartadas = len(filas) - len(unicas)
    if descartadas:
        logging.debug("Se unificaron %d fila(s) duplicada(s) por %s.", descartadas, claves)
    return list(unicas.values())


# ----------------------------------------------------------------------------
# Cliente REST
# ----------------------------------------------------------------------------

class ClienteSupabase:
    """Cliente mínimo de PostgREST: upsert por lotes y delete por filtro."""

    def __init__(self, url: str, clave: str, timeout: int = 60) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

        reintentos = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST", "PATCH", "DELETE"}),
            raise_on_status=False,
        )
        self.sesion = requests.Session()
        self.sesion.mount("https://", HTTPAdapter(max_retries=reintentos))
        self.sesion.mount("http://", HTTPAdapter(max_retries=reintentos))
        self.sesion.headers.update({
            "apikey": clave,
            "Authorization": f"Bearer {clave}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _rest(self, tabla: str) -> str:
        return f"{self.url}/rest/v1/{tabla}"

    def _verificar(self, respuesta: requests.Response, descripcion: str) -> None:
        if respuesta.status_code >= 400:
            detalle = respuesta.text[:600]
            raise SupabaseError(
                f"{descripcion} devolvió HTTP {respuesta.status_code}: {detalle}"
            )

    def upsert(
        self,
        tabla: str,
        filas: list[dict[str, Any]],
        on_conflict: str | None = None,
        retornar: bool = False,
        lote: int | None = None,
    ) -> list[dict[str, Any]]:
        """Inserta o actualiza filas. Devuelve las filas escritas si retornar=True."""
        if not filas:
            return []

        if on_conflict:
            filas = _deduplicar(filas, on_conflict.split(","))

        tamano = lote or LOTES_POR_TABLA.get(tabla, LOTE_PREDETERMINADO)
        preferencias = ["resolution=merge-duplicates"]
        preferencias.append("return=representation" if retornar else "return=minimal")

        params = {"on_conflict": on_conflict} if on_conflict else {}
        escritas: list[dict[str, Any]] = []

        for numero, grupo in enumerate(_lotes(filas, tamano), start=1):
            respuesta = self.sesion.post(
                self._rest(tabla),
                params=params,
                data=json.dumps(grupo, ensure_ascii=False).encode("utf-8"),
                headers={"Prefer": ",".join(preferencias)},
                timeout=self.timeout,
            )
            self._verificar(respuesta, f"upsert en {tabla} (lote {numero})")

            if retornar and respuesta.content:
                escritas.extend(respuesta.json())

        logging.info("Supabase: %s -> %d fila(s).", tabla, len(filas))
        return escritas

    def borrar(self, tabla: str, filtros: dict[str, str]) -> None:
        respuesta = self.sesion.delete(
            self._rest(tabla),
            params=filtros,
            headers={"Prefer": "return=minimal"},
            timeout=self.timeout,
        )
        self._verificar(respuesta, f"delete en {tabla}")


# ----------------------------------------------------------------------------
# Transformación del documento a filas
# ----------------------------------------------------------------------------

def _fila_ejecucion(documento: dict[str, Any], zona: ZoneInfo | None) -> dict[str, Any]:
    resumen = documento.get("resumen") or {}
    return {
        "generado_en": _fecha_hora(documento.get("generado_en"), zona),
        "schema_version": _entero(documento.get("schema_version")),
        "zona_horaria": _texto(documento.get("zona_horaria")),
        "fuentes_configuradas": _entero(resumen.get("fuentes_configuradas")) or 0,
        "fuentes_correctas": _entero(resumen.get("fuentes_correctas")) or 0,
        "fuentes_parciales": _entero(resumen.get("fuentes_parciales")) or 0,
        "fuentes_con_error": _entero(resumen.get("fuentes_con_error")) or 0,
        "tablas_extraidas": _entero(resumen.get("tablas_extraidas")) or 0,
        "noticias_extraidas": _entero(resumen.get("noticias_extraidas")) or 0,
        "equipos_estructurados": _entero(resumen.get("equipos_estructurados")) or 0,
    }


def _catalogo_fuentes(
    documento: dict[str, Any],
    fuentes_config: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Une lo que trae el documento con los campos que solo están en fuentes.json."""
    extra = {
        f["id"]: f
        for f in (fuentes_config or [])
        if isinstance(f, dict) and f.get("id")
    }

    catalogo: list[dict[str, Any]] = []
    for fuente in documento.get("fuentes", []):
        if not isinstance(fuente, dict) or not fuente.get("id"):
            continue

        definicion = extra.get(fuente["id"], {})
        catalogo.append({
            "id": fuente["id"],
            "nombre": _texto(fuente.get("nombre")) or fuente["id"],
            "modo": _texto(fuente.get("modo")),
            "categoria": _texto(fuente.get("categoria")),
            "url": _texto(fuente.get("url")),
            "solo_futbol": bool(definicion.get("solo_futbol", False)),
            "habilitada": bool(definicion.get("habilitada", True)),
            "actualizado_en": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
    return catalogo


def _es_tabla_posiciones(columnas: list[str]) -> bool:
    mapeadas = {COLUMNAS_POSICIONES.get(_normalizar_columna(c)) for c in columnas}
    return "equipo" in mapeadas and "puntos" in mapeadas


def _filas_posiciones(
    ejecucion_id: int,
    fuente_id: str,
    tabla: dict[str, Any],
) -> list[dict[str, Any]]:
    columnas = tabla.get("columnas") or []
    if not _es_tabla_posiciones(columnas):
        return []

    mapeo = {c: COLUMNAS_POSICIONES.get(_normalizar_columna(c)) for c in columnas}
    numericas = (
        "pos", "pj", "ganados", "empatados", "perdidos",
        "goles_favor", "goles_contra", "diferencia", "puntos",
    )

    salida: list[dict[str, Any]] = []

    for fila in tabla.get("filas") or []:
        if not isinstance(fila, dict):
            continue

        registro: dict[str, Any] = {
            "ejecucion_id": ejecucion_id,
            "fuente_id": fuente_id,
            "tabla_indice": _entero(tabla.get("indice")) or 0,
            "torneo": _texto(tabla.get("nombre")),
        }
        for columna, destino in mapeo.items():
            if destino:
                registro[destino] = fila.get(columna)

        equipo = _texto(registro.get("equipo"))
        if not equipo:
            continue
        registro["equipo"] = equipo
        for campo in numericas:
            registro[campo] = _entero(registro.get(campo))

        # La FBF a veces publica una diferencia de gol rota (por ejemplo "+111"
        # para un 16-5). Cuando hay goles a favor y en contra, ese resto manda:
        # la diferencia es derivable y el valor scrapeado no es confiable.
        # El texto original siempre queda intacto en public.tablas.filas.
        favor, contra = registro.get("goles_favor"), registro.get("goles_contra")
        if favor is not None and contra is not None:
            registro["diferencia"] = favor - contra

        salida.append(registro)

    return salida


def _filas_por_fuente(
    documento: dict[str, Any],
    ejecucion_id: int,
    zona: ZoneInfo | None,
) -> dict[str, list[dict[str, Any]]]:
    estados: list[dict[str, Any]] = []
    tablas: list[dict[str, Any]] = []
    posiciones: list[dict[str, Any]] = []
    secciones: list[dict[str, Any]] = []
    noticias: list[dict[str, Any]] = []

    for fuente in documento.get("fuentes", []):
        if not isinstance(fuente, dict) or not fuente.get("id"):
            continue
        fuente_id = fuente["id"]

        estados.append({
            "ejecucion_id": ejecucion_id,
            "fuente_id": fuente_id,
            "estado": _texto(fuente.get("estado")) or "desconocido",
            "codigo_http": _entero(fuente.get("codigo_http")),
            "url_final": _texto(fuente.get("url_final")),
            "titulo_pagina": _texto(fuente.get("titulo_pagina")),
            "actualizado_en": _fecha_hora(fuente.get("actualizado_en"), zona),
            "ultimo_intento_en": _fecha_hora(fuente.get("ultimo_intento_en"), zona),
            "duracion_ms": _entero(fuente.get("duracion_ms")),
            "cantidad_tablas": _entero(fuente.get("cantidad_tablas")) or 0,
            "cantidad_noticias": _entero(fuente.get("cantidad_noticias")) or 0,
            "cantidad_equipos": _entero(fuente.get("cantidad_equipos")) or 0,
            "datos_desde_cache": bool(fuente.get("datos_desde_cache", False)),
            "error": _texto(fuente.get("error")),
        })

        indices_vistos: set[int] = set()
        for tabla in fuente.get("tablas") or []:
            if not isinstance(tabla, dict):
                continue
            indice = _entero(tabla.get("indice")) or 0
            if indice in indices_vistos:
                continue
            indices_vistos.add(indice)

            filas_posiciones = _filas_posiciones(ejecucion_id, fuente_id, tabla)
            posiciones.extend(filas_posiciones)
            tablas.append({
                "ejecucion_id": ejecucion_id,
                "fuente_id": fuente_id,
                "indice": indice,
                "nombre": _texto(tabla.get("nombre")),
                "es_posiciones": bool(filas_posiciones),
                "columnas": tabla.get("columnas") or [],
                "filas": tabla.get("filas") or [],
            })

        for orden, seccion in enumerate(fuente.get("secciones") or [], start=1):
            if not isinstance(seccion, dict):
                continue
            secciones.append({
                "ejecucion_id": ejecucion_id,
                "fuente_id": fuente_id,
                "orden": orden,
                "titulo": _texto(seccion.get("titulo")),
                "contenido": _texto(seccion.get("contenido")),
            })

        for noticia in fuente.get("noticias") or []:
            if not isinstance(noticia, dict) or not noticia.get("id"):
                continue

            noticias.append({
                "id": noticia["id"],
                "fuente_id": fuente_id,
                "ejecucion_id": ejecucion_id,
                "titulo": _texto(noticia.get("titulo")) or "(sin título)",
                "url": _texto(noticia.get("url")) or "",
                "fecha": _fecha_hora(noticia.get("fecha"), zona),
                "fecha_texto": _texto(noticia.get("fecha")),
                "resumen": _texto(noticia.get("resumen")),
                "vista_ultima_en": datetime.now().astimezone().isoformat(timespec="seconds"),
            })

    return {
        "ejecuciones_fuentes": estados,
        "tablas": tablas,
        "posiciones": posiciones,
        "secciones": secciones,
        "noticias": noticias,
    }


def _filas_reduno(
    documento: dict[str, Any],
    ejecucion_id: int,
) -> dict[str, list[dict[str, Any]]]:
    equipos: list[dict[str, Any]] = []
    equipos_stats: list[dict[str, Any]] = []
    jugadores: list[dict[str, Any]] = []
    jugadores_stats: list[dict[str, Any]] = []
    ahora = datetime.now().astimezone().isoformat(timespec="seconds")

    for fuente in documento.get("fuentes", []):
        estructurados = (fuente or {}).get("datos_estructurados")
        if not isinstance(estructurados, dict):
            continue
        fuente_id = fuente.get("id")

        for equipo in estructurados.get("equipos") or []:
            if not isinstance(equipo, dict) or not equipo.get("id"):
                continue

            equipo_id = str(equipo["id"])
            metadatos = equipo.get("metadatos") if isinstance(equipo.get("metadatos"), dict) else {}
            detalle = equipo.get("detalle") if isinstance(equipo.get("detalle"), dict) else {}
            info = detalle.get("info") if isinstance(detalle.get("info"), dict) else {}

            equipos.append({
                "id": equipo_id,
                "fuente_id": fuente_id,
                "nombre": _texto(equipo.get("nombre") or metadatos.get("name") or info.get("name")),
                "nombre_corto": _texto(metadatos.get("shortName") or info.get("shortName")),
                "iniciales": _texto(metadatos.get("initials") or info.get("initials")),
                "pais": _texto(metadatos.get("country") or info.get("country")),
                "tipo": _texto(metadatos.get("teamType")),
                "genero": _texto(metadatos.get("gender")),
                "colores": metadatos.get("colors") or info.get("colors"),
                "metadatos": metadatos or None,
                "actualizado_en": ahora,
            })

            resumen_equipo = detalle.get("summary")
            if isinstance(resumen_equipo, dict):
                fila = {
                    "ejecucion_id": ejecucion_id,
                    "equipo_id": equipo_id,
                    "resumen_crudo": resumen_equipo,
                }
                for origen, destino in ESTADISTICAS_EQUIPO.items():
                    fila[destino] = _cantidad(resumen_equipo, origen)
                equipos_stats.append(fila)

            plantel = detalle.get("players")
            if not isinstance(plantel, dict):
                continue

            for jugador_id, jugador in plantel.items():
                if not isinstance(jugador, dict):
                    continue

                info_jugador = jugador.get("info") if isinstance(jugador.get("info"), dict) else {}
                nombre = info_jugador.get("name") if isinstance(info_jugador.get("name"), dict) else {}
                completo = " ".join(
                    parte for parte in (_texto(nombre.get("first")), _texto(nombre.get("last")))
                    if parte
                )
                posicion_id = _entero(info_jugador.get("posnId"))

                jugadores.append({
                    "id": str(jugador_id),
                    "equipo_id": equipo_id,
                    "nombre": _texto(nombre.get("nick") or nombre.get("first")),
                    "nombre_completo": _texto(completo),
                    "dorsal": _entero(info_jugador.get("squadNo")),
                    "posicion_id": posicion_id,
                    "posicion": POSICIONES_REDUNO.get(posicion_id),
                    "pais": _texto(info_jugador.get("country")),
                    "nacimiento": _fecha(info_jugador.get("birthdate")),
                    "edad": _entero(info_jugador.get("age")),
                    "altura": _texto(info_jugador.get("ht")),
                    "peso": _texto(info_jugador.get("wt")),
                    "genero": _texto(info_jugador.get("gender")),
                    "actualizado_en": ahora,
                })

                resumen_jugador = jugador.get("summary")
                if isinstance(resumen_jugador, dict):
                    fila = {
                        "ejecucion_id": ejecucion_id,
                        "jugador_id": str(jugador_id),
                        "equipo_id": equipo_id,
                        "resumen_crudo": resumen_jugador,
                    }
                    for origen, destino in ESTADISTICAS_JUGADOR.items():
                        fila[destino] = _cantidad(resumen_jugador, origen)
                    jugadores_stats.append(fila)

    return {
        "equipos": equipos,
        "equipos_estadisticas": equipos_stats,
        "jugadores": jugadores,
        "jugadores_estadisticas": jugadores_stats,
    }


# ----------------------------------------------------------------------------
# Sincronización
# ----------------------------------------------------------------------------

def credenciales() -> tuple[str | None, str | None]:
    url = os.environ.get("SUPABASE_URL", "").strip() or None
    clave = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        or os.environ.get("SUPABASE_KEY", "").strip()
        or None
    )
    return url, clave


def hay_credenciales() -> bool:
    url, clave = credenciales()
    return bool(url and clave)


def sincronizar(
    documento: dict[str, Any],
    fuentes_config: list[dict[str, Any]] | None = None,
    retencion_dias: int = 0,
    dry_run: bool = False,
    timeout: int = 60,
) -> dict[str, int]:
    """Sube el documento a Supabase. Devuelve cuántas filas se escribieron por tabla."""
    inicio = time.monotonic()
    url, clave = credenciales()

    if not dry_run and not (url and clave):
        raise SupabaseError(
            "Faltan SUPABASE_URL y/o SUPABASE_SERVICE_ROLE_KEY en el entorno."
        )

    try:
        zona = ZoneInfo(documento.get("zona_horaria") or "America/La_Paz")
    except Exception:
        zona = None

    fila_ejecucion = _fila_ejecucion(documento, zona)
    if not fila_ejecucion["generado_en"]:
        raise SupabaseError("El documento no tiene un 'generado_en' válido.")

    if dry_run:
        # Con id 0: sirve para contar filas y ver la forma del payload sin red.
        conteos = _resumir_payload(documento, fuentes_config, 0, zona)
        conteos["ejecuciones"] = 1
        logging.info("Supabase (dry-run): %s", _formatear_conteos(conteos))
        return conteos

    cliente = ClienteSupabase(url, clave, timeout=timeout)

    # 1. Ejecución primero: todo lo demás la referencia.
    escritas = cliente.upsert(
        "ejecuciones", [fila_ejecucion],
        on_conflict=CLAVES_CONFLICTO["ejecuciones"], retornar=True,
    )
    if not escritas:
        raise SupabaseError("Supabase no devolvió el id de la ejecución.")
    ejecucion_id = escritas[0]["id"]
    logging.info("Supabase: ejecución id=%s", ejecucion_id)

    # 2. Catálogos primero, porque son el destino de las claves foráneas; y solo
    #    después los datos de la corrida. vista_primera_en no viaja en el payload
    #    de noticias a propósito: así el upsert conserva la fecha en que vimos
    #    cada nota por primera vez.
    tablas = _tablas_a_escribir(documento, fuentes_config, ejecucion_id, zona)

    for tabla, filas in tablas.items():
        cliente.upsert(tabla, filas, on_conflict=CLAVES_CONFLICTO[tabla])

    if retencion_dias > 0:
        _limpiar_ejecuciones_viejas(cliente, retencion_dias)

    conteos = {"ejecuciones": 1}
    conteos.update({
        tabla: len(_deduplicar(filas, CLAVES_CONFLICTO[tabla].split(",")))
        for tabla, filas in tablas.items()
    })

    logging.info(
        "Supabase: sincronización completa en %.1f s | %s",
        time.monotonic() - inicio,
        _formatear_conteos(conteos),
    )
    return conteos


def _tablas_a_escribir(
    documento: dict[str, Any],
    fuentes_config: list[dict[str, Any]] | None,
    ejecucion_id: int,
    zona: ZoneInfo | None,
) -> dict[str, list[dict[str, Any]]]:
    """Arma todas las filas, en el orden en que hay que escribirlas."""
    reduno = _filas_reduno(documento, ejecucion_id)
    por_fuente = _filas_por_fuente(documento, ejecucion_id, zona)

    return {
        "fuentes": _catalogo_fuentes(documento, fuentes_config),
        "equipos": reduno["equipos"],
        "jugadores": reduno["jugadores"],
        "ejecuciones_fuentes": por_fuente["ejecuciones_fuentes"],
        "tablas": por_fuente["tablas"],
        "posiciones": por_fuente["posiciones"],
        "secciones": por_fuente["secciones"],
        "noticias": por_fuente["noticias"],
        "equipos_estadisticas": reduno["equipos_estadisticas"],
        "jugadores_estadisticas": reduno["jugadores_estadisticas"],
    }


def _resumir_payload(
    documento: dict[str, Any],
    fuentes_config: list[dict[str, Any]] | None,
    ejecucion_id: int,
    zona: ZoneInfo | None,
) -> dict[str, int]:
    """Cuenta las filas que se escribirían, ya deduplicadas como en el envío real."""
    return {
        tabla: len(_deduplicar(filas, CLAVES_CONFLICTO[tabla].split(",")))
        for tabla, filas in _tablas_a_escribir(documento, fuentes_config, ejecucion_id, zona).items()
    }


def _formatear_conteos(conteos: dict[str, int]) -> str:
    return " | ".join(f"{tabla}={cantidad}" for tabla, cantidad in sorted(conteos.items()))


def _limpiar_ejecuciones_viejas(cliente: ClienteSupabase, dias: int) -> None:
    """Borra corridas viejas. El cascade se lleva tablas, posiciones y stats."""
    limite = (datetime.now().astimezone() - timedelta(days=dias)).isoformat()
    cliente.borrar("ejecuciones", {"generado_en": f"lt.{limite}"})
    logging.info("Supabase: se borraron las ejecuciones anteriores a %s.", limite)


# ----------------------------------------------------------------------------
# CLI: subir a mano el JSON que ya está en disco
# ----------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(
        description="Sube a Supabase el JSON ya generado, sin volver a scrapear."
    )
    parser.add_argument(
        "--archivo",
        type=Path,
        default=BASE_DIR / "data" / "futbol_boliviano.json",
        help="Ruta del JSON a subir.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Arma las filas y muestra los conteos sin escribir en Supabase.",
    )
    parser.add_argument(
        "--retencion-dias",
        type=int,
        default=0,
        help="Borra las ejecuciones más viejas que N días. 0 conserva todo.",
    )
    args = parser.parse_args()

    if not args.archivo.exists():
        logging.error("No existe el archivo: %s", args.archivo)
        return 1

    with args.archivo.open("r", encoding="utf-8") as archivo:
        documento = json.load(archivo)

    ruta_fuentes = BASE_DIR / "fuentes.json"
    fuentes_config = None
    if ruta_fuentes.exists():
        with ruta_fuentes.open("r", encoding="utf-8") as archivo:
            fuentes_config = json.load(archivo)

    try:
        sincronizar(
            documento,
            fuentes_config=fuentes_config,
            retencion_dias=args.retencion_dias,
            dry_run=args.dry_run,
        )
    except SupabaseError as error:
        logging.error("%s", error)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
