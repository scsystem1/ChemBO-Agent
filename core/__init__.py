try:
    from .state import ChemBOState, create_initial_state
    from .graph import build_chembo_graph
except (ModuleNotFoundError, ImportError):  # pragma: no cover - enables lightweight module imports without optional deps
    ChemBOState = None
    create_initial_state = None
    build_chembo_graph = None
