"""Matplotlib backend for Studio notebooks: Agg rendering, and ``plt.show()`` sends the open figures to the running cell.

The kernel selects it with ``MPLBACKEND=module://cwstudio.mplbackend``, so ``plt.show()`` works even in the cell that first imports pyplot (patching ``plt.show`` after the fact only helps later cells).
"""
from matplotlib.backend_bases import _Backend
from matplotlib.backends.backend_agg import FigureCanvasAgg, _BackendAgg  # noqa: F401 (FigureCanvasAgg is part of the backend interface)


@_Backend.export
class _BackendStudio(_BackendAgg):
    @staticmethod
    def show(*args, **kwargs):
        from cwstudio import notebook
        if notebook.SHOW_HOOK is not None:
            notebook.SHOW_HOOK()
