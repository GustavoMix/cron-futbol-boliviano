# Actualizador de Fútbol Boliviano V4

Genera los datos que usa la app Android **Fútbol Boliviano**.

## Cómo funciona

| Qué | De dónde sale |
| --- | --- |
| Tabla de posiciones, fechas, resultados, próximos partidos, goleadores, asistencias, equipos | **Wikipedia** (artículo "Primera División de Bolivia AAAA") — fuente principal |
| Noticias | Sitios web que sí responden: FBF, Unitel, DIEZ, Red Uno, Opinión, Los Tiempos, La Razón, Bolívar, Always Ready… |

- No usa Facebook, SofaScore ni páginas "en vivo" (bloquean a los robots).
- Si una fuente falla o bloquea, se conservan los últimos datos buenos.
- Fuentes que bloquean siempre se apagan en `fuentes.json` (`"habilitada": false`).

## Archivos que genera

```text
data\app.json              <- pequeño (~80 KB), el que lee la app
data\futbol_boliviano.json <- completo, todo lo extraído de cada sitio
```

## Automático

GitHub Actions lo ejecuta cada 2 horas (08:00 a 00:00 hora de Bolivia) y sube
los JSON al repositorio. También se puede lanzar a mano desde la pestaña
**Actions → Actualizar fútbol boliviano → Run workflow**.

## En tu PC (Windows)

```bat
py -m pip install -r requirements.txt
py actualizador_futbol_boliviano.py
```

Probar solo Wikipedia:

```bat
py wikipedia_bolivia.py
```

Otras opciones:

```bat
py actualizador_futbol_boliviano.py --list-sources
py actualizador_futbol_boliviano.py --source diez_futbol
py actualizador_futbol_boliviano.py --loop --minutes 180
```
