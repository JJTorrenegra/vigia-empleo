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
FILTRO_VERSION = 3  # súbelo si cambias la lógica de búsqueda: se vuelve a analizar todo

# "administrativo" solo cuenta si aparece como puesto de trabajo, no como
# adjetivo legal ("procedimiento administrativo", "carga administrativa"...).
CONTEXTO_ANTES = set("""plaza plazas puesto puestos cuerpo escala subescala categoria
categorias personal bolsa auxiliar auxiliares tecnico tecnica tecnicos tecnicas oficial
ayudante convoca convocatoria proveer cubrir oposicion oposiciones concurso seleccion
contratacion contratar empleo""".split())
CONTEXTO_DESPUES = set("plaza plazas vacante vacantes subgrupo c1 c2".split())
# Un municipio precedido de esto no cuenta ("Región de Murcia" no es Murcia).
NO_MUNICIPIO = [["region", "de"], ["universidad", "de"], ["provincia", "de"], ["comunidad", "de"]]

PAUSA_BORM = 2.0        # el BORM es más sensible: una petición cada 2 s
DIAS_REINTENTO = 7      # un anuncio sin poder leer se reintenta hasta 7 días
CAB_NAVEGADOR = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "application/pdf,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
    "Referer": "https://www.borm.es/",
}


class TextoNoDisponible(Exception):
    """No se pudo obtener el texto del anuncio (se reintentará más tarde)."""


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


def patron_lista(frases):
    """Regex que encuentra cualquiera de las frases, como palabras enteras y
    admitiendo plural en cada palabra («bolsa de trabajo» -> «bolsas de trabajo»)."""
    partes = []
    for f in frases or []:
        palabras = normalizar(str(f)).split()
        if palabras:
            partes.append(r"\s+".join(re.escape(w) + r"(?:s|es)?" for w in palabras))
    if not partes:
        return None
    partes.sort(key=len, reverse=True)   # la frase más larga gana
    return re.compile(r"\b(?:" + "|".join(partes) + r")\b")


RE_ADM = re.compile(r"administrativ[oa]s?")


class Buscador:
    def __init__(self, cfg):
        perfiles = cfg.get("perfiles") or cfg.get("palabras_clave") or []
        self.re_perfil = patron_lista(perfiles)
        self.re_tipo = patron_lista(cfg.get("tipos_de_convocatoria"))
        self.re_excluir = patron_lista(cfg.get("excluir_si_el_titulo_contiene"))
        self.exigir_puesto = cfg.get("exigir_contexto_de_puesto", True)
        self.municipios = cfg["municipios"]
        self.mun_norm = {m: " ".join(normalizar(m).split()) for m in self.municipios}
        self.re_mun_texto = {
            m: re.compile(rf"\b{patron_frase(m)}\b") for m in self.municipios}
        self.re_mun_titulo = {
            m: re.compile(rf"\bayuntamiento\s+de\s+{patron_frase(m)}\b")
            for m in self.municipios}
        self.re_org = {o: re.compile(rf"\b{patron_frase(o)}\b")
                       for o in cfg.get("organismos_extra") or []}

    # ---- condición 1: tipo de convocatoria (vía de entrada al empleo)
    def tipo(self, texto):
        if not self.re_tipo:
            return ""
        m = self.re_tipo.search(normalizar(texto))
        return texto[m.start():m.end()] if m else None

    def excluido(self, titulo):
        return bool(self.re_excluir and self.re_excluir.search(normalizar(titulo)))

    # ---- condición 2: perfil
    def _es_puesto(self, n, texto, m):
        antes = n[max(0, m.start() - 80):m.start()].split()[-3:]
        despues = n[m.end():m.end() + 60].split()[:3]
        if any(w in CONTEXTO_ANTES for w in antes):
            return True
        if any(w in CONTEXTO_DESPUES for w in despues):
            return True
        return bool(re.match(r"\s*/\s*[oa]\b", texto[m.end():m.end() + 6]))  # «Administrativo/a»

    def perfil(self, texto):
        """(fragmento, término encontrado) de la primera coincidencia válida, o None."""
        if not self.re_perfil or not texto:
            return None
        n = normalizar(texto)
        for m in self.re_perfil.finditer(n):
            # «administrativo» suelto solo cuenta como puesto, no como adjetivo legal
            if (self.exigir_puesto and RE_ADM.fullmatch(n[m.start():m.end()])
                    and not self._es_puesto(n, texto, m)):
                continue
            ini, fin = max(0, m.start() - 160), min(len(texto), m.end() + 160)
            frag = " ".join(texto[ini:fin].split())
            frag = ("…" if ini > 0 else "") + frag + ("…" if fin < len(texto) else "")
            return frag, " ".join(texto[m.start():m.end()].split())
        return None

    def clave(self, texto):   # compatibilidad
        r = self.perfil(texto)
        return r[0] if r else None

    # ---- condición 3: zona (municipio u organismo de interés)
    def _organismo_extra(self, *textos):
        n = normalizar(" ".join(textos))
        for o, rx in self.re_org.items():
            if rx.search(n):
                return o
        return None

    def municipio(self, modo, titulo="", organismo="", texto=""):
        """Municipio u organismo encontrado según el modo, '' si no se exige,
        o None si no hay coincidencia."""
        if modo == "ninguno":
            return ""
        if modo == "organismo":
            for parte in re.split(r"\s+·\s+", organismo):
                org = " ".join(normalizar(parte).split())
                org = re.sub(r"^(excmo\s+)?ayuntamiento\s+de\s+", "", org)
                for m, mn in self.mun_norm.items():
                    if org == mn:
                        return m
            return self._organismo_extra(organismo)
        if modo == "titulo":
            n = normalizar(titulo)
            for m, rx in self.re_mun_titulo.items():
                if rx.search(n):
                    return m
            return self._organismo_extra(titulo, organismo)
        if modo == "texto":
            org = self._organismo_extra(titulo, organismo)
            if org:
                return org
            n = normalizar(" ".join([titulo, organismo, texto]))
            for m, rx in self.re_mun_texto.items():
                for hit in rx.finditer(n):
                    previo = n[max(0, hit.start() - 30):hit.start()].split()[-2:]
                    if previo not in NO_MUNICIPIO:
                        return m
            return None
        raise ValueError(f"municipio_en desconocido: {modo}")


# ------------------------------------------------------------- descargas ---

def descargar(url, accept=None, intentos=3, cabeceras=None, sin_sesion=False, pausa=None):
    """sin_sesion=True: conexión nueva, sin cookies de peticiones anteriores."""
    cab = dict(cabeceras or {})
    if accept:
        cab["Accept"] = accept
    cliente = requests if sin_sesion else SESION
    for i in range(intentos):
        try:
            r = cliente.get(url, headers=cab, timeout=40)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            time.sleep(PAUSA if pausa is None else pausa)
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


_diag = {"n": 0}
PDF = {"intentos": 0, "fallos": 0, "seguidos": 0}   # descargas reales de PDF y fallos


def texto_pdf(url):
    """Descarga un PDF y devuelve su texto. Si el servidor responde otra cosa
    (p. ej. una página HTML en lugar del PDF), reintenta con esperas y, si no
    hay manera, lanza TextoNoDisponible para dejarlo pendiente."""
    from pypdf import PdfReader
    PDF["intentos"] += 1
    if PDF["seguidos"] >= 3:   # el servidor no responde: no machacarlo ni perder 30 s por anuncio
        PDF["fallos"] += 1
        raise TextoNoDisponible("pausado: el servidor no devolvió PDF en las últimas peticiones")
    ultimo = ""
    for espera in (0, 8, 25):
        if espera:
            time.sleep(espera)
        try:
            r = descargar(url, cabeceras=CAB_NAVEGADOR, sin_sesion=True,
                          pausa=PAUSA_BORM, intentos=1)
        except requests.RequestException as e:
            ultimo = f"error de red o HTTP: {e}"
            continue
        if r is None:
            PDF["fallos"] += 1
            raise TextoNoDisponible("el anuncio no existe (404)")
        if r.content[:5] == b"%PDF-":
            PDF["seguidos"] = 0
            try:
                lector = PdfReader(io.BytesIO(r.content))
                return "\n".join((pg.extract_text() or "") for pg in lector.pages)
            except Exception as e:
                PDF["fallos"] += 1
                raise TextoNoDisponible(f"PDF ilegible: {e}")
        inicio = " ".join(re.sub(r"<[^>]+>", " ", r.text[:1500]).split())[:200]
        ultimo = (f"no es un PDF (HTTP {r.status_code}, {r.headers.get('content-type')}, "
                  f"url final {r.url}): «{inicio}»")
    PDF["fallos"] += 1
    PDF["seguidos"] += 1
    if _diag["n"] < 3:  # solo los 3 primeros, para no llenar el registro
        _diag["n"] += 1
        log(f"   ⚠ {url} -> {ultimo}")
    raise TextoNoDisponible(ultimo)


def leer_rss(url):
    r = descargar(url)
    if r is None:
        raise RuntimeError(f"El feed no existe (404): {url}")
    root = ET.fromstring(r.content)  # ElementTree respeta la codificación declarada
    for it in root.iter("item"):
        cats = [" ".join(re.sub(r"<br\s*/?>", " · ", c.text or "").split())
                for c in it.findall("category")]
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
        partes = [p.strip() for p in it["descripcion"].split(" - ")]
        partes = [p for p in partes if p and not re.match(
            r"(publicado|referencia|kbytes|paginas|páginas)\b|[IVX]+\.|[A-Z]\.\s", p, re.I)]
        yield {
            "id": m.group(0), "fuente": fuente["nombre"], "titulo": it["titulo"],
            "organismo": partes[0] if partes else "", "seccion": "", "fecha": it["fecha"],
            "url": f"https://www.boe.es/diario_boe/txt.php?id={m.group(0)}",
            "pdf": "", "modo": fuente["municipio_en"], "lector": "boe",
            "exigir_tipo": fuente.get("exigir_tipo", True),
            "tipo_en": fuente.get("tipo_en", "titulo"),
        }


RE_SECCION = re.compile(r"^\s*[IVX]+\d*\s*\.")
RE_DIA = re.compile(r"^(lunes|martes|miercoles|jueves|viernes|sabado|domingo)\b")


def clasificar_categorias(cats):
    """El boletín del día trae [sección, organismo]; los canales por organismo
    traen [fecha, organismo, sección]. Esto sirve para los dos."""
    seccion = organismo = ""
    for c in cats:
        if RE_SECCION.match(c):
            seccion = seccion or c
        elif RE_DIA.match(" ".join(normalizar(c).split())):
            continue
        else:
            organismo = organismo or c
    return seccion, organismo


def seccion_borm(seccion):
    m = re.match(r"\s*([IVX]+)", seccion)
    romano = m.group(1) if m else ""
    if "particular" in seccion.lower():
        return "particulares"
    return {"I": "comunidad_autonoma", "IV": "administracion_local",
            "V": "particulares"}.get(romano)


def candidatos_borm(fuente):
    conf = fuente.get("secciones", {})
    urls = fuente.get("urls") or [fuente["url"]]
    ok = 0
    for url in urls:
        try:
            items = list(leer_rss(url))
        except Exception as e:     # un canal caído no impide leer los demás
            errores.append(f"Canal BORM {url}: {e}")
            continue
        ok += 1
        for it in items:
            seccion, organismo = clasificar_categorias(it["categorias"])
            organismo = organismo or it["descripcion"]
            tipo = seccion_borm(seccion)
            if not tipo or not conf.get(tipo, {}).get("activa", False):
                continue
            apartados = conf[tipo].get("solo_apartados")
            if apartados and not any(seccion.strip().upper().startswith(str(a).upper())
                                     for a in apartados):
                continue
            m = re.search(r"/anuncio/(\d+)", it["url"])
            yield {
                "id": f"BORM-{m.group(1) if m else it['url']}", "fuente": fuente["nombre"],
                "titulo": it["titulo"], "organismo": organismo, "seccion": seccion,
                "fecha": it["fecha"], "url": it["url"], "pdf": it["url"],
                "modo": conf[tipo]["municipio_en"], "lector": "pdf",
                "exigir_tipo": conf[tipo].get("exigir_tipo", fuente.get("exigir_tipo", True)),
                "tipo_en": conf[tipo].get("tipo_en", fuente.get("tipo_en", "titulo")),
            }
    if ok == 0:
        raise RuntimeError("ningún canal del BORM respondió")


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


def candidatos_pendientes(estado):
    """Anuncios cuyo texto no se pudo leer en ejecuciones anteriores."""
    for id_, c in list(estado.get("pendientes", {}).items()):
        c = dict(c)
        c["fecha"] = dt.date.fromisoformat(c["fecha"])
        if (HOY - c["fecha"]).days > DIAS_REINTENTO:
            c["lector"] = "titulo"       # último recurso: mirar solo el título
        yield c


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
                "exigir_tipo": rel.get("exigir_tipo", False),
                "tipo_en": rel.get("tipo_en", "titulo"),
            }
        log(f"   API BOE {clave}: {n} anuncios en {', '.join(sorted(secciones))}")
        if dia < HOY:  # el de hoy se vuelve a mirar por si aún no estaba completo
            hechos.add(clave)
    limite = (HOY - dt.timedelta(days=cfg["dias"] + 5)).isoformat()
    estado["dias_boe"] = sorted(h for h in hechos if h >= limite)


# -------------------------------------------------------------- análisis ---

def analizar(c, buscador):
    """Devuelve el resultado si el anuncio cumple las tres condiciones, si no None.
    Primero se mira lo que es gratis (título, organismo) y solo después se
    descarga el texto completo."""
    titulo = c["titulo"]
    if buscador.excluido(titulo):
        return None
    exigir_tipo = c.get("exigir_tipo", True)
    tipo_en = c.get("tipo_en", "titulo")
    tipo = buscador.tipo(titulo)
    if exigir_tipo and tipo_en == "titulo" and not tipo:
        return None
    if c["modo"] in ("titulo", "organismo"):
        if buscador.municipio(c["modo"], titulo, c["organismo"]) is None:
            return None
    if c["lector"] == "boe":
        texto, org = texto_boe(c["id"])
        c["organismo"] = c["organismo"] or org
    elif c["lector"] == "titulo":   # texto imposible de leer tras varios días
        texto = ""
    else:
        texto = texto_pdf(c["pdf"])
    if not tipo:
        tipo = buscador.tipo(texto)
        if exigir_tipo and not tipo:
            return None
    hit = buscador.perfil(texto) or buscador.perfil(titulo)
    if not hit:
        return None
    fragmento, perfil = hit
    mun = buscador.municipio(c["modo"], titulo, c["organismo"], texto)
    if mun is None:
        return None
    promocion = bool(re.search(r"promocion\s+interna|turno\s+de\s+promocion|promocion\s+cruzada",
                               normalizar(texto)))
    return {
        "promocion": promocion, "tipo": tipo or "", "perfil": perfil,
        "id": c["id"], "fuente": c["fuente"], "titulo": titulo,
        "organismo": c["organismo"], "seccion": c["seccion"],
        "fecha": c["fecha"].isoformat(), "url": c["url"], "municipio": mun,
        "fragmento": (("(No se pudo leer el texto del anuncio; coincidencia solo en el título) "
                       if c["lector"] == "titulo" else "") + fragmento)[:520],
        "detectado": HOY.isoformat(),
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
            f" · zona: {r['municipio'] or '—'} · perfil: {r.get('perfil') or '—'}"
            f"{' · tipo: ' + r['tipo'] if r.get('tipo') else ''}"
            f"{' · ⚠️ promoción interna' if r.get('promocion') else ''}\n\n> {r['fragmento']}\n\n"
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


def generar_pagina(resultados, cfg, estado=None):
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
    <p class="tags">{'<span>perfil: ' + e(r['perfil']) + '</span>' if r.get('perfil') else ''}{'<span>tipo: ' + e(r['tipo']) + '</span>' if r.get('tipo') else ''}</p>
    <p class="frag">{e(r['fragmento'])}</p>
  </article>""")
    ult = (estado or {}).get("ultima", {})
    estado_txt = ""
    if ult:
        estado_txt = (f" · última revisión: {e(ult['hora'])} "
                      f"({ult['revisados']} anuncios nuevos analizados)")
        if ult.get("pendientes"):
            estado_txt += (f'<br><span class="aviso">⚠ {ult["pendientes"]} anuncio(s) del BORM '
                           "sin poder leer todavía; se reintentan en cada revisión.</span>")
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
 .aviso{{color:var(--acc);font-weight:600}}
 .tags{{margin:0 0 6px;display:flex;gap:6px;flex-wrap:wrap}} .tags span{{font-size:.75rem;border:1px solid var(--line);border-radius:999px;padding:0 8px;color:var(--mut)}}
 .vacio{{color:var(--mut)}} footer{{max-width:820px;margin:auto;padding:0 16px 30px;font-size:.8rem;color:var(--mut)}}
</style></head><body>
<header><h1>Vigía de empleo público</h1>
<p>{len(lista)} anuncio(s) en los últimos {cfg['dias']} días{estado_txt}</p></header>
<main>{''.join(tarjetas) or vacio}</main>
<footer>Búsqueda: vía de entrada ({len(cfg.get('tipos_de_convocatoria') or [])} términos) + perfil ({len(cfg.get('perfiles') or cfg.get('palabras_clave') or [])} términos) + zona ({len(cfg['municipios'])} municipios y {len(cfg.get('organismos_extra') or [])} organismos). En el BOE, el plazo de solicitudes suele contar desde el día siguiente a la publicación.</footer>
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
    ids_previos = {r["id"] for r in resultados}
    if estado.get("filtro") != FILTRO_VERSION:
        if vistos or resultados:
            log("Filtro actualizado: se vuelve a analizar todo (solo se avisa de lo nuevo).")
            vistos, resultados, estado["dias_boe"] = {}, [], []
            reanalisis = True
        estado["filtro"] = FILTRO_VERSION
    ids_resultado = {r["id"] for r in resultados}
    fuentes_caidas = 0
    pend = estado.setdefault("pendientes", {})
    total_rev = 0
    intentados = set()                      # ids ya probados en esta ejecución
    desde = HOY - dt.timedelta(days=cfg["dias"])

    def generadores():
        if pend:
            yield f"Pendientes de ejecuciones anteriores ({len(pend)})", candidatos_pendientes(estado)
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
                if c["id"] in vistos or c["id"] in intentados or c["fecha"] < desde:
                    continue
                intentados.add(c["id"])
                try:
                    res = analizar(c, buscador)
                except TextoNoDisponible as ex:
                    previo = pend.get(c["id"], {})
                    pend[c["id"]] = {**{k: v for k, v in c.items() if k != "fecha"},
                                     "fecha": c["fecha"].isoformat(),
                                     "lector": "pdf" if c["lector"] == "titulo" else c["lector"],
                                     "intentos": previo.get("intentos", 0) + 1}
                    errores.append(f"{c['id']}: texto no disponible (se reintentará): {str(ex)[:120]}")
                    continue
                except Exception as ex:  # un anuncio roto no para el resto
                    errores.append(f"{c['id']}: {ex}")
                    continue
                pend.pop(c["id"], None)
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
        total_rev += n_rev
        log(f"   {n_rev} anuncios nuevos revisados")

    # Limpieza: olvidar lo visto hace más de 90 días
    limite = (HOY - dt.timedelta(days=90)).isoformat()
    vistos = {k: v for k, v in vistos.items() if v >= limite}
    resultados = [r for r in resultados if r["fecha"] >= limite]

    for k in [k for k, c in pend.items() if k in vistos]:
        pend.pop(k)
    guardar(F_VISTOS, vistos)
    guardar(F_RESULTADOS, resultados)
    try:
        from zoneinfo import ZoneInfo
        ahora = dt.datetime.now(ZoneInfo("Europe/Madrid"))
    except Exception:
        ahora = dt.datetime.now()
    estado["ultima"] = {"hora": ahora.strftime("%d/%m/%Y %H:%M"), "revisados": total_rev,
                        "pendientes": len(pend)}
    guardar(F_ESTADO, estado)
    generar_pagina(resultados, cfg, estado)
    if reanalisis:   # no repetir avisos de lo que ya conocías
        nuevos = [r for r in nuevos if r["id"] not in ids_previos]
    if not sin_avisos:
        avisar(nuevos, cfg)

    log(f"\nNovedades: {len(nuevos)}")
    if errores:
        log("Avisos/errores:\n  - " + "\n  - ".join(errores[:50]))
    if pend:
        log(f"\n⚠ {len(pend)} anuncio(s) con texto ilegible por ahora; se reintentarán "
            f"durante {DIAS_REINTENTO} días.")
    bloqueo = PDF["fallos"] >= 3 and PDF["fallos"] * 2 >= PDF["intentos"]
    if bloqueo:
        log(f"⚠ El BORM no ha devuelto el texto de {PDF['fallos']} de {PDF['intentos']} anuncios: "
            "posible bloqueo temporal del servidor.")
    if fuentes_caidas or bloqueo:
        sys.exit(1)  # GitHub te avisará por correo de que algo ha fallado


if __name__ == "__main__":
    main()
