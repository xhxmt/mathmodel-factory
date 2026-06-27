try:
    from .main import _valid_model_step_key, app, settings
    from .project_api import get_steps as _project_get_steps
except ImportError:
    import sys
    from pathlib import Path

    HERE = Path(__file__).resolve().parent
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    from main import _valid_model_step_key, app, settings  # type: ignore
    from project_api import get_steps as _project_get_steps  # type: ignore


def get_steps(project, base_name):
    return _project_get_steps(settings, project, base_name)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
