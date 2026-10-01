"""ChipWhisperer Studio - standalone desktop application for ChipWhisperer.

See docs/DESIGN.md for the architecture. Run with ``python -m cwstudio`` or the ``cw-studio`` console script.
"""

__version__ = "0.4.4"

from cwstudio import compat as _compat

_compat.install()
