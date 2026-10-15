"""Bound remote documents before decoding HTML, RSS or JSON."""
import requests


def get_document(url, *, max_bytes=2_000_000, **kwargs):
    response = requests.get(url, stream=True, **kwargs)
    try:
        if int(response.headers.get('Content-Length') or 0) > max_bytes:
            raise ValueError('Documento remoto excede o limite permitido')
        data = bytearray()
        for chunk in response.iter_content(64 * 1024):
            if len(data) + len(chunk) > max_bytes:
                raise ValueError('Documento remoto excede o limite permitido')
            data.extend(chunk)
        response._content = bytes(data)
        response._content_consumed = True
        return response
    finally:
        response.close()
