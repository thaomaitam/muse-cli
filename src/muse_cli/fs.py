"""Muse "System Files": the agent's home directory, browsed via fs.* routes.

Paths are relative to the System Files root (the agent home, which
contains agents/, config/, memory/, workspace/ ...). The server rejects
absolute paths and `..`; `norm()` maps "/", "", "/config" etc. to that form.
"""
import base64
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile

CHUNK = 4 * 1024 * 1024   # fs.read window
WRITE_CHUNK = 4 * 1024 * 1024


def norm(path):
    p = (path or "").strip().replace("\\", "/")
    if p.startswith("sandbox://"):
        p = p[len("sandbox://"):]
    p = posixpath.normpath("/" + p).lstrip("/")
    if p == ".":
        p = ""
    if any(part == ".." for part in p.split("/")):
        raise ValueError(f"path traversal not allowed: {path!r}")
    return p


class FS:
    def __init__(self, gw):
        self.gw = gw

    def _call(self, method, body, timeout=60):
        return self.gw.call_json(method, body=body, timeout=timeout)

    # -- read ---------------------------------------------------------------
    def list_dir(self, path=""):
        return self._call("fs.list", {"path": norm(path)}).get("entries", [])

    def stat(self, path):
        return self._call("fs.stat", {"path": norm(path)})

    def read_chunk(self, path, offset=0, length=CHUNK):
        r = self._call("fs.read", {"path": norm(path), "offset": offset, "len": length}, timeout=120)
        return base64.b64decode(r.get("data_base64") or ""), bool(r.get("eof", True))

    def read_file(self, path, max_bytes=None):
        out, off = bytearray(), 0
        while True:
            n = CHUNK if max_bytes is None else min(CHUNK, max_bytes - off)
            if n <= 0:
                break
            data, eof = self.read_chunk(path, off, n)
            out += data
            off += len(data)
            if eof or not data:
                break
        return bytes(out)

    def search(self, contains, limit=None):
        body = {"name": {"contains": contains}}
        if limit:
            body["limit"] = limit
        return self._call("fs.search", body).get("entries", [])

    def walk(self, path="", depth=None, _level=0):
        """Yield (relpath, entry) depth-first."""
        base = norm(path)
        for e in self.list_dir(base):
            rel = posixpath.join(base, e["name"]) if base else e["name"]
            yield rel, e, _level
            if e.get("type") == "directory" and (depth is None or _level + 1 < depth):
                yield from self.walk(rel, depth, _level + 1)

    # -- write --------------------------------------------------------------
    def write_file(self, path, data, overwrite=True, create_parent=True):
        if isinstance(data, str):
            data = data.encode()
        p, off, first = norm(path), 0, True
        while first or off < len(data):
            part = data[off:off + WRITE_CHUNK]
            self._call("fs.write", {"path": p, "overwrite": overwrite if first else True,
                                    "append": not first, "create_parent": create_parent,
                                    "data_base64": base64.b64encode(part).decode()}, timeout=120)
            off += len(part)
            first = False
        return {"path": "/" + p, "bytes_written": len(data)}

    def mkdir(self, path, parents=True):
        return self._call("fs.mkdir", {"path": norm(path), "recursive": parents})

    def move(self, src, dst, overwrite=False, create_parent=True):
        return self._call("fs.rename", {"source": norm(src), "destination": norm(dst),
                                        "overwrite": overwrite, "create_parent": create_parent})

    def delete(self, path, recursive=False):
        return self._call("fs.delete", {"path": norm(path), "recursive": recursive})

    # -- transfer helpers ---------------------------------------------------
    def get(self, path, dest, recursive=False):
        """Download file (or folder with recursive) into local dir/file. Returns local paths."""
        p = norm(path)
        st = self.stat(p)
        name = posixpath.basename(p) or "system-files"
        if st.get("kind") in ("dir", "directory"):
            if not recursive:
                raise ValueError(f"{path} is a folder; use -r")
            root = os.path.join(dest, name)
            os.makedirs(root, exist_ok=True)
            got = []
            for rel, e, _ in self.walk(p):
                local = os.path.join(root, *rel[len(p):].lstrip("/").split("/"))
                if e.get("type") == "directory":
                    os.makedirs(local, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(local), exist_ok=True)
                    data = self.read_file(rel)
                    with open(local, "wb") as f:
                        f.write(data)
                    got.append(local)
            return got
        local = os.path.join(dest, name) if os.path.isdir(dest) else dest
        data = self.read_file(p)
        with open(local, "wb") as f:
            f.write(data)
        return [local]

    def put(self, local, remote, recursive=False, overwrite=True):
        """Upload local file (or folder with recursive) to remote path."""
        r = norm(remote)
        if os.path.isdir(local):
            if not recursive:
                raise ValueError(f"{local} is a folder; use -r")
            done = []
            for d, _dirs, files in os.walk(local):
                rel = os.path.relpath(d, local).replace(os.sep, "/")
                rd = r if rel == "." else posixpath.join(r, rel)
                self.mkdir(rd)
                for fn in files:
                    with open(os.path.join(d, fn), "rb") as f:
                        done.append(self.write_file(posixpath.join(rd, fn), f.read(), overwrite))
            return done
        with open(local, "rb") as f:
            return [self.write_file(r, f.read(), overwrite)]


def apply_replace(text, old, new, count=None):
    if old not in text:
        raise ValueError(f"--replace: text not found: {old!r}")
    return text.replace(old, new) if count is None else text.replace(old, new, count)


def edit_in_editor(data, suffix=""):
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False) as t:
        t.write(data)
    try:
        subprocess.call(editor + " " + _shq(t.name), shell=True)
        with open(t.name, "rb") as f:
            return f.read()
    finally:
        os.unlink(t.name)


def _shq(s):
    import shlex
    return shlex.quote(s)


def fmt_size(n):
    if n is None:
        return "-"
    for u in ("B", "K", "M", "G"):
        if n < 1024 or u == "G":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
