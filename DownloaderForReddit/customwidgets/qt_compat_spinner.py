"""
Compatibility shim: pyqtspinner >= 2.x already handles int-casting correctly,
so CompatibleWaitingSpinner is now a direct alias for WaitingSpinner.

The subclass that patched pyqtspinner 0.1.1's float/int issues was removed
when requirements-mine.txt was updated to pyqtspinner==2.*.
"""

from pyqtspinner.spinner import WaitingSpinner as CompatibleWaitingSpinner

__all__ = ["CompatibleWaitingSpinner"]
