"""Make mutmut's mutated modules importable from a foreign working directory.

mutmut 3.7 loads its Config at *import* time (mutmut.utils.safe_setproctitle
calls Config.get()), and the config reader is cwd-relative.  Several tests here
spawn `python -m git_ots` with cwd set to a throwaway git repository, so the
mutated module's `from mutmut.mutation.trampoline import ...` blows up with
"Could not figure out where the code to mutate is".

This file sits on PYTHONPATH (mutants/src) for those subprocesses, so the
interpreter's `site` module imports it before anything else runs.  It only
neutralises the guess-failure; everything the trampoline actually needs at
runtime comes from the MUTANT_UNDER_TEST environment variable.
"""

import os

if os.environ.get("MUTANT_UNDER_TEST") is not None:
    try:
        import mutmut.configuration as _mutmut_configuration

        _mutmut_configuration._guess_source_paths = lambda: ["src"]
    except Exception:  # pragma: no cover - best effort only
        pass
