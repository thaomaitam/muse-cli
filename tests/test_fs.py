import base64
import os
import tempfile
import unittest

from muse_cli.fs import FS, apply_replace, norm


class FakeGW:
    """In-memory stand-in for Gateway.call_json over fs.* routes."""
    def __init__(self, files):
        self.files = dict(files)  # rel path -> bytes
        self.dirs = {""}
        for p in files:
            parts = p.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                self.dirs.add("/".join(parts[:i]))
        self.calls = []

    def call_json(self, method, body=None, timeout=30):
        self.calls.append((method, body))
        p = body.get("path")
        if method == "fs.list":
            names = {}
            for f in self.files:
                if os.path.dirname(f) == p:
                    names[os.path.basename(f)] = {"type": "file", "size": len(self.files[f])}
            for d in self.dirs:
                if d and os.path.dirname(d) == p:
                    names[os.path.basename(d)] = {"type": "directory"}
            return {"path": p, "entries": [{"name": n, **e} for n, e in sorted(names.items())]}
        if method == "fs.stat":
            return {"path": "/" + p, "kind": "dir" if p in self.dirs else "file"}
        if method == "fs.read":
            d = self.files[p][body["offset"]:body["offset"] + body["len"]]
            return {"data_base64": base64.b64encode(d).decode(),
                    "eof": body["offset"] + len(d) >= len(self.files[p])}
        if method == "fs.write":
            data = base64.b64decode(body["data_base64"])
            self.files[p] = (self.files.get(p, b"") if body["append"] else b"") + data
            return {"path": "/" + p, "bytes_written": len(data)}
        if method == "fs.mkdir":
            self.dirs.add(p)
            return {"path": "/" + p, "created": True}
        raise AssertionError(method)


class NormTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(norm("/"), "")
        self.assertEqual(norm(""), "")
        self.assertEqual(norm("/config/home.yaml"), "config/home.yaml")
        self.assertEqual(norm("sandbox://workspace/a.txt"), "workspace/a.txt")
        self.assertEqual(norm("a//b/./c/"), "a/b/c")

    def test_traversal_is_clamped(self):
        self.assertEqual(norm("../etc"), "etc")  # normpath on rooted path clamps


class FSTest(unittest.TestCase):
    def test_list_and_walk(self):
        fs = FS(FakeGW({"config/home.yaml": b"x", "memory/a/b.md": b"y"}))
        self.assertEqual([e["name"] for e in fs.list_dir("/")], ["config", "memory"])
        self.assertIn("memory/a/b.md", [r for r, _, _ in fs.walk("")])
        self.assertNotIn("memory/a/b.md", [r for r, _, _ in fs.walk("", depth=2)])

    def test_chunked_read_and_write(self):
        import muse_cli.fs as m
        old = m.CHUNK, m.WRITE_CHUNK
        m.CHUNK = m.WRITE_CHUNK = 3
        try:
            gw = FakeGW({"f.bin": b"0123456789"})
            fs = FS(gw)
            self.assertEqual(fs.read_file("f.bin"), b"0123456789")
            self.assertEqual(fs.read_file("f.bin", max_bytes=4), b"0123")
            fs.write_file("g.bin", b"abcdefgh")
            self.assertEqual(gw.files["g.bin"], b"abcdefgh")
            writes = [b for meth, b in gw.calls if meth == "fs.write"]
            self.assertEqual([w["append"] for w in writes], [False, True, True])
        finally:
            m.CHUNK, m.WRITE_CHUNK = old

    def test_get_and_put_recursive(self):
        gw = FakeGW({"memory/a.md": b"A", "memory/sub/b.md": b"B"})
        fs = FS(gw)
        with tempfile.TemporaryDirectory() as d:
            got = fs.get("memory", d, recursive=True)
            self.assertEqual(len(got), 2)
            with open(os.path.join(d, "memory", "sub", "b.md"), "rb") as f:
                self.assertEqual(f.read(), b"B")
            with self.assertRaises(ValueError):
                fs.get("memory", d)
            fs.put(os.path.join(d, "memory"), "data/copy", recursive=True)
        self.assertEqual(gw.files["data/copy/sub/b.md"], b"B")

    def test_replace(self):
        self.assertEqual(apply_replace("a b a", "a", "c"), "c b c")
        with self.assertRaises(ValueError):
            apply_replace("abc", "z", "y")


if __name__ == "__main__":
    unittest.main()
