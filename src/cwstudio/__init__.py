"""ChipWhisperer Studio - standalone desktop application for ChipWhisperer.

See docs/DESIGN.md for the architecture. Run with ``python -m cwstudio`` or the ``cw-studio`` console script.
"""

__version__ = "1.0.0"

from cwstudio import compat as _compat

_compat.install()
