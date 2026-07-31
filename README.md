# Actualizador de Fútbol Boliviano V3 — Sin SofaScore

Esta versión elimina completamente SofaScore.

## Fuentes

Tiene 20 fuentes:

- 4 páginas oficiales de torneos de la FBF.
- 4 secciones adicionales de la FBF.
- 2 clubes oficiales.
- 9 medios bolivianos.
- 1 fuente JSON estructurada de Red Uno.

## Instalar

```bat
py -m pip install -r requirements.txt
```

## Probar primero Red Uno estructurado

```bat
py actualizador_futbol_boliviano.py --source reduno_estadisticas_equipos
```

## Probar la División Profesional de la FBF

```bat
py actualizador_futbol_boliviano.py --source fbf_division_profesional
```

## Ejecutar las 20 fuentes

```bat
py actualizador_futbol_boliviano.py
```

## Ver la lista

```bat
py actualizador_futbol_boliviano.py --list-sources
```

## Resultado

```text
data\futbol_boliviano.json
```

## Modo continuo

Como son 20 fuentes, el intervalo predeterminado es de tres horas:

```bat
py actualizador_futbol_boliviano.py --loop --minutes 180
```

Durante pruebas puedes usar 30 minutos:

```bat
py actualizador_futbol_boliviano.py --loop --minutes 30
```

## Importante

Los medios sirven principalmente para noticias, amistosos, reprogramaciones,
convocatorias y resultados reportados. No todos publican tablas, asistencias o
tarjetas en formato estructurado.

La fuente estructurada de Red Uno descarga la lista de equipos y el JSON disponible
para cada equipo. Su estructura puede cambiar. El programa conserva los últimos
datos válidos si una fuente falla.
