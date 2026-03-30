"""Platform-specific automation workarounds."""


def get_platform_prefill(url: str):
    """Return platform-specific prefill function for the given URL, or None.

    Platform modules expose a prefill(page, profile, settings) function that
    fills non-standard DOM widgets AFTER generic extract/fill_form_fields but
    BEFORE the vision agent runs. This keeps deterministic fields out of the
    vision loop entirely.
    """
    url_lower = url.lower()
    if "avature.net" in url_lower:
        from .avature import prefill
        return prefill
    # Future: workday, greenhouse, taleo, etc.
    return None
