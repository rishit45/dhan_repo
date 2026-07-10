from pprint import pformat


SENSITIVE_KEYS = {"access_token", "token", "client_secret", "password"}


def mask_sensitive(data):
    if isinstance(data, dict):
        masked = {}
        for key, value in data.items():
            if str(key).lower() in SENSITIVE_KEYS:
                masked[key] = "***MASKED***"
            else:
                masked[key] = mask_sensitive(value)
        return masked
    if isinstance(data, list):
        return [mask_sensitive(item) for item in data]
    if isinstance(data, tuple):
        return tuple(mask_sensitive(item) for item in data)
    return data


def print_data(label, data):
    print(f"[DATA] {label}: {pformat(mask_sensitive(data))}")
