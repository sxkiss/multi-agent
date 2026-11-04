"""重-2 回归：SSRF 与压缩包成员校验。"""
import io
import os
import stat
import tarfile
import zipfile

import pytest

from security_utils import (
    UnsafeArchiveError,
    UnsafeURLError,
    safe_extract_tar,
    safe_extract_zip,
    validate_public_url,
    validate_zip_members,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://10.1.2.3/x",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x",
        "http://localhost/x",
        "file:///etc/passwd",
    ],
)
def test_ssrf_blocks_private_and_non_http(url):
    with pytest.raises(UnsafeURLError):
        validate_public_url(url)


def test_public_ip_literal_allowed_without_dns():
    assert validate_public_url("https://1.1.1.1/a.zip") == "https://1.1.1.1/a.zip"


def _zip_with(name, data=b"x", *, mode=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo(name)
        if mode is not None:
            info.create_system = 3
            info.external_attr = mode << 16
        zf.writestr(info, data)
    buf.seek(0)
    return zipfile.ZipFile(buf)


@pytest.mark.parametrize("name", ["../escape.txt", "/tmp/escape.txt", "a/../../escape.txt"])
def test_zip_traversal_rejected(name):
    with _zip_with(name) as zf:
        with pytest.raises(UnsafeArchiveError):
            validate_zip_members(zf)


def test_zip_symlink_rejected():
    with _zip_with("link", "", mode=stat.S_IFLNK | 0o777) as zf:
        with pytest.raises(UnsafeArchiveError):
            validate_zip_members(zf)


def test_zip_safe_extract_roundtrip(tmp_path):
    with _zip_with("skill/SKILL.md", b"ok") as zf:
        count = safe_extract_zip(zf, str(tmp_path), strip_prefix="skill/")
    assert count == 1
    assert (tmp_path / "SKILL.md").read_text() == "ok"


def test_tar_symlink_rejected(tmp_path):
    path = tmp_path / "bad.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo("escape")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with tarfile.open(path, "r:gz") as tf:
        with pytest.raises(UnsafeArchiveError):
            safe_extract_tar(tf, str(tmp_path / "out"))


def test_tar_normal_restore(tmp_path):
    path = tmp_path / "ok.tar.gz"
    src = tmp_path / "SKILL.md"
    src.write_text("ok")
    with tarfile.open(path, "w:gz") as tf:
        tf.add(src, arcname="skills/demo/SKILL.md")
    out = tmp_path / "out"
    out.mkdir()
    with tarfile.open(path, "r:gz") as tf:
        safe_extract_tar(tf, str(out))
    assert (out / "skills" / "demo" / "SKILL.md").read_text() == "ok"
