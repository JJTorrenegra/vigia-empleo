# Vigía de empleo público

Revisa cada día el **BOE** y el **BORM**, descarga el **texto completo** de cada
anuncio y te avisa de los que contienen *administrativo* y alguno de tus
municipios. Los resultados de los últimos 30 días quedan en una página web, y
cada novedad te llega por correo como un *issue* de GitHub.

## Qué vigila

| Fuente | Dónde busca el municipio |
|---|---|
| BOE · Oposiciones (canal 140) | En el título («Ayuntamiento de X») |
| BOE · Concursos de personal (canal 141) | En el título |
| BOE · III. Otras disposiciones | En todo el texto |
| BOE · V.B. Otros anuncios oficiales | En todo el texto |
| BORM · Comunidad Autónoma (sección I) | No se exige; basta «administrativo» |
| BORM · Administración Local (sección IV) | El ayuntamiento que publica debe ser uno de los tuyos |
| BORM · Particulares (sección V) | En todo el texto |

«administrativo» se busca siempre en el **texto completo**, no solo en el título.
La primera ejecución rellena además los 30 días anteriores del BOE (sección II.B)
con la API de datos abiertos.

> **Importante:** el RSS del BORM solo trae el boletín del día. El vigía se ejecuta
> dos veces al día para no perder ninguno, pero si GitHub fallara un día entero,
> ese BORM no se revisaría.

## Puesta en marcha (una sola vez, unos 10 minutos)

1. **Crea una cuenta** en <https://github.com> si no la tienes.
2. **Crea un repositorio nuevo** (botón «New»), por ejemplo `vigia-empleo`.
   - Marca **Public** si quieres la página web gratis (ver «Privacidad» abajo).
3. **Sube los archivos**: en el repositorio, «Add file → Upload files» y arrastra
   `vigia.py`, `config.yaml`, `requirements.txt`, `README.md` y las carpetas
   `datos` y `docs`. Pulsa «Commit changes».
4. **Crea el workflow** (la carpeta `.github` es oculta y a veces no se sube
   arrastrando): «Add file → Create new file», escribe como nombre
   `.github/workflows/vigia.yml`, pega el contenido de ese archivo y guarda.
5. **Permisos**: Settings → Actions → General → *Workflow permissions* →
   marca **Read and write permissions** → Save.
6. **Página web**: Settings → Pages → *Source*: «Deploy from a branch» →
   rama `main`, carpeta `/docs` → Save. Al cabo de un minuto tendrás la dirección
   (algo como `https://TU-USUARIO.github.io/vigia-empleo/`). Guárdala en el móvil.
7. **Primera ejecución**: pestaña **Actions** → «Vigía de empleo público» →
   **Run workflow**. La primera vez tarda más (rellena 30 días).
8. **Correo**: en tu perfil → Settings → Notifications, comprueba que tienes el
   correo activado para *Watching*. Los avisos llegan como issues nuevos.

A partir de ahí funciona solo, todos los días.

## Cambiar la búsqueda

Todo está en `config.yaml`, que puedes editar desde la web de GitHub (icono del
lápiz): palabras clave, municipios, fuentes activas y dónde se busca el
municipio en cada una. Los cambios se aplican en la siguiente ejecución.

Detalles útiles:
- «administrativo» también encuentra *Auxiliar Administrativo* y *administrativos*.
  Si solo quieres un cuerpo concreto, cambia la palabra clave.
- En el BOE, los anuncios de la Comunidad Autónoma o de la Universidad de Murcia no
  dicen «Ayuntamiento de…», así que no aparecen con la búsqueda por título. Lo de
  la Comunidad Autónoma ya lo cubre el BORM.
- En los anuncios con «En todo el texto» puede salir algo de ruido (por ejemplo,
  una dirección en Murcia). Si molesta, cambia ese `municipio_en` a `titulo`.

## Si algo falla

- Si una fuente no responde, la ejecución termina en rojo y **GitHub te envía un
  correo**. Lo demás se guarda igualmente; al día siguiente vuelve a intentarlo.
- GitHub puede pausar las tareas programadas de un repositorio sin actividad
  durante 60 días. El propio vigía guarda cambios a diario, pero si alguna vez
  ves el aviso en la pestaña Actions, basta con pulsar «Enable workflow».

## Privacidad

Con el plan gratuito, la página web solo funciona si el repositorio es
**público**: cualquiera con el enlace podría ver qué buscas (son anuncios
oficiales públicos, pero tenlo en cuenta). Si prefieres un repositorio
**privado**, todo funciona igual excepto la página; los resultados los verías en
los avisos por correo y en `docs/index.html` dentro del repositorio.

## Probar en tu ordenador (opcional)

```
pip install -r requirements.txt
python vigia.py --sin-avisos
```
y abre `docs/index.html` en el navegador.
