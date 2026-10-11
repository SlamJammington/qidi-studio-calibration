"""Send G-code to a network printer saved in QIDI Studio.

QIDI's Klipper printers (and OctoPrint) accept OctoPrint-style uploads, which
is what QIDI Studio itself uses for its "octoprint" host type:
POST http://<host>/api/files/local with the file and an optional "print" flag.
"""
import json
import os
import urllib.error
import urllib.request
import uuid

SUPPORTED = ("octoprint",)


def describe(printer):
    return "%s (%s)" % (printer.get("name", "printer"), printer.get("print_host", "?"))


def base_url(printer):
    host = (printer.get("print_host") or "").strip().rstrip("/")
    if not host:
        raise ValueError("%s has no address set in QIDI Studio" % printer.get("name", "This printer"))
    if "://" not in host:
        host = "http://" + host
    port = str(printer.get("printhost_port") or "").strip()
    if port and port != "0" and host.count(":") < 2:
        host += ":" + port
    return host


def _multipart(fields, file_field, filename, data):
    boundary = "----qidi-studio-calibration-" + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, name, value)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n" % (boundary, file_field, filename)).encode())
    parts.append(data)
    parts.append(("\r\n--%s--\r\n" % boundary).encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def upload(printer, path, start=False, timeout=120):
    """Upload `path` to `printer` (a QIDI Studio physical printer). Returns the server's reply."""
    kind = (printer.get("host_type") or "octoprint").lower()
    if kind not in SUPPORTED:
        raise ValueError("QIDI Studio's \"%s\" printer connections aren't supported yet. Copy the "
                         ".gcode to the printer yourself (USB stick or its web page)." % kind)
    url = base_url(printer) + "/api/files/local"
    with open(path, "rb") as f:
        data = f.read()
    body, content_type = _multipart({"select": "true" if start else "false",
                                     "print": "true" if start else "false"},
                                    "file", os.path.basename(path), data)
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": content_type, "Content-Length": str(len(body))})
    key = printer.get("printhost_apikey") or ""
    if key:
        req.add_header("X-Api-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError("%s refused the upload (HTTP %d): %s" % (describe(printer), e.code, detail)) from e
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise RuntimeError("Couldn't reach %s: %s" % (describe(printer), reason)) from e
    try:
        return json.loads(reply)
    except ValueError:
        return {"reply": reply}
