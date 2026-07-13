"""Generate a Dhan access token in Dhan Cloud.

Dhan Cloud substitutes the three template variables below before this file
runs.  This file intentionally does not read environment variables.
"""

import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TOKEN_ENDPOINT = "https://auth.dhan.co/app/generateAccessToken"

# Dhan Cloud substitutes these template variables at run time.  They are not
# credentials stored in this source file.
CLOUD_CLIENT_ID = "{{client_id}}"
CLOUD_PIN = "{{pin}}"
CLOUD_TOTP = "{{totp}}"


def _cloud_value(value, label):
    """Return a substituted Dhan Cloud value, rejecting raw templates."""
    value = str(value or "").strip()
    if value.startswith("{{") and value.endswith("}}"):
        raise RuntimeError(f"Dhan Cloud did not substitute {label}")
    return value


def generate_access_token():
    """Return ``(client_id, access_token)`` without logging either secret."""
    client_id = _cloud_value(CLOUD_CLIENT_ID, "{{client_id}}")
    pin = _cloud_value(CLOUD_PIN, "{{pin}}")
    totp = _cloud_value(CLOUD_TOTP, "{{totp}}")

    if not client_id:
        raise RuntimeError("Dhan Cloud {{client_id}} is empty")
    if not pin or not totp:
        raise RuntimeError("Dhan Cloud {{pin}} and {{totp}} must both be set")

    url = f"{TOKEN_ENDPOINT}?{urlencode({'dhanClientId': client_id, 'pin': pin, 'totp': totp})}"
    request = Request(url, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Dhan access-token generation failed: {exc}") from exc

    token = payload.get("accessToken") if isinstance(payload, dict) else None
    if not token:
        raise RuntimeError("Dhan did not return an access token; verify your PIN and current TOTP")
    return client_id, str(token)


def main():
    _, token = generate_access_token()
    print(token)


if __name__ == "__main__":
    main()
