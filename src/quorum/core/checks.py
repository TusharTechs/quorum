"""Deny by default: every route must declare a policy. Registered as a Django system check,
so `manage.py check` (run by the container entrypoint) refuses to boot a build in which a
developer forgot to mark a view."""

from django.core.checks import Error, register
from django.urls import URLPattern, URLResolver, get_resolver

EXEMPT_PREFIXES = ("django.contrib.admin", "django.views.static", "django.contrib.staticfiles", "ninja.")


def _walk(patterns, prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p.callback


@register()
def every_route_has_a_policy(app_configs, **kwargs):
    errors = []
    try:
        resolver = get_resolver()
    except Exception as e:  # pragma: no cover
        return [Error(f"URLconf failed to load: {e}")]
    from quorum.api.router import api

    for path, cb in _walk(resolver.url_patterns):
        inner = getattr(cb, "func", cb)  # functools.partial (ninja) -> underlying function
        mod = getattr(inner, "__module__", "") or getattr(cb, "__module__", "") or ""
        if path.startswith("api/v1/"):
            continue  # API operations are checked individually below (E002)
        if mod.startswith(EXEMPT_PREFIXES) or path.startswith("admin/"):
            continue
        view = getattr(cb, "view_class", cb)
        if getattr(cb, "quorum_policy", None) or getattr(view, "quorum_policy", None):
            continue
        if mod.startswith("ninja"):
            continue
        errors.append(Error(f"route '{path}' ({mod}.{getattr(cb, '__name__', '?')}) has no @policy", id="quorum.E001"))
    for router_prefix, router in api._routers:
        for path, path_view in router.path_operations.items():
            for op in path_view.operations:
                if not getattr(op.view_func, "quorum_policy", None):
                    errors.append(Error(f"API operation {op.view_func.__name__} ({router_prefix}{path}) has no @policy",
                                        id="quorum.E002"))
    return errors
