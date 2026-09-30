"""HTTPS helper: certificate source selection and the error shown when verification fails."""
import ssl
import sys
import urllib.error
import urllib.request

import pytest

from cwstudio import net


@pytest.fixture(autouse=True)
def fresh_context(monkeypatch):
    monkeypatch.setattr(net, "_CTX", None)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    yield
    net._CTX = None


def test_uses_os_trust_store():
    truststore = pytest.importorskip("truststore")
    assert isinstance(net.ssl_context(), truststore.SSLContext)


def test_falls_back_to_certifi(monkeypatch):
    certifi = pytest.importorskip("certifi")
    monkeypatch.setitem(sys.modules, "truststore", None)  # import now fails
    ctx = net.ssl_context()
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.cert_store_stats()["x509_ca"] > 50, certifi.where()


def test_ssl_cert_file_wins(monkeypatch, tmp_path):
    certifi = pytest.importorskip("certifi")
    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
    ctx = net.ssl_context()
    assert ctx.cert_store_stats()["x509_ca"] > 50


def test_certificate_failure_explains_the_fix(monkeypatch):
    def boom(req, timeout=None, context=None):
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed: unable to get local issuer certificate"))
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(net.CertificateError) as e:
        net.urlopen("https://raw.githubusercontent.com/x")
    msg = str(e.value)
    assert "raw.githubusercontent.com" in msg and "SSL_CERT_FILE" in msg and "trust store" in msg


def test_plain_file_urls_do_not_need_tls(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hi")
    with net.urlopen(f.as_uri()) as r:
        assert r.read() == b"hi"
