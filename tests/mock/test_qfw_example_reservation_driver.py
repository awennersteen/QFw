import argparse

from qfw_qiskit.reservation_set import QPMReservation

import qfw_example_reservation_driver as driver


def _args():
	return argparse.Namespace(backend="nwqsim", timeout=1, target_device=None)


def test_reserve_uses_scheduler_owned_reservation(monkeypatch):
	resolved = type("Resolved", (), {"service_id": "nwqsim"})()
	qpm = type("QPM", (), {
		"reserve": lambda self, request: (_ for _ in ()).throw(
			AssertionError("reserve must not be called"))
	})()
	emitted = []
	monkeypatch.setattr(
		driver, "resolve_qpm", lambda *_, **__: (resolved, qpm))
	monkeypatch.setattr(
		driver,
		"parse_qfw_reservations",
		lambda required: [QPMReservation("nwqsim", "41")],
	)
	monkeypatch.setattr(
		driver, "emit", lambda kind, **payload: emitted.append((kind, payload))
	)

	assert driver.reserve(_args()) == 0
	assert emitted == [(
		"reserve",
		{
			"backend": "nwqsim",
			"service_id": "nwqsim",
			"ownership": "scheduler",
			"decision": {"status": "accepted", "reservation_id": "41"},
		},
	)]


def test_resolve_qpm_selects_by_target_device(monkeypatch):
	# A site plane can run several QPMs of one provider (the shim serves the
	# IQM, IBM and AWS devices), so a backend alone is ambiguous there. The
	# device the job asked for is what tells them apart.
	calls = []
	resolved = type("Resolved", (), {
		"service_id": "shim-ornl-20q", "runtime_id": "runtime-1"})()

	class FakeBinding:
		def api(self, name, expected_runtime_id=None):
			return (name, expected_runtime_id)

	class FakeResolver:
		def resolve(self, **kwargs):
			calls.append(kwargs)
			return resolved

		def managed_binding(self, resolved_qpm):
			assert resolved_qpm is resolved
			return FakeBinding()

	monkeypatch.setattr(driver, "defw_get_directory_service", lambda: object())
	monkeypatch.setattr(
		driver.QPMResolver, "from_environment",
		staticmethod(lambda dirsvc=None: FakeResolver()))

	got, api = driver.resolve_qpm("shim", 1, target_device="ornl-iqm-20q")
	assert got is resolved
	assert api == ("admission", "runtime-1")
	assert calls[-1]["provider"] == "shim"
	assert calls[-1]["selector_resource"] == "ornl-iqm-20q"

	driver.resolve_qpm("shim", 1)
	assert "selector_resource" not in calls[-1]
