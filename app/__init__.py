"""Dodo Lead Audit app package.

Uses the operating system's certificate store for HTTPS (truststore), so httpx works
behind Windows setups where certifi's bundle fails to verify certificates.
"""
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover - optional on systems where certifi works
    pass
