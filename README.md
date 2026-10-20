# 🌲 Radar de empleos Seattle + Miami

Robot que todos los días revisa los portales de empleo de mis empresas objetivo,
filtra vacantes de datos/analytics en Seattle o remotas y las clasifica según
lo que dicen sobre patrocinio de visa:

- 🟢 el aviso menciona patrocinio, o la empresa tiene historial de patrocinar
- 🟡 el aviso no dice nada sobre visas
- 🔴 el aviso dice que no patrocina (oculto por defecto en la página)

Hecho con Python + GitHub Actions + GitHub Pages.

## Novedades v2

- Dos áreas en la página: **Seattle** y **Miami / Weston** (cada empresa tiene `region` en `companies.yaml`).
- Lee también portales **Workday** (campo `workday_url`) y detecta el portal desde `careers_url`.
- Botón **Crear CV para esta vacante**: arma un prompt con tu CV + el aviso, lo copia y abre Claude.
  Tu CV se guarda **solo en tu navegador** (botón "Cargar CV"), nunca en el repo.

## Archivos

| Archivo | Para qué |
|---|---|
| `companies.yaml` | Empresas, palabras clave y notas de Glassdoor. **El único que se edita a mano** |
| `scraper.py` | Lee Greenhouse, Lever y Ashby, filtra y clasifica |
| `.github/workflows/radar-diario.yml` | Corre el script todos los días a las 7 AM (Chile) |
| `docs/index.html` | La página |
| `docs/data/jobs.json` | Lo genera el robot, no tocar |

## Instalación (una sola vez)

1. Crea un repositorio nuevo en GitHub (por ejemplo `radar-seattle`). Para usar Pages gratis debe ser **público**.
2. Sube todos estos archivos respetando las carpetas (incluida `.github/workflows/`).
3. En el repo: **Settings → Pages → Source: Deploy from a branch → Branch: `main`, carpeta `/docs`** → Save.
4. Ve a la pestaña **Actions**, elige "Radar diario de empleos" y aprieta **Run workflow**.
5. Abre `https://TU-USUARIO.github.io/radar-seattle/`. Desde ahora se actualiza solo cada mañana.

Si el paso 4 falla al guardar resultados: **Settings → Actions → General → Workflow permissions → Read and write**.

## Mantenimiento

- **Una empresa aparece en rojo en "Empresas que sigue el robot":** su slug o portal está mal. Abre su página de empleos,
  mira la URL (`jobs.lever.co/ALGO`, `boards.greenhouse.io/ALGO`, `jobs.ashbyhq.com/ALGO`) y corrige `slug` y `ats` en `companies.yaml`.
  Si usa otro sistema (Workday, propio, etc.), este robot no la puede leer todavía.
- **Notas de Glassdoor:** cada uno o dos meses, completa `rating`, `reviews` y `checked` (AAAA-MM-DD).
  La página avisa cuando una nota tiene más de 60 días.
- **Agregar empresas:** copia un bloque de `companies.yaml` y cambia los datos.

## Limitaciones

- La clasificación de visa es por texto. Un 🟡 no garantiza nada: confírmalo en la entrevista.
- Recuerda mencionar la **H-1B1 (Chile)**: no tiene lotería ni el cargo de USD 100.000.
