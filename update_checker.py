import requests

from version import __version__, GITHUB_REPO


def _parse_version(v):
    """Zet een versiestring om naar een vergelijkbare tuple, bv. 'v1.2.0' -> (1, 2, 0)."""
    v = v.lstrip("vV")
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_for_update(timeout=5):
    """
    Controleert de nieuwste GitHub Release van GITHUB_REPO.
    Retourneert een dict {"update_available": bool, "latest": str, "url": str},
    of None als de controle mislukt (bv. geen internetverbinding).
    """
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        latest_tag = data.get("tag_name", "")
        latest_url = data.get("html_url", "")
        update_available = _parse_version(latest_tag) > _parse_version(__version__)
        return {
            "update_available": update_available,
            "latest": latest_tag,
            "url": latest_url,
        }
    except (requests.RequestException, ValueError, KeyError):
        return None
