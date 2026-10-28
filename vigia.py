#!/usr/bin/env python3
"""
Vigía de empleo público: lee el BOE y el BORM, descarga el texto completo
de cada anuncio y avisa de los que cumplen la búsqueda de config.yaml.

Uso:  python vigia.py            (ejecución normal)
      python vigia.py --sin-avisos (no abre issues; útil para probar)
"""
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
import yaml

RAIZ = Path(__file__).resolve().parent
DATOS = RAIZ / "datos"
DOCS = RAIZ / "docs"
F_VISTOS = DATOS / "vistos.json"
F_RESULTADOS = DATOS / "resultados.json"
F_ESTADO = DATOS / "estado.json"

HOY = dt.date.today()
PAUSA = 0.4  # segundos entre descargas, para no saturar los servidores
FILTRO_VERSION = 2  # súbelo si cambias la lógica de búsqueda: se vuelve a analizar todo

# "administrativo" solo cuenta si aparece como puesto de trabajo, no como
# adjetivo legal ("procedimiento administrativo", "carga administrativa"...).
CONTEXTO_ANTES = set("""plaza plazas puesto puestos cuerpo escala subescala categoria
categorias personal bolsa auxiliar auxiliares tecnico tecnica tecnicos tecnicas oficial
ayudante convoca convocatoria proveer cubrir oposicion oposiciones concurso seleccion
contratacion contratar empleo""".split())
CONTEXTO_DESPUES = set("plaza plazas vacante vacantes subgrupo c1 c2".split())
# Un municipio precedido de esto no cuenta ("Región de Murcia" no es Murcia).
NO_MUNICIPIO = [["region", "de"], ["universidad", "de"], ["provincia", "de"], ["comunidad", "de"]]

SESION = requests.Session()
SESION.headers["User-Agent"] = "VigiaEmpleoPublico/1.0 (uso personal)"

errores = []  # errores no fatales que se muestran al final


def log(msg):
    print(msg, flush=True)


# ----------------------------------------------------------------- texto ---

def _norm_char(c):
    """Un carácter -> un carácter: minúscula, sin tilde, no alfanumérico = espacio.
    Mantiene la longitud para poder recortar fragmentos del texto original."""
    base = unicodedata.normalize("NFD", c)[0].lower()
    return base if base.isalnum() else " "


def normalizar(texto):
    return "".join(_norm_char(c) for c in texto)


def patron_frase(frase):
    palabras = normalizar(frase).split()
    return r"\s+".join(re.escape(p) for p in palabras)


class Buscador:
    def __init__(self, cfg):
        claves = "|".join(patron_frase(p) for p in cfg["palabras_clave"])
        self.re_clave = re.compile(rf"\b(?:{claves})s?\b")
        self.exigir_puesto = cfg.get("exigir_contexto_de_puesto", True)
        self.municipios = cfg["municipios"]
        self.mun_norm = {m: " ".join(normalizar(m).split()) for m in self.municipios}
        self.re_mun_texto = {
            m: re.compile(rf"\b{patron_frase(m)}\b") for m in self.municipios}
        self.re_mun_titulo = {
            m: re.compile(rf"\bayuntamiento\s+de\s+{patron_frase(m)}\b")
            for m in self.municipios}

    def _es_puesto(self, n, texto, m):
        antes = n[max(0, m.start() - 80):m.start()].split()[-3:]
        despues = n[m.end():m.end() + 60].split()[:3]
        if any(w in CONTEXTO_ANTES for w in antes):
            return True
        if any(w in CONTEXTO_DESPUES for w in despues):
            return True
        return bool(re.match(r"\s*/\s*[oa]\b", texto[m.end():m.end() + 6]))  # «Administrativo/a»

    def clave(self, texto):
        """Devuelve un fragmento alrededor de la primera coincidencia válida, o None."""
        n = normalizar(texto)
        for m in self.re_clave.finditer(n):
            if self.exigir_puesto and not self._es_puesto(n, texto, m):
                continue
            ini, fin = max(0, m.start() - 160), min(len(texto), m.end() + 160)
            frag = " ".join(texto[ini:fin].split())
            return ("…" if ini > 0 else "") + frag + ("…" if fin < len(texto) else "")
        return None

    def municipio(self, modo, titulo="", organismo="", texto=""):
        """Devuelve el municipio encontrado según el modo, '' si no se exige,
        o None si no hay coincidencia."""
        if modo == "ninguno":
            return ""
        if modo == "organismo":
            org = " ".join(normalizar(organismo).split())
            org = re.sub(r"^(excmo\s+)?ayuntamiento\s+de\s+", "", org)
            for m, mn in self.mun_norm.items():
                if org == mn:
                    return m
            return None
        if modo == "titulo":
            n = normalizar(titulo)
            for m, rx in self.re_mun_titulo.items():
                if rx.search(n):
                    return m
            return None
        if modo == "texto":
            n = normalizar(" ".join([titulo, organismo, texto]))
            for m, rx in self.re_mun_texto.items():
                for hit in rx.finditer(n):
                    previo = n[max(0, hit.start() - 30):hit.start()].split()[-2:]
                    if previo not in NO_MUNICIPIO:
                        return m
            return None
        raise ValueError(f"municipio_en desconocido: {modo}")


# ------------------------------------------------------------- descargas ---

def descargar(url, accept=None, intentos=3):
    cab = {"Accept": accept} if accept else {}
    for i in range(intentos):
        try:
            r = SESION.get(url, headers=cab, timeout=40)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            time.sleep(PAUSA)
            return r
        except requests.RequestException as e:
            if i == intentos - 1:
                raise
            log(f"   reintentando ({e})")
            time.sleep(3 * (i + 1))


def texto_xml(contenido):
    root = ET.fromstring(contenido)
    return " ".join(t.strip() for t in root.itertext() if t.strip())


def texto_boe(id_boe):
    r = descargar(f"https://www.boe.es/diario_boe/xml.php?id={id_boe}")
    if r is not None and r.content.strip():
        try:
            root = ET.fromstring(r.content)
            meta = root.find("metadatos")
            org = (meta.findtext("departamento") or "") if meta is not None else ""
            cuerpo = root.find("texto")
            txt = texto_xml(ET.tostring(cuerpo)) if cuerpo is not None else texto_xml(r.content)
            return txt, org.strip()
        except ET.ParseError:
            pass
    r = descargar(f"https://www.boe.es/diario_boe/txt.php?id={id_boe}")
    if r is None:
        return "", ""
    limpio = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", limpio)), ""


def texto_pdf(url):
    from pypdf import PdfReader
    r = descargar(url)
    if r is None:
        return ""
    lector = PdfReader(io.BytesIO(r.content))
    return "\n".join((p.extract_text() or "") for p in lector.pages)


def leer_rss(url):
    r = descargar(url)
    if r is None:
        raise RuntimeError(f"El feed no existe (404): {url}")
    root = ET.fromstring(r.content)  # ElementTree respeta la codificación declarada
    for it in root.iter("item"):
        cats = [(c.text or "").strip() for c in it.findall("category")]
        fecha = None
        if it.findtext("pubDate"):
            try:
                fecha = parsedate_to_datetime(it.findtext("pubDate")).date()
            except (TypeError, ValueError):
                pass
        yield {
            "titulo": " ".join((it.findtext("title") or "").split()),
            "url": (it.findtext("link") or "").strip(),
            "descripcion": " ".join((it.findtext("description") or "").split()),
            "categorias": cats,
            "fecha": fecha or HOY,
        }


# ----------------------------------------------------------------- fuentes ---

def candidatos_boe_rss(fuente):
    for it in leer_rss(fuente["url"]):
        m = re.search(r"BOE-[A-Z]-\d{4}-\d+", it["url"] + " " + it["descripcion"])
        if not m:
            continue  # p. ej. la entrada "Sumario"
        yield {
            "id": m.group(0), "fuente": fuente["nombre"], "titulo": it["titulo"],
            "organismo": "", "seccion": "", "fecha": it["fecha"],
            "url": f"https://www.boe.es/diario_boe/txt.php?id={m.group(0)}",
            "pdf": "", "modo": fuente["municipio_en"], "lector": "boe",
        }


def seccion_borm(seccion):
    m = re.match(r"\s*([IVX]+)", seccion)
    romano = m.group(1) if m else ""
    if "particular" in seccion.lower():
        return "particulares"
    return {"I": "comunidad_autonoma", "IV": "administracion_local",
            "V": "particulares"}.get(romano)


def candidatos_borm(fuente):
    conf = fuente.get("secciones", {})
    for it in leer_rss(fuente["url"]):
        seccion = it["categorias"][0] if it["categorias"] else ""
        organismo = it["categorias"][1] if len(it["categorias"]) > 1 else it["descripcion"]
        tipo = seccion_borm(seccion)
        if not tipo or not conf.get(tipo, {}).get("activa", False):
            continue
        m = re.search(r"/anuncio/(\d+)", it["url"])
        yield {
            "id": f"BORM-{m.group(1) if m else it['url']}", "fuente": fuente["nombre"],
            "titulo": it["titulo"], "organismo": organismo, "seccion": seccion,
            "fecha": it["fecha"], "url": it["url"], "pdf": it["url"],
            "modo": conf[tipo]["municipio_en"], "lector": "pdf",
        }


def _recorrer_sumario(nodo, seccion="", departamento=""):
    """Recorre el JSON del sumario del BOE sin depender de su forma exacta."""
    if isinstance(nodo, list):
        for x in nodo:
            yield from _recorrer_sumario(x, seccion, departamento)
    elif isinstance(nodo, dict):
        if "identificador" in nodo and "titulo" in nodo:
            yield seccion, departamento, nodo
            return
        sec, dep = seccion, departamento
        if "departamento" in nodo:          # nodo de sección
            sec = str(nodo.get("codigo", seccion))
        elif "epigrafe" in nodo or ("item" in nodo and not departamento):  # departamento
            dep = str(nodo.get("nombre", departamento))
        for v in nodo.values():
            if isinstance(v, (list, dict)):
                yield from _recorrer_sumario(v, sec, dep)


def candidatos_relleno_boe(cfg, estado):
    rel = cfg.get("relleno_boe", {})
    if not rel.get("activo"):
        return
    secciones = {str(s).upper() for s in rel.get("secciones", [])}
    hechos = set(estado.get("dias_boe", []))
    for d in range(cfg["dias"], -1, -1):
        dia = HOY - dt.timedelta(days=d)
        clave = dia.isoformat()
        if clave in hechos or dia.weekday() == 6:  # domingo: no hay BOE
            continue
        try:
            r = descargar(f"https://www.boe.es/datosabiertos/api/boe/sumario/{dia:%Y%m%d}",
                          accept="application/json")
        except requests.RequestException as e:
            errores.append(f"API BOE {clave}: {e}")
            continue
        if r is None:
            if dia < HOY:
                hechos.add(clave)  # no hubo boletín ese día
            continue
        try:
            datos = r.json()
        except ValueError:
            errores.append(f"API BOE {clave}: respuesta no es JSON")
            continue
        n = 0
        for sec, dep, item in _recorrer_sumario(datos):
            if sec.upper() not in secciones:
                continue
            n += 1
            yield {
                "id": item["identificador"], "fuente": f"BOE · sección {sec} (relleno)",
                "titulo": " ".join(str(item["titulo"]).split()), "organismo": dep,
                "seccion": sec, "fecha": dia,
                "url": f"https://www.boe.es/diario_boe/txt.php?id={item['identificador']}",
                "pdf": "", "modo": rel.get("municipio_en", "titulo"), "lector": "boe",
            }
        log(f"   API BOE {clave}: {n} anuncios en {', '.join(sorted(secciones))}")
        if dia < HOY:  # el de hoy se vuelve a mirar por si aún no estaba completo
            hechos.add(clave)
    limite = (HOY - dt.timedelta(days=cfg["dias"] + 5)).isoformat()
    estado["dias_boe"] = sorted(h for h in hechos if h >= limite)


# -------------------------------------------------------------- análisis ---

def analizar(c, buscador):
    """Devuelve el resultado si el anuncio cumple la búsqueda, si no None."""
    # Atajo: si el municipio se exige en título/organismo y no está, no hace
    # falta descargar el texto.
    if c["modo"] in ("titulo", "organismo"):
        mun = buscador.municipio(c["modo"], c["titulo"], c["organismo"])
        if mun is None:
            return None
    if c["lector"] == "boe":
        texto, org = texto_boe(c["id"])
        c["organismo"] = c["organismo"] or org
    else:
        texto = texto_pdf(c["pdf"])
    fragmento = buscador.clave(texto) or buscador.clave(c["titulo"])
    if not fragmento:
        return None
    mun = buscador.municipio(c["modo"], c["titulo"], c["organismo"], texto)
    if mun is None:
        return None
    promocion = bool(re.search(r"promocion\s+interna|turno\s+de\s+promocion|promocion\s+cruzada",
                               normalizar(texto)))
    return {
        "promocion": promocion,
        "id": c["id"], "fuente": c["fuente"], "titulo": c["titulo"],
        "organismo": c["organismo"], "seccion": c["seccion"],
        "fecha": c["fecha"].isoformat(), "url": c["url"], "municipio": mun,
        "fragmento": fragmento[:500], "detectado": HOY.isoformat(),
    }


# ----------------------------------------------------------------- avisos ---

def crear_issue(titulo, cuerpo):
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        log("   (sin GITHUB_TOKEN: no se abre issue; solo en local)")
        return
    r = SESION.post(f"https://api.github.com/repos/{repo}/issues",
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/vnd.github+json"},
                    json={"title": titulo[:250], "body": cuerpo}, timeout=30)
    if r.status_code >= 300:
        errores.append(f"No se pudo crear el issue: {r.status_code} {r.text[:200]}")


def ficha_md(r):
    org = f"**{r['organismo']}** · " if r["organismo"] else ""
    return (f"### {r['titulo']}\n{org}{r['fuente']} · publicado el {fecha_es(r['fecha'])}"
            f" · municipio: {r['municipio'] or '—'}{' · ⚠️ promoción interna' if r.get('promocion') else ''}\n\n> {r['fragmento']}\n\n"
            f"🔗 {r['url']}\n")


def avisar(nuevos, cfg):
    av = cfg.get("avisos", {})
    if not av.get("issues") or not nuevos:
        return
    nota = ("\n\n---\n*Recuerda: en las convocatorias del BOE el plazo suele contar "
            "desde el día siguiente a su publicación.*")
    if len(nuevos) > av.get("agrupar_si_mas_de", 8):
        cuerpo = "\n\n".join(ficha_md(r) for r in nuevos)
        crear_issue(f"🔔 {len(nuevos)} novedades de empleo público ({fecha_es(HOY.isoformat())})",
                    cuerpo[:60000] + nota)
    else:
        for r in nuevos:
            origen = "BORM" if r["fuente"].startswith("BORM") else "BOE"
            crear_issue(f"🔔 [{origen}] {r['municipio'] + ' · ' if r['municipio'] else ''}{r['titulo']}",
                        ficha_md(r) + nota)


# ----------------------------------------------------------------- página ---

MESES = "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split()


def fecha_es(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.day} de {MESES[d.month - 1]} de {d.year}"


def generar_pagina(resultados, cfg):
    desde = (HOY - dt.timedelta(days=cfg["dias"])).isoformat()
    lista = sorted((r for r in resultados if r["fecha"] >= desde),
                   key=lambda r: (r["fecha"], r["id"]), reverse=True)
    e = html.escape
    tarjetas = []
    for r in lista:
        nuevo = (r["detectado"] >= (HOY - dt.timedelta(days=1)).isoformat()
                 and r["fecha"] >= (HOY - dt.timedelta(days=3)).isoformat())
        etiqueta = "BORM" if r["fuente"].startswith("BORM") else "BOE"
        tarjetas.append(f"""
  <article class="{'nuevo' if nuevo else ''}">
    <div class="meta"><span class="tag {etiqueta.lower()}">{etiqueta}</span>
      <time>{fecha_es(r['fecha'])}</time>{'<span class="badge">nuevo</span>' if nuevo else ''}{'<span class="badge prom">promoción interna</span>' if r.get('promocion') else ''}</div>
    <h2><a href="{e(r['url'])}" target="_blank" rel="noopener">{e(r['titulo'])}</a></h2>
    <p class="org">{e(r['organismo'] or '')}{' · ' if r['organismo'] else ''}{e(r['fuente'])}{' · <b>' + e(r['municipio']) + '</b>' if r['municipio'] else ''}</p>
    <p class="frag">{e(r['fragmento'])}</p>
  </article>""")
    vacio = '<p class="vacio">No hay anuncios que cumplan la búsqueda en este periodo.</p>'
    pagina = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vigía de empleo público</title>
<style>
 :root{{--bg:#f6f5f1;--fg:#1d1d1b;--mut:#6b6a65;--card:#fff;--line:#e3e1da;--acc:#9a3b1b}}
 @media (prefers-color-scheme:dark){{:root{{--bg:#151513;--fg:#ecebe6;--mut:#a3a29b;--card:#1f1f1c;--line:#33322e;--acc:#e0875f}}}}
 *{{box-sizing:border-box}} body{{margin:0;font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}}
 header,main{{max-width:820px;margin:auto;padding:20px 16px}} header h1{{margin:0 0 4px;font-size:1.5rem}}
 header p{{margin:0;color:var(--mut);font-size:.9rem}}
 article{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:0 0 12px}}
 article.nuevo{{border-left:4px solid var(--acc)}}
 .meta{{display:flex;gap:8px;align-items:center;font-size:.82rem;color:var(--mut)}}
 .tag{{font-weight:700;padding:1px 7px;border-radius:4px;border:1px solid var(--line)}}
 .badge{{background:var(--acc);color:#fff;border-radius:4px;padding:1px 7px;font-weight:600}}
 h2{{font-size:1.02rem;margin:6px 0}} h2 a{{color:var(--fg)}} h2 a:hover{{color:var(--acc)}}
 .org{{margin:0 0 6px;font-size:.88rem;color:var(--mut)}} .frag{{margin:0;font-size:.9rem}}
 .badge.prom{{background:transparent;color:var(--acc);border:1px solid var(--acc)}}
 .vacio{{color:var(--mut)}} footer{{max-width:820px;margin:auto;padding:0 16px 30px;font-size:.8rem;color:var(--mut)}}
</style></head><body>
<header><h1>Vigía de empleo público</h1>
<p>{len(lista)} anuncio(s) en los últimos {cfg['dias']} días · actualizado el {fecha_es(HOY.isoformat())}</p></header>
<main>{''.join(tarjetas) or vacio}</main>
<footer>Búsqueda: {e(', '.join(cfg['palabras_clave']))} + {len(cfg['municipios'])} municipios. En el BOE, el plazo de solicitudes suele contar desde el día siguiente a la publicación.</footer>
</body></html>"""
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(pagina, encoding="utf-8")


# ----------------------------------------------------------------- principal ---

def cargar(path, defecto):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else defecto


def guardar(path, datos):
    path.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    sin_avisos = "--sin-avisos" in sys.argv
    cfg = yaml.safe_load((RAIZ / "config.yaml").read_text(encoding="utf-8"))
    buscador = Buscador(cfg)
    DATOS.mkdir(exist_ok=True)
    vistos = cargar(F_VISTOS, {})
    resultados = cargar(F_RESULTADOS, [])
    estado = cargar(F_ESTADO, {})
    reanalisis = False
    if estado.get("filtro") != FILTRO_VERSION:
        if vistos or resultados:
            log("Filtro actualizado: se vuelve a analizar todo (sin enviar avisos repetidos).")
            vistos, resultados, estado["dias_boe"] = {}, [], []
            reanalisis = True
        estado["filtro"] = FILTRO_VERSION
    ids_resultado = {r["id"] for r in resultados}
    fuentes_caidas = 0

    def generadores():
        for f in cfg["fuentes"]:
            if not f.get("activa", True):
                continue
            yield f["nombre"], (candidatos_boe_rss(f) if f["tipo"] == "boe_rss"
                                else candidatos_borm(f))
        yield "Relleno BOE (API)", candidatos_relleno_boe(cfg, estado)

    nuevos = []
    for nombre, gen in generadores():
        log(f"▶ {nombre}")
        n_rev = 0
        try:
            for c in gen:
                if c["id"] in vistos:
                    continue
                try:
                    res = analizar(c, buscador)
                except Exception as ex:  # un anuncio roto no para el resto
                    errores.append(f"{c['id']}: {ex}")
                    continue
                vistos[c["id"]] = HOY.isoformat()
                n_rev += 1
                if res and res["id"] not in ids_resultado:
                    log(f"   ✔ {res['titulo'][:90]}")
                    resultados.append(res)
                    ids_resultado.add(res["id"])
                    nuevos.append(res)
        except Exception as ex:
            fuentes_caidas += 1
            errores.append(f"Fuente «{nombre}» no disponible: {ex}")
        log(f"   {n_rev} anuncios nuevos revisados")

    # Limpieza: olvidar lo visto hace más de 90 días
    limite = (HOY - dt.timedelta(days=90)).isoformat()
    vistos = {k: v for k, v in vistos.items() if v >= limite}
    resultados = [r for r in resultados if r["fecha"] >= limite]

    guardar(F_VISTOS, vistos)
    guardar(F_RESULTADOS, resultados)
    guardar(F_ESTADO, estado)
    generar_pagina(resultados, cfg)
    if not sin_avisos and not reanalisis:
        avisar(nuevos, cfg)

    log(f"\nNovedades: {len(nuevos)}")
    if errores:
        log("Avisos/errores:\n  - " + "\n  - ".join(errores[:50]))
    if fuentes_caidas:
        sys.exit(1)  # GitHub te avisará por correo de que algo ha fallado


if __name__ == "__main__":
    main()
