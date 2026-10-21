# Shared setup for the tests that need QFw's own venv.
#
# These run outside the CI mock job, which installs no qiskit, and outside a
# full QFw install there is no DEFw either. Each test file used to put the
# services directory on the path and stand in its own minimal
# defw_exception. That made collection order matter: whichever file was
# imported first installed the stub the whole session then used, so a file
# needing more exception types than the first one happened to define failed
# on import. test_braket_qasm3 collects first alphabetically and defined only
# DEFwExecutionError, which is why the drivers package could not be imported
# from test_iqm_transpile.
#
# So do it once, here, before any test module is imported, and mint each
# DEFw* name on demand instead of listing the ones a test needs today.

import pathlib
import sys
import types

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = str(REPO_ROOT / "services")
if SERVICES not in sys.path:
	sys.path.insert(0, SERVICES)

if "defw_exception" not in sys.modules:
	try:
		import defw_exception  # noqa: F401
	except ImportError:
		class _StubError(Exception):
			pass

		class _DEFwExceptionStub(types.ModuleType):
			def __getattr__(self, name):
				if not name.startswith("DEFw"):
					raise AttributeError(name)
				minted = type(name, (_StubError,), {})
				setattr(self, name, minted)
				return minted

		sys.modules["defw_exception"] = _DEFwExceptionStub("defw_exception")
