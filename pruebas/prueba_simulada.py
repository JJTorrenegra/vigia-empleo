"""Prueba sin internet: simula BOE, BORM y la API con la estructura real observada."""
import io, json, shutil, sys, tempfile
from pathlib import Path
from reportlab.pdfgen import canvas

ORIG = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
for f in ["vigia.py", "config.yaml"]:
    shutil.copy(ORIG / f, TMP / f)
sys.path.insert(0, str(TMP))
import vigia  # noqa

HOY = vigia.HOY
RFC = HOY.strftime("%a, %d %b %Y 00:00:00 +0100")


def pdf(texto):
    b = io.BytesIO(); c = canvas.Canvas(b)
    y = 800
    for linea in texto.split("\n"):
        c.drawString(40, y, linea); y -= 16
    c.save(); return b.getvalue()


def rss(items, enc="utf-8"):
    cuerpo = "".join(items)
    return f'<?xml version="1.0" encoding="{enc}"?><rss><channel><title>t</title>{cuerpo}</channel></rss>'.encode(enc)


def boe_item(id_, titulo):
    return (f"<item><title>{titulo}</title><link>https://www.boe.es/diario_boe/txt.php?id={id_}</link>"
            f"<description>Administración Local - Publicado el x - Referencia: {id_}</description>"
            f"<pubDate>{RFC}</pubDate></item>")


def borm_item(n, titulo, seccion, org):
    return (f"<item><title>{titulo}</title><link>https://www.borm.es/services/anuncio/{n}/pdf</link>"
            f"<category>{seccion}</category><category>{org}</category><description>{org}</description>"
            f"<pubDate>{RFC}</pubDate></item>")


T = "Resolución de 19 de octubre de 2026, del Ayuntamiento de {} referente a la convocatoria para proveer varias plazas."
RESP = {
    "c=140": rss([
        "<item><title>Sumario</title><link>https://www.boe.es/boe/dias/x/</link><description>Referencia: BOE-S-2026-266</description></item>",
        boe_item("BOE-A-2026-1", T.format("Torrevieja (Alicante/Alacant),")),       # SÍ
        boe_item("BOE-A-2026-2", T.format("Valladolid,")),                            # no (título)
        boe_item("BOE-A-2026-3", T.format("Alhama de Murcia (Murcia),")),             # no: no es Murcia
        boe_item("BOE-A-2026-4", T.format("San Javier (Murcia),")),                   # no: sin administrativo
    ], enc="iso-8859-1"),
    "c=141": rss([boe_item("BOE-A-2026-1", T.format("Torrevieja (Alicante/Alacant),"))]),  # duplicado
    "s=3": rss([boe_item("BOE-A-2026-5", "Resolución por la que se publica el Convenio con el Ayuntamiento de Cartagena."),
                boe_item("BOE-A-2026-6", "Resolución sobre un convenio en Zamora.")]),
    "s=5B": rss([]),
    "borm.es/rss/boletin.xml": rss([
        borm_item(101, "5249 Lista de admitidos", "I2. Autoridades y Personal", "Consejería de Economía, Hacienda"),  # SÍ
        borm_item(102, "5272 Padrón de agua", "IV. Administración Local", "Alhama de Murcia"),                    # no
        borm_item(103, "5273 Bolsa de empleo", "IV. Administración Local", "San Javier"),                          # SÍ
        borm_item(104, "5274 Licencia de obras", "IV. Administración Local", "Cartagena"),                        # no
        borm_item(105, "5275 Edicto", "III. Administración de Justicia", "Juzgado de Murcia"),                    # ignorado
        borm_item(106, "5276 Anuncio de empresa", "V. Otras Disposiciones y Anuncios", "Particulares"),           # SÍ
    ]),
}
TEXTOS_BOE = {
    "BOE-A-2026-1": "Una plaza de Auxiliar Administrativo/a de la plantilla de personal laboral fijo.",
    "BOE-A-2026-3": "Una plaza de Administrativo.",
    "BOE-A-2026-4": "Una plaza de Policía Local.",
    "BOE-A-2026-5": "El Ayuntamiento de Cartagena contratará personal administrativo para el proyecto.",
    "BOE-A-2026-6": "Personal administrativo en Zamora.",
    "BOE-A-2026-9": "Dos plazas de Administrativa de Administración General.",
}
PDFS = {
    "101": "Relacion de admitidos al Cuerpo Administrativo, opcion General.",
    "102": "Padron de agua del tercer trimestre. Personal administrativo de caja.",
    "103": "Bolsa de trabajo de Administrativo/a para el Ayuntamiento de San Javier.",
    "104": "Licencia de obras en Cartagena.",
    "106": "Empresa de Torre Pacheco busca auxiliar administrativa para proyecto temporal.",
}
AYER = HOY - vigia.dt.timedelta(days=1 if HOY.weekday() != 0 else 2)
SUMARIO = {"status": {"code": "200"}, "data": {"sumario": {"diario": [{"seccion": [
    {"codigo": "2B", "nombre": "Oposiciones y concursos", "departamento": [
        {"codigo": "8010", "nombre": "ADMINISTRACIÓN LOCAL", "epigrafe": [{"nombre": "Personal", "item": [
            {"identificador": "BOE-A-2026-9", "titulo": T.format("Orihuela (Alicante/Alacant),")},
            {"identificador": "BOE-A-2026-1", "titulo": T.format("Torrevieja (Alicante/Alacant),")}]}]}]},
    {"codigo": "3", "nombre": "Otras", "departamento": {"nombre": "MINISTERIO", "item":
        {"identificador": "BOE-A-2026-77", "titulo": "Ayuntamiento de Murcia algo"}}}]}]}}}


class R:
    def __init__(self, content=b"", status=200):
        self.content, self.status_code = content, status
        self.text = content.decode("utf-8", "ignore")
    def json(self): return json.loads(self.content)


def falso(url, accept=None, intentos=3):
    for k, v in RESP.items():
        if k in url:
            return R(v)
    if "xml.php?id=" in url:
        i = url.split("id=")[1]
        t = TEXTOS_BOE.get(i)
        if t is None:
            return None
        return R(f"<documento><metadatos><departamento>ADMINISTRACIÓN LOCAL</departamento></metadatos><texto><p>{t}</p></texto></documento>".encode())
    if "/anuncio/" in url:
        return R(pdf(PDFS[url.split("/anuncio/")[1].split("/")[0]]))
    if "sumario/" in url:
        return R(json.dumps(SUMARIO).encode()) if url.endswith(AYER.strftime("%Y%m%d")) else None
    raise AssertionError("URL inesperada " + url)


vigia.descargar = falso
sys.argv = ["vigia.py", "--sin-avisos"]
vigia.main()
res = json.loads((TMP / "datos/resultados.json").read_text())
ids = sorted(r["id"] for r in res)
print("\nENCONTRADOS:", ids)
esperado = sorted(["BOE-A-2026-1", "BOE-A-2026-5", "BOE-A-2026-9", "BORM-101", "BORM-103", "BORM-106"])
assert ids == esperado, f"esperado {esperado}"
for r in res:
    print(f" - {r['id']:14} {r['municipio'] or '(sin municipio)':22} {r['fragmento'][:70]}")

# Segunda ejecución: no debe haber novedades ni descargas nuevas
vigia.errores.clear()
contador = {"n": 0}
def contar(url, accept=None, intentos=3):
    if "rss" not in url and "sumario" not in url:
        contador["n"] += 1
    return falso(url, accept, intentos)
vigia.descargar = contar
vigia.main()
assert contador["n"] == 0, f"descargó {contador['n']} textos de nuevo"
print("\nOK: segunda ejecución sin repetir descargas")
shutil.copy(TMP / "docs/index.html", ORIG / "pruebas/ejemplo_pagina.html")
