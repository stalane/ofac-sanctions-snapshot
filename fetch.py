import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET

import db

OFAC_ENTITIES_URL = "https://sanctionslistservice.ofac.treas.gov/entities"


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _children(el, name):
    for child in el:
        if _local(child.tag) == name:
            yield child


def _first_text(el, *path):
    cur = el
    for name in path:
        found = None
        for child in _children(cur, name):
            found = child
            break
        if found is None:
            return None
        cur = found
    return (cur.text or "").strip() or None


def _extract_primary_name(el):
    fallback = None
    for names in _children(el, "names"):
        for name in _children(names, "name"):
            for translations in _children(name, "translations"):
                for tr in _children(translations, "translation"):
                    ffn = _first_text(tr, "formattedFullName")
                    if not ffn:
                        continue
                    if fallback is None:
                        fallback = ffn
                    if _first_text(tr, "isPrimary") == "true":
                        return ffn
    return fallback


def parse_xml(xml_path, db_path):
    entities, countries, programs, lists, s_types = [], [], [], [], []
    data_as_of = ""
    seen_countries = set()

    for event, el in ET.iterparse(xml_path, events=("end",)):
        name = _local(el.tag)
        if name == "dataAsOf":
            data_as_of = (el.text or "").strip()
            continue
        if name != "entity":
            continue

        eid = int(el.get("id"))
        identity_id = _first_text(el, "generalInfo", "identityId")
        entity_type = _first_text(el, "generalInfo", "entityType")
        full_name = _extract_primary_name(el)
        entities.append((eid, identity_id, entity_type, full_name))

        for addresses in _children(el, "addresses"):
            for address in _children(addresses, "address"):
                c = _first_text(address, "country")
                if c and (eid, c) not in seen_countries:
                    seen_countries.add((eid, c))
                    countries.append((eid, c))

        for programs_el in _children(el, "sanctionsPrograms"):
            for prog in _children(programs_el, "sanctionsProgram"):
                programs.append((eid, (prog.text or "").strip()))

        for lists_el in _children(el, "sanctionsLists"):
            for lst in _children(lists_el, "sanctionsList"):
                lists.append((eid, (lst.text or "").strip(), lst.get("datePublished")))

        for types_el in _children(el, "sanctionsTypes"):
            for s_type in _children(types_el, "sanctionsType"):
                s_types.append((eid, (s_type.text or "").strip()))

        el.clear()

    conn = db.connect(db_path)
    db.init_db(conn)
    for table, rows in (
        ("entities", entities),
        ("countries", countries),
        ("programs", programs),
        ("lists", lists),
        ("sanctions_types", s_types),
    ):
        db.load_rows(conn, table, rows)
    if data_as_of:
        db.set_meta(conn, "data_as_of", data_as_of)
    conn.commit()
    conn.close()

    return {
        "entities": len(entities),
        "countries": {c for _, c in countries},
        "programs": {p for _, p in programs},
        "lists": {l for _, l, _ in lists},
        "data_as_of": data_as_of,
    }


def download(url, dest):
    try:
        subprocess.run(
            ["curl", "-sL", "--max-time", "600", "-o", dest, url],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("OFAC download failed") from exc
    return dest


def load_data(db_path):
    tmp = tempfile.NamedTemporaryFile(suffix=".xml", delete=False)
    tmp.close()
    try:
        download(OFAC_ENTITIES_URL, tmp.name)
        return parse_xml(tmp.name, db_path)
    finally:
        os.unlink(tmp.name)