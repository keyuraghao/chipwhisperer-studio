"""HTTPS helper shared by every download and GitHub lookup in Studio.

Python's default SSL setup only finds CA certificates where OpenSSL was configured to look, which fails in frozen bundles, python.org builds on macOS and Windows, and on networks that inspect HTTPS with their own root certificate ("certificate verify failed: unable to get local issuer certificate"). Studio therefore verifies against the operating system's trust store through ``truststore`` (the same certificates the browser uses, including roots installed by an IT department), falls back to Mozilla's bundle from ``certifi``, and honours ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` when they are set.
"""
from __future__ import annotations

import logging
import os
import ssl
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger("cwstudio.net")
_CTX: Optional[ssl.SSLContext] = None
HELP = "Studio checks HTTPS certificates against your system's trust store. If your network inspects HTTPS traffic, install its root certificate in the system store, or point SSL_CERT_FILE at a PEM bundle that contains it, then restart Studio."


def ssl_context() -> ssl.SSLContext:
    """A verifying SSL context that works on every platform Studio ships for (built once)."""
    global _CTX
    if _CTX is not None:
        return _CTX
    cafile, capath = os.environ.get("SSL_CERT_FILE"), os.environ.get("SSL_CERT_DIR")
    if cafile or capath:
        _CTX = ssl.create_default_context(cafile=cafile or None, capath=capath or None)
        log.debug("using certificates from SSL_CERT_FILE/SSL_CERT_DIR")
        return _CTX
    try:
        import truststore
        _CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        log.debug("using the operating system trust store")
        return _CTX
    except Exception as e:  # noqa: BLE001
        log.debug("truststore unavailable (%s), trying certifi", e)
    try:
        import certifi
        _CTX = ssl.create_default_context(cafile=certifi.where())
        log.debug("using the certifi CA bundle")
    except Exception as e:  # noqa: BLE001
        log.debug("certifi unavailable (%s), using Python's default certificates", e)
        _CTX = ssl.create_default_context()
    return _CTX


class CertificateError(OSError):
    pass


def urlopen(req, timeout: float = 30):
    """``urllib.request.urlopen`` with Studio's SSL context and a helpful message when certificate checks fail."""
    url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    kw = {"context": ssl_context()} if url.startswith("https:") else {}
    try:
        return urllib.request.urlopen(req, timeout=timeout, **kw)
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(e.reason):
            host = urllib.request.urlparse(url).hostname
            raise CertificateError(f"could not verify the HTTPS certificate of {host} ({e.reason}). {HELP}") from None
        raise
