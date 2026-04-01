"""Platform-specific automation workarounds."""


def get_platform_prefill(url: str):
    """Return platform-specific prefill function for the given URL, or None.

    Platform modules expose a prefill(page, profile, settings) function that
    fills non-standard DOM widgets AFTER generic extract/fill_form_fields but
    BEFORE the vision agent runs. This keeps deterministic fields out of the
    vision loop entirely.
    """
    from ..account_registry import detect_ats_platform

    if detect_ats_platform(url) == "avature":
        from .avature import prefill
        return prefill
    # Future: workday, greenhouse, taleo, etc.
    return None


def get_platform_vision_page_handler(url: str):
    """Return a platform-owned page handler for the generic vision loop, or None."""
    from ..account_registry import detect_ats_platform

    if detect_ats_platform(url) == "avature":
        from .avature import handle_avature_page
        return handle_avature_page
    return None
