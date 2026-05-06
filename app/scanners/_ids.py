from base64 import urlsafe_b64encode


def make_encoded_asset_id(category: str, raw: str) -> str:
    encoded = urlsafe_b64encode(raw.encode('utf-8')).decode('ascii').rstrip('=') or 'item'
    return f'{category}__{encoded}'
