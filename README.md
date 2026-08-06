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

Además, si está configurado Supabase, los mismos datos se escriben en la base.

## Supabase

GitHub Actions sigue siendo el que ejecuta el scraper; Supabase es donde quedan
los datos para consultarlos. El JSON se sigue commiteando al repo igual que
antes, así que nada de lo que ya lea `data/futbol_boliviano.json` se rompe.

### 1. Crear las tablas

En el panel de Supabase: **SQL Editor → New query**, pegar el contenido de
`supabase/schema.sql` y ejecutar. Se puede volver a correr sin problema.

### 2. Cargar los secrets en GitHub

En **Settings → Secrets and variables → Actions** del repo, agregar:

| Secret | Dónde sacarlo |
| --- | --- |
| `SUPABASE_URL` | Project Settings → Data API → Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Project Settings → API Keys → `service_role` |

La clave `service_role` saltea las políticas de seguridad: va únicamente en los
secrets de GitHub, nunca en el código ni en una app cliente. Para leer desde una
app se usa la clave `anon`, que solo tiene permiso de lectura.

Sin estos secrets el programa avisa y sigue funcionando: genera el JSON igual.

### 3. Probar en local

```bat
set SUPABASE_URL=https://xxxxxxxx.supabase.co
set SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOi...

py supabase_sync.py --dry-run
py supabase_sync.py
```

`--dry-run` arma las filas y muestra cuántas iría a escribir en cada tabla, sin
tocar la base. `supabase_sync.py` sube el JSON que ya está en `data/` sin volver
a scrapear, que es lo más rápido para probar.

Desde el actualizador completo:

```bat
py actualizador_futbol_boliviano.py --supabase-dry-run
py actualizador_futbol_boliviano.py --sin-supabase
```

### Qué queda en cada tabla

| Tabla | Contenido |
| --- | --- |
| `ejecuciones` | Una fila por corrida, con el resumen. |
| `fuentes` | Catálogo de las 20 fuentes. |
| `ejecuciones_fuentes` | Cómo le fue a cada fuente en cada corrida. |
| `posiciones` | Tablas de posiciones normalizadas, listas para ordenar y filtrar. |
| `tablas` | Todas las tablas HTML crudas en `jsonb`, incluidas las que no son de posiciones. |
| `secciones` | Bloques de texto (goleadores, fixture, etc.). |
| `noticias` | Archivo histórico deduplicado por URL. |
| `equipos` / `jugadores` | Catálogo estructurado de Red Uno. |
| `equipos_estadisticas` / `jugadores_estadisticas` | Goles, tarjetas, minutos, etc. por corrida. |

Vistas ya armadas para lo más pedido: `v_posiciones_actuales`, `v_goleadores`,
`v_noticias_recientes`, `v_estado_fuentes` y `v_ultima_ejecucion`.

### Consultar desde una app

Con la clave `anon` y la librería de Supabase:

```js
// Tabla de posiciones de la División Profesional
const { data } = await supabase
  .from('v_posiciones_actuales')
  .select('pos, equipo, pj, puntos, diferencia')
  .eq('fuente_id', 'fbf_division_profesional')
  .order('pos');

// Los diez máximos goleadores
const { data: goleadores } = await supabase
  .from('v_goleadores')
  .select('nombre_completo, equipo, goles, partidos')
  .limit(10);

// Últimas noticias de un club
const { data: noticias } = await supabase
  .from('v_noticias_recientes')
  .select('titulo, url, fecha')
  .eq('fuente_id', 'bolivar_noticias')
  .limit(20);
```

### Retención

`config.json` tiene `supabase.retencion_dias` en 90: las corridas más viejas se
borran solas, y con ellas sus tablas, posiciones y estadísticas. Poner `0`
conserva todo. Las noticias y los catálogos de equipos y jugadores no se borran
nunca, porque son archivo histórico y no dependen de una corrida.

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

Un jugador transferido a mitad de temporada aparece en el plantel de sus dos
clubes. Por eso `jugadores_estadisticas` guarda una fila por jugador **y**
equipo, y `jugadores.equipo_id` es solo el último club en el que se lo vio.
