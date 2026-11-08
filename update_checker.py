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
        # Downloadbare bestanden van de release (bv. SimTim.zip), nodig voor
        # de directe download vanuit de update-popup.
        assets = [
            {
                "name": a.get("name", ""),
                "url": a.get("browser_download_url", ""),
                "size": a.get("size", 0),
            }
            for a in data.get("assets", [])
            if a.get("browser_download_url")
        ]
        return {
            "update_available": update_available,
            "latest": latest_tag,
            "url": latest_url,
            "assets": assets,
        }
    except (requests.RequestException, ValueError, KeyError):
        return None