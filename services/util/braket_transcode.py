# Shared Qiskit -> Amazon Braket OpenQASM 3 transcode utilities.
#
# Used by the QDMI driver's Braket profile (svc_lib_qpm/drivers/qdmi_profiles.py)
# to turn the circuit a client sent, QPY or OpenQASM 2, into the self-contained
# OpenQASM 3 program the Amazon Braket QDMI device library submits as a Braket
# quantum task, and to key the counts that come back onto the circuit's
# classical bits.
#
# Braket compiles and routes a non-verbatim program on its side, so no coupling
# map is applied here. What has to hold is the vocabulary: every gate must be
# one the device accepts, under Braket's name for it. A gate the device does
# not accept is decomposed into ones it does, and anything still unexpressible
# fails here, before a task is created and billed.
#
# Qiskit is imported where it is used, so this module imports without it.

from defw_exception import DEFwExecutionError

# Measurement and barrier are output/directive syntax. Reset needs explicit
# device support, just like a gate.
DIRECTIVES = frozenset({"barrier", "measure"})

# The technology family of a Braket device, from the vendor segment of its
# ARN: arn:aws:braket:<region>::device/qpu/<vendor>/<name>, or
# arn:aws:braket:::device/quantum-simulator/amazon/<name>. The values are the
# qhw-device-v1 vocabulary.
VENDOR_TECHNOLOGY = {
	"rigetti": "superconducting",
	"iqm": "superconducting",
	"ionq": "trapped-ion",
	"aqt": "trapped-ion",
	"quera": "neutral-atom",
}


def technology_for_arn(arn):
	"""The qhw technology family for a Braket device ARN, or None."""
	parts = str(arn or "").split("/")
	if "quantum-simulator" in parts:
		return "simulator"
	if "qpu" in parts:
		index = parts.index("qpu")
		if index + 1 < len(parts):
			return VENDOR_TECHNOLOGY.get(parts[index + 1].lower())
	return None


def accepted_operation_names(device):
	"""The gate names this Braket device takes, lowercased.

	QDMI's device operations are the QPU's native gate set (for SV1 and DM1,
	the executable set). The Braket device library puts Braket's broader
	supportedOperations set in device CUSTOM1 as operation handles. A
	non-verbatim program may use any of them, since Braket compiles it.
	"""
	names = set()
	for operation in _operations(device):
		name = _operation_name(operation)
		if name:
			names.add(name.lower())
	query = getattr(device, "query_custom_operations", None)
	if query is not None:
		try:
			from mqt.core.qdmi import CustomProperty
			extra = query(CustomProperty.CUSTOM1) or []
		except Exception:
			extra = []
		for operation in extra:
			name = _operation_name(operation)
			if name:
				names.add(name.lower())
	if not names:
		raise DEFwExecutionError(
			"the Braket device reported no operations, so no program can "
			"be checked against it")
	return names


def to_braket_qasm3(circuit, backend):
	"""Return Braket OpenQASM 3 and the legacy counts' measurement map."""
	from qiskit.converters import circuit_to_dag, dag_to_circuit
	from mqt.core.qdmi import ProgramFormat

	accepted = accepted_operation_names(backend.device)

	# Temporary: Braket's QPU Target contains only native gates, while its
	# non-verbatim compiler also accepts supportedOperations (CUSTOM1).
	# Reuse the adapter's aliases and serializer, retaining this broader
	# vocabulary until the adapter exposes that compilation contract.
	# https://github.com/Munich-Quantum-Software/amazon-braket-qdmi-device/issues/207
	def braket_name(name):
		name = name.lower()
		if name in accepted:
			return name
		aliases = backend._map_qiskit_gate_to_operation_names(name)
		return min(aliases & accepted, default=name)

	def unaccepted(circ):
		return sorted({
			instruction.operation.name for instruction in circ.data
			if instruction.operation.name.lower() not in DIRECTIVES
			and braket_name(instruction.operation.name) not in accepted})

	if circuit.parameters:
		raise DEFwExecutionError("Braket circuits must have bound parameters")
	# The legacy histogram carries final qubit values only. It cannot
	# recover an earlier measurement followed by a gate. Remove this guard
	# with the library's program-output release, after checking its subset.
	# https://github.com/Munich-Quantum-Software/amazon-braket-qdmi-device/pull/242
	measuring = False
	for instruction in circuit.data:
		name = instruction.operation.name.lower()
		if name == "measure":
			measuring = True
		elif measuring and name != "barrier":
			raise DEFwExecutionError(
				"Braket's legacy QDMI results require terminal measurements")

	if unaccepted(circuit):
		circuit = _decompose(circuit, accepted, backend)
		if missing := unaccepted(circuit):
			raise DEFwExecutionError(
				f"the device accepts none of {missing}, and they could not be "
				f"decomposed into its operation set {sorted(accepted)}")

	dag = circuit_to_dag(circuit)
	dag.remove_qubits(*(set(dag.idle_wires()) & set(dag.qubits)))
	program_circuit = dag_to_circuit(dag)
	measurement_map = []
	for index, instruction in enumerate(program_circuit.data):
		name = instruction.operation.name.lower()
		if name == "measure":
			measurement_map.append((
				program_circuit.find_bit(instruction.qubits[0]).index,
				program_circuit.find_bit(instruction.clbits[0]).index))
		elif name not in DIRECTIVES:
			new_name = braket_name(name)
			if new_name != name:
				program_circuit.data[index] = instruction.replace(
					operation=instruction.operation.copy(name=new_name))
	native = {op.name().lower() for op in _operations(backend.device)}
	used = {inst.operation.name for inst in program_circuit.data} - DIRECTIVES
	if used <= native:
		# MQT Core's serialization hook suffices for the native vocabulary.
		program, _fmt = backend._serialize_circuit(
			program_circuit, [ProgramFormat.QASM3])
	else:
		# Temporary: the 1.2 adapter's DAG round trip resets standard gate
		# names and re-aliases them against only native operations. Serialize
		# the broader vocabulary directly until #207's contract is supported.
		from qiskit import qasm3
		from qiskit.circuit import QuantumRegister
		if program_circuit.qubits and not program_circuit.qregs:
			program_circuit.add_register(
				QuantumRegister(bits=program_circuit.qubits, name="q"))
		program = qasm3.dumps(program_circuit, includes=(), basis_gates=sorted(used))
	return program, {
		"num_clbits": program_circuit.num_clbits, "map": measurement_map}


def remap_counts(counts, measurement):
	"""Key the Braket device library's histogram by classical bit.

	The library writes each key with the highest-index measured qubit
	leftmost, over the measured qubits in ascending order. Qiskit keys by
	classical bit, highest index leftmost, over every bit of the registers.
	The two agree only when bit i holds qubit i and every qubit is measured,
	so rebuild each key from the measurement map recorded at transcode time.
	An unmeasured bit reads 0, as it does in Qiskit.
	"""
	# Temporary for amazon-braket-qdmi 1.2. Delete with its program-output
	# release; applying this map to already-normalized counts would corrupt
	# permuted outputs. Update setup/qdmi-requirements.txt in the same change.
	# https://github.com/Munich-Quantum-Software/amazon-braket-qdmi-device/pull/242
	if not counts or not isinstance(measurement, dict):
		return counts
	pairs = list(measurement.get("map") or [])
	num_clbits = int(measurement.get("num_clbits") or 0)
	if not pairs or not num_clbits:
		return counts
	measured = sorted({qubit for qubit, _clbit in pairs})
	# Where each measured qubit's bit sits in a library key: position 0 holds
	# the highest measured qubit.
	position = {
		qubit: len(measured) - 1 - rank for rank, qubit in enumerate(measured)}
	remapped = {}
	for key, count in counts.items():
		bits = str(key)
		if len(bits) != len(measured):
			raise DEFwExecutionError(
				f"Braket counts key {key!r} does not cover the "
				f"{len(measured)} measured qubits")
		out = ["0"] * num_clbits
		for qubit, clbit in pairs:
			out[num_clbits - 1 - clbit] = bits[position[qubit]]
		new_key = "".join(out)
		remapped[new_key] = remapped.get(new_key, 0) + count
	return remapped


def _decompose(circuit, accepted, backend):
	# Rewrite into Qiskit gates whose Braket spelling the device accepts. Only
	# standard gates can be a transpile basis; a Braket-only name (gpi, ms,
	# ...) has no Qiskit gate to decompose into and is left to the rename.
	from qiskit import transpile
	from qiskit.circuit.library import get_standard_gate_name_mapping
	standard = set(get_standard_gate_name_mapping()) - DIRECTIVES
	basis = sorted(
		name for name in standard
		if backend._map_qiskit_gate_to_operation_names(name) & accepted)
	if not basis:
		return circuit
	try:
		return transpile(
			circuit, basis_gates=basis + ["measure"], optimization_level=1)
	except Exception as exc:
		raise DEFwExecutionError(
			"could not decompose the circuit into the device's operation set "
			f"{sorted(accepted)}: {exc}") from exc


def _operations(device):
	# The first query that reaches AWS. Its failure is the one to report:
	# without credentials or network the library says "Permission denied"
	# here, and swallowing it would turn into a misleading complaint about
	# the circuit's gates further on.
	try:
		return list(device.operations() or [])
	except Exception as exc:
		raise DEFwExecutionError(
			f"could not read the Braket device's operations: {exc}") from exc


def _operation_name(operation):
	try:
		return operation.name()
	except Exception:
		return None
