# requests-utf8-length

With urllib3 2.x, string request bodies are sent as UTF-8 bytes, but Content-Length can count Unicode code points. Compute the byte length consistently for non-ASCII string bodies. Preserve ASCII and bytes behavior, and existing handling of seekable byte streams. This environment uses urllib3 2.x; no real network is needed for reproduction.
