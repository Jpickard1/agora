"""Tests for `hubcli post --image` (#117) — agents posting figures from the CLI.
Hermetic: temp root, --no-pointer not needed (we pass --root + never write the
pointer), no server required (save_upload is filesystem-direct)."""

import io
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import cli
from agenthub.store import HubStore

# a tiny valid-enough PNG header + bytes (content doesn't matter for the test)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _root():
    return tempfile.mkdtemp(prefix="post-image-")


def _img(dirpath, name="figure.png"):
    p = os.path.join(dirpath, name)
    with open(p, "wb") as f:
        f.write(PNG)
    return p


def _post(root, argv):
    args = cli.build_parser().parse_args(["--root", root, "post", *argv])
    buf = io.StringIO()
    with redirect_stdout(buf):
        args.func(args)
    return buf.getvalue()


def test_post_image_with_caption():
    root = _root()
    img = _img(root)
    out = _post(root, ["-c", "general", "--author", "yi3", "result figure", "--image", img])
    assert "📎image" in out
    msgs = HubStore(root).read_channel("general")
    m = msgs[-1]
    assert m["text"] == "result figure"
    assert m["meta"]["image"].startswith("/uploads/")
    # the uploaded file actually exists under HUB_ROOT/uploads
    assert os.path.exists(os.path.join(root, m["meta"]["image"].lstrip("/")))


def test_post_image_only_no_text():
    root = _root()
    img = _img(root)
    out = _post(root, ["-c", "general", "--author", "yi3", "--image", img])
    assert "📎image" in out
    m = HubStore(root).read_channel("general")[-1]
    assert m["text"] == ""                    # image-only is allowed
    assert m["meta"]["image"].startswith("/uploads/")


def test_post_image_preserves_extension():
    root = _root()
    img = _img(root, "plot.svg")
    _post(root, ["-c", "general", "--author", "yi3", "--image", img])
    m = HubStore(root).read_channel("general")[-1]
    assert m["meta"]["image"].endswith(".svg")


def test_post_missing_image_errors():
    root = _root()
    try:
        _post(root, ["-c", "general", "--author", "yi3", "--image",
                     os.path.join(root, "nope.png")])
        assert False, "expected SystemExit on missing image"
    except SystemExit as e:
        assert e.code != 0


def test_plain_post_still_works_without_image():
    root = _root()
    out = _post(root, ["-c", "general", "--author", "yi3", "hello"])
    assert "📎image" not in out
    m = HubStore(root).read_channel("general")[-1]
    assert m["text"] == "hello" and not (m.get("meta") or {}).get("image")


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
