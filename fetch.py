import os
import subprocess
import tempfile
import time
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


def parse_xml(xml_path, db_path, progress=None):
    entities, countries, programs, lists, s_types = [], [], [], [], []
    data_as_of = ""
    seen_countries = set()
    if progress is not None:
        progress["phase"] = "parsing"

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

        if progress is not None:
            progress["entities"] += 1

        el.clear()

    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        for table in ("entities", "countries", "programs", "lists", "sanctions_types"):
            conn.execute(f"DELETE FROM {table}")
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
    finally:
        conn.close()

    return {
        "entities": len(entities),
        "countries": {c for _, c in countries},
        "programs": {p for _, p in programs},
        "lists": {l for _, l, _ in lists},
        "data_as_of": data_as_of,
    }


def _content_length(url):
    try:
        proc = subprocess.run(
            ["curl", "-sIL", "-o", "/dev/null", "-D", "-", url],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    length = None
    for line in proc.stdout.splitlines():
        if line.lower().startswith("content-length:"):
            try:
                length = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return length


def download(url, dest, progress=None):
    if progress is not None:
        progress["total_bytes"] = _content_length(url)
    proc = subprocess.Popen(["curl", "-sL", "--max-time", "600", "-o", dest, url])
    while proc.poll() is None:
        if progress is not None:
            try:
                progress["bytes"] = os.path.getsize(dest)
            except OSError:
                pass
        time.sleep(0.2)
    if proc.returncode != 0:
        raise RuntimeError("OFAC download failed")
    if progress is not None:
        progress["bytes"] = os.path.getsize(dest)
    return dest


def load_data(db_path, progress=None):
    tmp = tempfile.NamedTemporaryFile(suffix=".xml", delete=False)
    tmp.close()
    try:
        if progress is not None:
            progress.update(phase="downloading", bytes=0, total_bytes=None, entities=0)
        download(OFAC_ENTITIES_URL, tmp.name, progress)
        result = parse_xml(tmp.name, db_path, progress)
        if progress is not None:
            progress["phase"] = "done"
        return result
    finally:
        os.unlink(tmp.name)