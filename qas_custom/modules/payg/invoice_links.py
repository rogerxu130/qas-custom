"""Narrow authorization for a reviewed draft consolidation relink."""
from contextlib import contextmanager
from contextvars import ContextVar


_relink = ContextVar("payg_invoice_relink", default=None)


@contextmanager
def allow_invoice_relink(operation, source, target):
    token = _relink.set((operation, source, target))
    try:
        yield
    finally:
        _relink.reset(token)


def permitted_invoice_relink(operation, source, target):
    return _relink.get() == (operation, source, target)
