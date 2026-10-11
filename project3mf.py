"""Write a single-object QIDI Studio project (.3mf).

The object is made of parts (normal and negative volumes) and can carry
per-layer custom G-code, which QIDI Studio shows as markers on the layer
slider. The chosen presets can be embedded so the project opens with them.
"""
import datetime
import json
import os
import uuid
import zipfile
from xml.sax.saxutils import escape, quoteattr

import qidi_profiles as qp


def project_config(store, machine, process, filament):
    """The merged settings QIDI Studio stores in Metadata/project_settings.config.

    Values come straight from the resolved presets so QIDI Studio recognises and
    selects the same presets on load; keys the presets don't define fall back to
    QIDI Studio's own defaults, exactly as they do for the presets themselves.
    """
    m = store.resolve("machine", machine)
    p = store.resolve("process", process)
    f = store.resolve("filament", filament)
    cfg = {}
    for src in (p, m, f):
        for k, v in src.items():
            if k in qp.META_KEYS or k.startswith("compatible_"):
                continue
            cfg[k] = v

    def inherits(kind, name):
        pr = store.get(kind, name)
        return "" if pr.system else (pr.data.get("inherits") or "")

    def diff_to_parent(kind, name):
        parent = inherits(kind, name)
        if not parent or not store.get(kind, parent):
            return ""
        mine, base = store.resolve(kind, name), store.resolve(kind, parent)
        keys = [k for k in set(mine) | set(base)
                if k not in qp.META_KEYS and mine.get(k) != base.get(k)]
        return ";".join(sorted(keys))

    colour = qp.first(f.get("default_filament_colour"), "") or "#F2754E"
    bed_types = store.app_config.get("user_bed_type_list", {}) if isinstance(store.app_config, dict) else {}
    cfg.update({
        "name": "project_settings",
        "from": "project",
        "version": store.app_version(),
        "print_settings_id": process,
        "printer_settings_id": machine,
        "filament_settings_id": [filament],
        "print_compatible_printers": p.get("compatible_printers") or [],
        "inherits_group": [inherits("process", process), inherits("filament", filament),
                           inherits("machine", machine)],
        "different_settings_to_system": [diff_to_parent("process", process),
                                         diff_to_parent("filament", filament),
                                         diff_to_parent("machine", machine)],
        "filament_ids": [f.get("filament_id", "")],
        "filament_colour": [colour],
        "filament_multi_colour": [colour],
        "filament_colour_type": ["1"],
        "filament_self_index": ["1"],
        "filament_map": ["1"],
        "flush_volumes_matrix": ["0"],
        "flush_volumes_vector": ["140", "140"],
    })
    if machine in bed_types:
        cfg["curr_bed_type"] = bed_types[machine]
    return cfg


class Printer:
    """The bits of the chosen presets that shape where and how the object prints."""

    def __init__(self, store=None, machine=None, process=None):
        m = store.resolve("machine", machine) if store and machine else {}
        p = store.resolve("process", process) if store and process else {}
        x0, y0, x1, y1 = qp.bed_shape(m)
        self.center = ((x0 + x1) / 2, (y0 + y1) / 2)
        self.max_height = qp.as_float(m.get("printable_height"), 0)
        self.first_layer = qp.as_float(p.get("initial_layer_print_height"), 0.2)
        self.layer = qp.as_float(p.get("layer_height"), 0.2)

    def check_height(self, height, what="Tower"):
        if self.max_height and height > self.max_height:
            raise ValueError(f"{what} is {height:g} mm tall but the printer only allows {self.max_height:g} mm")


def layer_print_z(first_h, layer_h, boundary):
    """print_z of the first layer sliced above `boundary`.

    Layers are sliced through their middle, so a layer whose middle lands
    exactly on the boundary still prints the section below (e.g. 0.16 mm
    layers at z 11.0); it has to be strictly above.
    """
    if boundary <= 0:
        return round(first_h, 4)
    i = 1
    while True:
        z = first_h + i * layer_h
        if z - layer_h / 2 > boundary + 1e-4:
            return round(z, 4)
        i += 1


def _mesh_xml(obj_id, mesh, uid):
    out = ['  <object id="%d" p:UUID="%s" type="model">\n   <mesh>\n    <vertices>\n' % (obj_id, uid)]
    out.extend('     <vertex x="%.6g" y="%.6g" z="%.6g"/>\n' % v for v in mesh.verts)
    out.append('    </vertices>\n    <triangles>\n')
    out.extend('     <triangle v1="%d" v2="%d" v3="%d"/>\n' % t for t in mesh.tris)
    out.append('    </triangles>\n   </mesh>\n  </object>\n')
    return "".join(out)


MODEL_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
              '<model unit="millimeter" xml:lang="en-US" '
              'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
              'xmlns:QIDIStudio="http://schemas.qiditech.com/package/2021" '
              'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" '
              'requiredextensions="p">\n')


def write_project(path, parts=None, *, center, objects=None, store=None, machine=None, process=None,
                  filament=None, embed_settings=True, description="", layer_gcodes=(), config_overrides=None):
    """Write a project with one object made of `parts`, or several `objects`.

    parts: [(name, subtype, Mesh)], centred on x/y with the base at z=0.
    objects: [{"name", "parts", "offset": (dx, dy) from the plate centre, "settings": {key: value}}],
        each its own object with optional per-object settings (e.g. print_flow_ratio).
    layer_gcodes: [(print_z, gcode)] custom G-code markers.
    config_overrides: settings to change in the embedded presets.
    """
    name = os.path.basename(path)
    for ext in (".3mf", ".gcode"):
        if name.lower().endswith(ext):
            name = name[:-len(ext)]
    if objects is None:
        objects = [{"name": name, "parts": parts}]
    app_version = store.app_version() if store else "02.07.02.60"
    cx, cy = center
    today = datetime.date.today().isoformat()

    # Every part mesh lives in 3D/Objects/object_1.model with ids 1..N; each object
    # is a set of components pointing at its parts, numbered after the meshes.
    mesh_id = 0
    for obj in objects:
        obj["_mesh_ids"] = []
        for _ in obj["parts"]:
            mesh_id += 1
            obj["_mesh_ids"].append(mesh_id)
    for i, obj in enumerate(objects):
        obj["_id"] = mesh_id + 1 + i

    sub = [MODEL_HEAD, ' <metadata name="QIDIStudio:3mfVersion">1</metadata>\n <resources>\n']
    for obj in objects:
        for mid, (_, _, mesh) in zip(obj["_mesh_ids"], obj["parts"]):
            sub.append(_mesh_xml(mid, mesh, str(uuid.uuid4())))
    sub.append(' </resources>\n <build/>\n</model>\n')

    main = [MODEL_HEAD]
    for k, v in (("Application", "QIDIStudio-" + app_version), ("CreationDate", today),
                 ("ModificationDate", today), ("QIDIStudio:3mfVersion", "1"), ("Title", name),
                 ("Description", description)):
        main.append(' <metadata name="%s">%s</metadata>\n' % (k, escape(v)))
    main.append(' <resources>\n')
    for obj in objects:
        main.append('  <object id="%d" p:UUID="%s" type="model">\n   <components>\n'
                    % (obj["_id"], str(uuid.uuid4())))
        for mid in obj["_mesh_ids"]:
            main.append('    <component p:path="/3D/Objects/object_1.model" objectid="%d" '
                        'p:UUID="%s" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n' % (mid, str(uuid.uuid4())))
        main.append('   </components>\n  </object>\n')
    main.append(' </resources>\n <build p:UUID="%s">\n' % str(uuid.uuid4()))
    for obj in objects:
        dx, dy = obj.get("offset", (0.0, 0.0))
        main.append('  <item objectid="%d" p:UUID="%s" transform="1 0 0 0 1 0 0 0 1 %.4f %.4f 0" '
                    'printable="1"/>\n' % (obj["_id"], str(uuid.uuid4()), cx + dx, cy + dy))
    main.append(' </build>\n</model>\n')

    ms = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n']
    for obj in objects:
        ms.append('  <object id="%d">\n'
                  '    <metadata key="name" value=%s/>\n'
                  '    <metadata key="extruder" value="1"/>\n'
                  # Calibration objects test overhangs, bridges and travel; supports would get in the way.
                  '    <metadata key="enable_support" value="0"/>\n' % (obj["_id"], quoteattr(obj["name"])))
        for key, value in (obj.get("settings") or {}).items():
            ms.append('    <metadata key=%s value=%s/>\n' % (quoteattr(key), quoteattr(str(value))))
        for mid, (pname, subtype, mesh) in zip(obj["_mesh_ids"], obj["parts"]):
            ms.append('    <part id="%d" subtype="%s">\n'
                      '      <metadata key="name" value=%s/>\n'
                      '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
                      '      <mesh_stat face_count="%d" edges_fixed="0" degenerate_facets="0" '
                      'facets_removed="0" facets_reversed="0" backwards_edges="0"/>\n'
                      '    </part>\n' % (mid, subtype, quoteattr(pname), len(mesh.tris)))
        ms.append('  </object>\n')
    ms.append('  <plate>\n'
              '    <metadata key="plater_id" value="1"/>\n'
              '    <metadata key="plater_name" value=""/>\n'
              '    <metadata key="locked" value="false"/>\n'
              '    <metadata key="filament_map_mode" value="Auto For Flush"/>\n'
              '    <metadata key="filament_maps" value="1"/>\n')
    for obj in objects:
        ms.append('    <model_instance>\n'
                  '      <metadata key="object_id" value="%d"/>\n'
                  '      <metadata key="instance_id" value="0"/>\n'
                  '      <metadata key="identify_id" value="%d"/>\n'
                  '    </model_instance>\n' % (obj["_id"], 1000 + obj["_id"]))
    ms.append('  </plate>\n  <assemble>\n')
    for obj in objects:
        dx, dy = obj.get("offset", (0.0, 0.0))
        ms.append('   <assemble_item object_id="%d" instance_id="0" transform="1 0 0 0 1 0 0 0 1 %.4f %.4f 0" '
                  'offset="0 0 0" />\n' % (obj["_id"], cx + dx, cy + dy))
    ms.append('  </assemble>\n</config>\n')

    gc = ['<?xml version="1.0" encoding="utf-8"?>\n<custom_gcodes_per_layer>\n<plate>\n'
          '<plate_info id="1"/>\n']
    for z, code in layer_gcodes:
        gc.append('<layer top_z="%s" type="4" extruder="1" color="" extra=%s gcode=%s/>\n'
                  % (repr(z), quoteattr(code), quoteattr(code)))
    gc.append('<mode value="SingleExtruder"/>\n</plate>\n</custom_gcodes_per_layer>\n')

    content_types = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
                     ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
                     ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
                     ' <Default Extension="png" ContentType="image/png"/>\n'
                     ' <Default Extension="gcode" ContentType="text/x.gcode"/>\n</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n</Relationships>')
    model_rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                  '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
                  ' <Relationship Target="/3D/Objects/object_1.model" Id="rel-1" '
                  'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n</Relationships>')
    slice_info = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <header>\n'
                  '    <header_item key="X-QDT-Client-Type" value="slicer"/>\n'
                  '    <header_item key="X-QDT-Client-Version" value="%s"/>\n'
                  '  </header>\n</config>\n' % app_version)

    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", "".join(main))
        z.writestr("3D/_rels/3dmodel.model.rels", model_rels)
        z.writestr("3D/Objects/object_1.model", "".join(sub))
        z.writestr("Metadata/model_settings.config", "".join(ms))
        z.writestr("Metadata/custom_gcode_per_layer.xml", "".join(gc))
        z.writestr("Metadata/slice_info.config", slice_info)
        if embed_settings and store and machine and process and filament:
            cfg = project_config(store, machine, process, filament)
            cfg.update(config_overrides or {})
            z.writestr("Metadata/project_settings.config", json.dumps(cfg, indent=4, ensure_ascii=False))
    os.replace(tmp, path)
    return path
