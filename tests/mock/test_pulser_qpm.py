# Guards what a QPM for a Pasqal device tells clients about itself: it runs
# Pulser sequences and nothing else, its technology is neutral-atom, and it
# advertises the neutral-atom capability bit, so the `pasqal` selection finds
# it and a lookup for a superconducting shim never does.

import pathlib
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = str(REPO_ROOT / "services")
if SERVICES not in sys.path:
	sys.path.insert(0, SERVICES)


def _lib_qpm(monkeypatch, provider):
	from svc_lib_qpm import descriptor as shim_descriptor
	from svc_lib_qpm.svc_qpm import QPM
	monkeypatch.delenv("QFW_QPU_DEVICE_ID", raising=False)
	monkeypatch.setattr(
		shim_descriptor, "resolve_descriptor", lambda device_id=None: {
			"provider": provider, "num_qubits": 61})
	qpm = QPM.__new__(QPM)
	qpm.controller_telemetry = lambda: {}
	return qpm


def test_a_pasqal_qpm_declares_pulser_alone(monkeypatch):
	properties = _lib_qpm(monkeypatch, "pasqal").query()["properties"]

	assert properties["circuit_formats"] == ["pulser-abstract-repr"]
	assert "qpy_version" not in properties
	assert properties["device_provider"] == "pasqal"
	assert properties["technology"] == "neutral-atom"
	assert properties["num_qubits"] == 61


@pytest.mark.parametrize("provider,capability", [
	("pasqal", "QPM_CAP_NEUTRAL_ATOM"),
	("iqm", "QPM_CAP_SUPERCONDUCTING"),
	("ibm", "QPM_CAP_SUPERCONDUCTING"),
])
def test_the_capability_bit_follows_what_the_qpm_runs(
		monkeypatch, provider, capability):
	# A client looks a QPM up by its capability bits, so a Pasqal shim must
	# not answer a lookup for a superconducting one, nor the reverse.
	from api_qpm_common import QPMCapability
	from qfw_qiskit.qpm_resolver import _bits_match
	from qfw_qiskit.qpm_selection import qpm_selection_for_provider
	info = _lib_qpm(monkeypatch, provider).query()

	assert info["qpm_capabilities"] == int(
		getattr(QPMCapability, capability))
	assert _bits_match(
		info["qpm_capabilities"],
		qpm_selection_for_provider("pasqal")["qpm_capabilities"]
	) == (provider == "pasqal")
	assert _bits_match(
		info["qpm_capabilities"],
		qpm_selection_for_provider("shim")["qpm_capabilities"]
	) == (provider != "pasqal")
