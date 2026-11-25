#!/usr/bin/env python3
"""
procesar_gpx.py
----------------
Persona A - LAB_SIG (Mapeo de parque comunal)

Lee todos los archivos GPX exportados por OSMTracker que estan dentro de
data/raw/<captura>/*.gpx, junto con sus fotos/audios enlazados, y genera:

  1. data/parque.geojson      -> GeoJSON con todos los elementos
  2. docs/parque.geojson       -> copia para que la pagina (Leaflet) la lea
  3. docs/media/<archivo>      -> copia de cada foto/audio referenciado

Como funciona la lectura del GPX:
- OSMTracker graba un <trk> de fondo (la caminata completa). Ese track NO
  es la forma del elemento, es solo ruido de posicion; se ignora.
- Cada <wpt> es un punto dejado a mano (foto, audio, nota de texto o un
  boton de la disposicion "mapeo_parque").
- Los <wpt> de foto/audio se reconocen por el <link href="...">; se
  asocian al elemento mas cercano en el tiempo.
- Los <wpt> cuyo <name> coincide (sin tildes/mayusculas) con uno de los
  botones de layouts/mapeo_parque se agrupan en orden de aparicion: si
  hay 1 -> Point, 2 -> LineString, 3+ -> Polygon (se cierra el anillo).
  Esto es lo que pasara en campo real: para una linea o un poligono hay
  que presionar el mismo boton una vez por vertice.
- Cualquier <wpt> que no calce con un boton conocido se guarda igual,
  como una nota de texto en un Point (por ejemplo la estatua, que no
  tiene boton segun el Parte C).

Uso:
    python3 procesar_gpx.py [--raw data/raw] [--out-data data/parque.geojson]
                            [--out-docs docs/parque.geojson] [--media docs/media]
"""

import argparse
import json
import shutil
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}

# Categorias segun layouts/mapeo_parque/es.xml y el readme del repo.
# "aliases" cubre tanto la etiqueta exacta del boton como variantes en
# minuscula / texto libre que aparecieron en las pruebas de campo.
CATEGORIES = {
    "banca": {
        "aliases": ["banca", "banco"],
        "osm_tag": "amenity=bench",
    },
    "basurero": {
        "aliases": ["basurero"],
        "osm_tag": "amenity=waste_basket",
    },
    "poste_de_luz": {
        "aliases": ["poste de luz", "poste"],
        "osm_tag": "highway=street_lamp",
    },
    "arbol": {
        "aliases": ["arbol"],
        "osm_tag": "natural=tree",
    },
    "camara": {
        "aliases": ["camara"],
        "osm_tag": "man_made=surveillance",
    },
    "hilera_arboles": {
        "aliases": ["hilera arboles", "hilera de arboles"],
        "osm_tag": "natural=tree_row",
    },
    "sendero": {
        "aliases": ["sendero"],
        "osm_tag": "highway=footway",
    },
    "cancha": {
        "aliases": ["cancha"],
        "osm_tag": "leisure=pitch",
    },
    "skate_park": {
        "aliases": ["skate park", "skatepark"],
        "osm_tag": "leisure=pitch;sport=skateboard",
    },
    "parque_infantil": {
        "aliases": ["parque infantil", "parque"],
        "osm_tag": "leisure=playground",
    },
}

IMAGE_EXT = {".jpg", ".jpeg", ".png"}
AUDIO_EXT = {".3gp", ".amr", ".m4a", ".mp3", ".wav", ".ogg"}


def normalize(text):
    """minuscula, sin tildes, espacios simples -> para comparar nombres."""
    if text is None:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.lower().split())


# alias normalizado -> categoria
ALIAS_LOOKUP = {}
for cat, info in CATEGORIES.items():
    for alias in info["aliases"]:
        ALIAS_LOOKUP[normalize(alias)] = cat


def parse_gpx(path):
    """Devuelve (metadata_name, lista_de_wpts, tracks) de un archivo GPX.

    cada wpt: dict(lat, lon, ele, time, name, link_href)
    """
    root = ET.parse(path).getroot()
    meta_name_el = root.find("g:metadata/g:name", GPX_NS)
    meta_name = meta_name_el.text if meta_name_el is not None else path.stem

    wpts = []
    for w in root.findall("g:wpt", GPX_NS):
        name_el = w.find("g:name", GPX_NS)
        time_el = w.find("g:time", GPX_NS)
        link_el = w.find("g:link", GPX_NS)
        wpts.append({
            "lat": float(w.get("lat")),
            "lon": float(w.get("lon")),
            "time": time_el.text if time_el is not None else None,
            "name": name_el.text.strip() if name_el is not None and name_el.text else "",
            "link_href": link_el.get("href") if link_el is not None else None,
        })

    return meta_name, wpts


def split_media_and_tags(wpts):
    """Separa los wpt de foto/audio (tienen <link>) del resto."""
    media = []
    others = []
    for w in wpts:
        if w["link_href"]:
            ext = Path(w["link_href"]).suffix.lower()
            kind = "photo" if ext in IMAGE_EXT else "audio" if ext in AUDIO_EXT else "file"
            media.append({**w, "kind": kind})
        else:
            others.append(w)
    return media, others


def nearest_media(target_time, media, kind):
    """Devuelve el nombre de archivo del media de ese tipo mas cercano en tiempo."""
    candidates = [m for m in media if m["kind"] == kind and m["time"]]
    if not candidates:
        return None
    if target_time is None:
        return candidates[0]["link_href"]
    best = min(candidates, key=lambda m: abs(_to_epoch(m["time"]) - _to_epoch(target_time)))
    return best["link_href"]


def _to_epoch(iso_time):
    from datetime import datetime
    return datetime.fromisoformat(iso_time.replace("Z", "+00:00")).timestamp()


def group_tag_waypoints(others):
    """Agrupa wpts consecutivos con la MISMA categoria conocida en un solo
    elemento (1 punto=Point, 2=LineString, 3+=Polygon). Los wpts que no
    calzan con ninguna categoria se devuelven como notas sueltas (Point).
    """
    groups = []  # lista de dict(category|None, points=[wpt,...], note_text)
    current_cat = None
    current_points = []

    def flush():
        nonlocal current_cat, current_points
        if current_points:
            groups.append({"category": current_cat, "points": current_points})
        current_cat, current_points = None, []

    for w in others:
        cat = ALIAS_LOOKUP.get(normalize(w["name"]))
        if cat is None:
            # nota libre: siempre su propio elemento, no se agrupa
            flush()
            groups.append({"category": None, "points": [w], "note_text": w["name"]})
            continue
        if cat != current_cat:
            flush()
            current_cat = cat
        current_points.append(w)
    flush()
    return groups


def group_to_geometry(points):
    coords = [[p["lon"], p["lat"]] for p in points]
    if len(coords) == 1:
        return {"type": "Point", "coordinates": coords[0]}
    if len(coords) == 2:
        return {"type": "LineString", "coordinates": coords}
    ring = coords[:]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def build_features(gpx_path, author="A"):
    meta_name, wpts = parse_gpx(gpx_path)
    media, others = split_media_and_tags(wpts)
    groups = group_tag_waypoints(others)

    features = []
    for g in groups:
        points = g["points"]
        first_time = points[0]["time"]
        category = g.get("category")
        note_text = g.get("note_text")

        if category is not None:
            props = {
                "category": category,
                "osm_tag": CATEGORIES[category]["osm_tag"],
                "timestamp": first_time,
                "author": author,
                "photo": nearest_media(first_time, media, "photo"),
                "audio": nearest_media(first_time, media, "audio"),
                "note": None,
                "source_file": gpx_path.name,
            }
        else:
            props = {
                "category": "nota",
                "osm_tag": None,
                "timestamp": first_time,
                "author": author,
                "photo": nearest_media(first_time, media, "photo"),
                "audio": nearest_media(first_time, media, "audio"),
                "note": note_text,
                "source_file": gpx_path.name,
            }

        features.append({
            "type": "Feature",
            "properties": props,
            "geometry": group_to_geometry(points),
        })

    return features, media


def process(raw_dir, out_data, out_docs, media_dir):
    raw_dir = Path(raw_dir)
    media_dir = Path(media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)

    all_features = []
    copied = 0

    gpx_files = sorted(raw_dir.rglob("*.gpx"))
    for gpx_path in gpx_files:
        features, media = build_features(gpx_path)
        all_features.extend(features)

        # copiar los archivos de media enlazados junto a su gpx
        for m in media:
            src = gpx_path.parent / m["link_href"]
            if src.exists():
                dest = media_dir / src.name
                shutil.copyfile(src, dest)
                copied += 1

    fc = {"type": "FeatureCollection", "features": all_features}

    for out_path in (Path(out_data), Path(out_docs)):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(fc, f, ensure_ascii=False, indent=2)

    print(f"GPX procesados: {len(gpx_files)}")
    print(f"Elementos generados: {len(all_features)}")
    print(f"Archivos de media copiados: {copied}")
    print(f"Escrito: {out_data} y {out_docs}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", default="data/raw")
    parser.add_argument("--out-data", default="data/parque.geojson")
    parser.add_argument("--out-docs", default="docs/parque.geojson")
    parser.add_argument("--media", default="docs/media")
    args = parser.parse_args()

    process(args.raw, args.out_data, args.out_docs, args.media)


if __name__ == "__main__":
    main()
