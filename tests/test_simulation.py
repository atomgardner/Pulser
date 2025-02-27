# Copyright 2020 Pulser Development Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections import Counter
from unittest.mock import patch

import numpy as np
import pytest
import qutip

from pulser import Pulse, Register, Sequence
from pulser.devices import Chadoq2, IroiseMVP, MockDevice
from pulser.register.register_layout import RegisterLayout
from pulser.sampler import sampler
from pulser.waveforms import BlackmanWaveform, ConstantWaveform, RampWaveform
from pulser_simulation import QutipEmulator, SimConfig, Simulation


@pytest.fixture
def reg():
    q_dict = {
        "control1": np.array([-4.0, 0.0]),
        "target": np.array([0.0, 4.0]),
        "control2": np.array([4.0, 0.0]),
    }
    return Register(q_dict)


@pytest.fixture
def seq(reg):
    duration = 1000
    pi = Pulse.ConstantDetuning(BlackmanWaveform(duration, np.pi), 0.0, 0)
    twopi = Pulse.ConstantDetuning(
        BlackmanWaveform(duration, 2 * np.pi), 0.0, 0
    )
    pi_Y = Pulse.ConstantDetuning(
        BlackmanWaveform(duration, np.pi), 0.0, -np.pi / 2
    )
    seq = Sequence(reg, Chadoq2)
    # Declare Channels
    seq.declare_channel("ryd", "rydberg_local", "control1")
    seq.declare_channel("raman", "raman_local", "control1")

    # Prepare state 'hhh':
    seq.add(pi_Y, "raman")
    seq.target("target", "raman")
    seq.add(pi_Y, "raman")
    seq.target("control2", "raman")
    seq.add(pi_Y, "raman")

    # Write CCZ sequence:
    seq.add(pi, "ryd", protocol="wait-for-all")
    seq.target("control2", "ryd")
    seq.add(pi, "ryd")
    seq.target("target", "ryd")
    seq.add(twopi, "ryd")
    seq.target("control2", "ryd")
    seq.add(pi, "ryd")
    seq.target("control1", "ryd")
    seq.add(pi, "ryd")

    # Add a ConstantWaveform part to testout the drawing procedure
    seq.add(Pulse.ConstantPulse(duration, 1, 0, 0), "ryd")
    return seq


@pytest.fixture
def matrices():
    pauli = {}
    pauli["I"] = qutip.qeye(2)
    pauli["X"] = qutip.sigmax()
    pauli["Y"] = qutip.sigmay()
    pauli["Z"] = qutip.sigmaz()
    return pauli


def test_bad_import():
    with pytest.warns(
        UserWarning,
        match="'pulser.simulation' are changed to 'pulser_simulation'.",
    ):
        import pulser.simulation  # noqa: F401

    assert pulser.simulation.Simulation is Simulation
    assert pulser.simulation.SimConfig is SimConfig


def test_initialization_and_construction_of_hamiltonian(seq, mod_device):
    fake_sequence = {"pulse1": "fake", "pulse2": "fake"}
    with pytest.raises(TypeError, match="sequence has to be a valid"):
        Simulation(fake_sequence)
    with pytest.raises(TypeError, match="sequence has to be a valid"):
        QutipEmulator(fake_sequence, Register.square(2), mod_device)
    # Simulation cannot be run on a register not defining "control1"
    with pytest.raises(
        ValueError,
        match="The ids of qubits targeted in Local channels",
    ):
        QutipEmulator(
            sampler.sample(seq),
            Register(
                {
                    "target": np.array([0.0, 0.0]),
                    "control2": np.array([1.0, 0.0]),
                }
            ),
            MockDevice,
        )
    sim = Simulation(seq, sampling_rate=0.011)
    assert sim._seq == seq
    assert sim._qdict == seq.qubit_info
    assert sim._size == len(seq.qubit_info)
    assert sim._tot_duration == 9000  # seq has 9 pulses of 1µs
    assert sim._qid_index == {"control1": 0, "target": 1, "control2": 2}

    with pytest.raises(ValueError, match="too small, less than"):
        Simulation(seq, sampling_rate=0.0001)
    with pytest.raises(ValueError, match="`sampling_rate`"):
        Simulation(seq, sampling_rate=5)
    with pytest.raises(ValueError, match="`sampling_rate`"):
        Simulation(seq, sampling_rate=-1)

    assert sim._sampling_rate == 0.011
    assert len(sim.sampling_times) == int(
        sim._sampling_rate * sim._tot_duration
    )

    assert isinstance(sim._hamiltonian, qutip.QobjEvo)
    # Checks adapt() method:
    assert bool(set(sim._hamiltonian.tlist).intersection(sim.sampling_times))
    for qobjevo in sim._hamiltonian.ops:
        for sh in qobjevo.qobj.shape:
            assert sh == sim.dim**sim._size

    assert not seq.is_parametrized()
    with pytest.warns(UserWarning, match="returns a copy of itself"):
        seq_copy = seq.build()  # Take a copy of the sequence
    x = seq_copy.declare_variable("x")
    seq_copy.add(Pulse.ConstantPulse(x, 1, 0, 0), "ryd")
    assert seq_copy.is_parametrized()
    with pytest.raises(ValueError, match="needs to be built"):
        Simulation(seq_copy)

    layout = RegisterLayout([[0, 0], [10, 10]])
    mapp_reg = layout.make_mappable_register(1)
    seq_ = Sequence(mapp_reg, Chadoq2)
    assert seq_.is_register_mappable() and not seq_.is_parametrized()
    with pytest.raises(ValueError, match="needs to be built"):
        Simulation(seq_)


def test_extraction_of_sequences(seq):
    sim = Simulation(seq)
    for channel in seq.declared_channels:
        addr = seq.declared_channels[channel].addressing
        basis = seq.declared_channels[channel].basis

        if addr == "Global":
            for slot in seq._schedule[channel]:
                if isinstance(slot.type, Pulse):
                    samples = sim.samples[addr][basis]
                    assert (
                        samples["amp"][slot.ti : slot.tf]
                        == slot.type.amplitude.samples
                    ).all()
                    assert (
                        samples["det"][slot.ti : slot.tf]
                        == slot.type.detuning.samples
                    ).all()
                    assert (
                        samples["phase"][slot.ti : slot.tf] == slot.type.phase
                    ).all()

        elif addr == "Local":
            for slot in seq._schedule[channel]:
                if isinstance(slot.type, Pulse):
                    for qubit in slot.targets:  # TO DO: multiaddressing??
                        samples = sim.samples[addr][basis][qubit]
                        assert (
                            samples["amp"][slot.ti : slot.tf]
                            == slot.type.amplitude.samples
                        ).all()
                        assert (
                            samples["det"][slot.ti : slot.tf]
                            == slot.type.detuning.samples
                        ).all()
                        assert (
                            samples["phase"][slot.ti : slot.tf]
                            == slot.type.phase
                        ).all()


def test_building_basis_and_projection_operators(seq, reg):
    # All three levels:
    sim = Simulation(seq, sampling_rate=0.01)
    assert sim.basis_name == "all"
    assert sim.dim == 3
    assert sim.basis == {
        "r": qutip.basis(3, 0),
        "g": qutip.basis(3, 1),
        "h": qutip.basis(3, 2),
    }
    assert (
        sim.op_matrix["sigma_rr"]
        == qutip.basis(3, 0) * qutip.basis(3, 0).dag()
    )
    assert (
        sim.op_matrix["sigma_gr"]
        == qutip.basis(3, 1) * qutip.basis(3, 0).dag()
    )
    assert (
        sim.op_matrix["sigma_hg"]
        == qutip.basis(3, 2) * qutip.basis(3, 1).dag()
    )

    # Check local operator building method:
    with pytest.raises(ValueError, match="Duplicate atom"):
        sim.build_operator([("sigma_gg", ["target", "target"])])
    with pytest.raises(ValueError, match="not a valid operator"):
        sim.build_operator([("wrong", ["target"])])
    with pytest.raises(ValueError, match="Invalid qubit names: {'wrong'}"):
        sim.build_operator([("sigma_gg", ["wrong"])])

    # Check building operator with one operator
    op_standard = sim.build_operator([("sigma_gg", ["target"])])
    op_one = sim.build_operator(("sigma_gg", ["target"]))
    assert np.linalg.norm(op_standard - op_one) < 1e-10

    # Global ground-rydberg
    seq2 = Sequence(reg, Chadoq2)
    seq2.declare_channel("global", "rydberg_global")
    pi_pls = Pulse.ConstantDetuning(BlackmanWaveform(1000, np.pi), 0.0, 0)
    seq2.add(pi_pls, "global")
    sim2 = Simulation(seq2, sampling_rate=0.01)
    assert sim2.basis_name == "ground-rydberg"
    assert sim2.dim == 2
    assert sim2.basis == {"r": qutip.basis(2, 0), "g": qutip.basis(2, 1)}
    assert (
        sim2.op_matrix["sigma_rr"]
        == qutip.basis(2, 0) * qutip.basis(2, 0).dag()
    )
    assert (
        sim2.op_matrix["sigma_gr"]
        == qutip.basis(2, 1) * qutip.basis(2, 0).dag()
    )

    # Digital
    seq2b = Sequence(reg, Chadoq2)
    seq2b.declare_channel("local", "raman_local", "target")
    seq2b.add(pi_pls, "local")
    sim2b = Simulation(seq2b, sampling_rate=0.01)
    assert sim2b.basis_name == "digital"
    assert sim2b.dim == 2
    assert sim2b.basis == {"g": qutip.basis(2, 0), "h": qutip.basis(2, 1)}
    assert (
        sim2b.op_matrix["sigma_gg"]
        == qutip.basis(2, 0) * qutip.basis(2, 0).dag()
    )
    assert (
        sim2b.op_matrix["sigma_hg"]
        == qutip.basis(2, 1) * qutip.basis(2, 0).dag()
    )

    # Local ground-rydberg
    seq2c = Sequence(reg, Chadoq2)
    seq2c.declare_channel("local_ryd", "rydberg_local", "target")
    seq2c.add(pi_pls, "local_ryd")
    sim2c = Simulation(seq2c, sampling_rate=0.01)
    assert sim2c.basis_name == "ground-rydberg"
    assert sim2c.dim == 2
    assert sim2c.basis == {"r": qutip.basis(2, 0), "g": qutip.basis(2, 1)}
    assert (
        sim2c.op_matrix["sigma_rr"]
        == qutip.basis(2, 0) * qutip.basis(2, 0).dag()
    )
    assert (
        sim2c.op_matrix["sigma_gr"]
        == qutip.basis(2, 1) * qutip.basis(2, 0).dag()
    )

    # Global XY
    seq2 = Sequence(reg, MockDevice)
    seq2.declare_channel("global", "mw_global")
    seq2.add(pi_pls, "global")
    # seq2 cannot be run on Chadoq2 because it does not support mw
    with pytest.raises(
        ValueError,
        match="Bases used in samples should be supported by device.",
    ):
        QutipEmulator(sampler.sample(seq2), seq2.register, Chadoq2)
    sim2 = Simulation(seq2, sampling_rate=0.01)
    assert sim2.basis_name == "XY"
    assert sim2.dim == 2
    assert sim2.basis == {"u": qutip.basis(2, 0), "d": qutip.basis(2, 1)}
    assert (
        sim2.op_matrix["sigma_uu"]
        == qutip.basis(2, 0) * qutip.basis(2, 0).dag()
    )
    assert (
        sim2.op_matrix["sigma_du"]
        == qutip.basis(2, 1) * qutip.basis(2, 0).dag()
    )
    assert (
        sim2.op_matrix["sigma_ud"]
        == qutip.basis(2, 0) * qutip.basis(2, 1).dag()
    )


def test_empty_sequences(reg):
    seq = Sequence(reg, MockDevice)
    with pytest.raises(ValueError, match="no declared channels"):
        Simulation(seq)
    seq.declare_channel("ch0", "mw_global")
    with pytest.raises(ValueError, match="No instructions given"):
        Simulation(seq)
    with pytest.raises(ValueError, match="SequenceSamples is empty"):
        QutipEmulator(sampler.sample(seq), seq.register, seq.device)

    seq = Sequence(reg, MockDevice)
    seq.declare_channel("test", "rydberg_local", "target")
    seq.declare_channel("test2", "rydberg_global")
    with pytest.raises(ValueError, match="No instructions given"):
        Simulation(seq)


def test_get_hamiltonian():
    simple_reg = Register.from_coordinates([[10, 0], [0, 0]], prefix="atom")
    detun = 1.0
    rise = Pulse.ConstantDetuning(RampWaveform(1500, 0.0, 2.0), detun, 0.0)
    simple_seq = Sequence(simple_reg, Chadoq2)
    simple_seq.declare_channel("ising", "rydberg_global")
    simple_seq.add(rise, "ising")

    simple_sim = Simulation(simple_seq, sampling_rate=0.01)
    with pytest.raises(ValueError, match="less than or equal to"):
        simple_sim.get_hamiltonian(1650)
    with pytest.raises(ValueError, match="greater than or equal to"):
        simple_sim.get_hamiltonian(-10)
    # Constant detuning, so |rr><rr| term is C_6/r^6 - 2*detuning for any time
    simple_ham = simple_sim.get_hamiltonian(143)
    assert np.isclose(
        simple_ham[0, 0], Chadoq2.interaction_coeff / 10**6 - 2 * detun
    )

    np.random.seed(123)
    simple_sim_noise = Simulation(
        simple_seq, config=SimConfig(noise="doppler", temperature=20000)
    )
    simple_ham_noise = simple_sim_noise.get_hamiltonian(144)
    assert np.isclose(
        simple_ham_noise.full(),
        np.array(
            [
                [
                    4.47984523 + 0.0j,
                    0.09606404 + 0.0j,
                    0.09606404 + 0.0j,
                    0.0 + 0.0j,
                ],
                [
                    0.09606404 + 0.0j,
                    12.03082372 + 0.0j,
                    0.0 + 0.0j,
                    0.09606404 + 0.0j,
                ],
                [
                    0.09606404 + 0.0j,
                    0.0 + 0.0j,
                    -12.97113702 + 0.0j,
                    0.09606404 + 0.0j,
                ],
                [0.0 + 0.0j, 0.09606404 + 0.0j, 0.09606404 + 0.0j, 0.0 + 0.0j],
            ]
        ),
    ).all()


def test_single_atom_simulation():
    one_reg = Register.from_coordinates([(0, 0)], "atom")
    one_seq = Sequence(one_reg, Chadoq2)
    one_seq.declare_channel("ch0", "rydberg_global")
    one_seq.add(
        Pulse.ConstantDetuning(ConstantWaveform(16, 1.0), 1.0, 0), "ch0"
    )
    one_sim = Simulation(one_seq)
    one_res = one_sim.run()
    assert one_res._size == one_sim._size
    one_sim = Simulation(one_seq, evaluation_times="Minimal")
    one_resb = one_sim.run()
    assert one_resb._size == one_sim._size


def test_add_max_step_and_delays():
    reg = Register.from_coordinates([(0, 0)])
    seq = Sequence(reg, Chadoq2)
    seq.declare_channel("ch", "rydberg_global")
    seq.delay(1500, "ch")
    seq.add(Pulse.ConstantDetuning(BlackmanWaveform(600, np.pi), 0, 0), "ch")
    seq.delay(2000, "ch")
    seq.add(
        Pulse.ConstantDetuning(BlackmanWaveform(600, np.pi / 2), 0, 0), "ch"
    )
    sim = Simulation(seq)
    res_large_max_step = sim.run(max_step=1)
    res_auto_max_step = sim.run()
    r = qutip.basis(2, 0)
    occ_large = res_large_max_step.expect([r.proj()])[0]
    occ_auto = res_auto_max_step.expect([r.proj()])[0]
    assert np.isclose(occ_large[-1], 0, 1e-4)
    assert np.isclose(occ_auto[-1], 0.5, 1e-4)


def test_run(seq, patch_plt_show):
    sim = Simulation(seq, sampling_rate=0.01)
    sim.set_config(SimConfig("SPAM", eta=0.0))
    with patch("matplotlib.pyplot.savefig"):
        sim.draw(draw_phase_area=True, fig_name="my_fig.pdf")
    bad_initial = np.array([1.0])
    good_initial_array = np.r_[1, np.zeros(sim.dim**sim._size - 1)]
    good_initial_qobj = qutip.tensor(
        [qutip.basis(sim.dim, 0) for _ in range(sim._size)]
    )
    good_initial_qobj_no_dims = qutip.basis(sim.dim**sim._size, 2)

    with pytest.raises(
        ValueError, match="Incompatible shape of initial state"
    ):
        sim.set_initial_state(bad_initial)

    with pytest.raises(
        ValueError, match="Incompatible shape of initial state"
    ):
        sim.set_initial_state(qutip.Qobj(bad_initial))

    with pytest.warns(
        DeprecationWarning, match="Setting `initial_state` is deprecated"
    ):
        sim.initial_state = good_initial_array

    sim.set_initial_state(good_initial_array)
    sim.run()
    sim.set_initial_state(good_initial_qobj)
    sim.run()
    sim.set_initial_state(good_initial_qobj_no_dims)
    sim.run()
    seq.measure("ground-rydberg")
    sim.run()
    assert sim._seq._measurement == "ground-rydberg"

    sim.run(progress_bar=True)
    sim.run(progress_bar=False)
    sim.run(progress_bar=None)
    with pytest.raises(
        ValueError,
        match="`progress_bar` must be a bool.",
    ):
        sim.run(progress_bar=1)

    sim.set_config(SimConfig("SPAM", eta=0.1))
    with pytest.raises(
        NotImplementedError,
        match="Can't combine state preparation errors with an initial state "
        "different from the ground.",
    ):
        sim.run()


def test_eval_times(seq):
    with pytest.raises(
        ValueError, match="evaluation_times float must be between 0 " "and 1."
    ):
        sim = Simulation(seq, sampling_rate=1.0)
        sim.set_evaluation_times(3.0)
    with pytest.raises(ValueError, match="Wrong evaluation time label."):
        sim = Simulation(seq, sampling_rate=1.0)
        sim.set_evaluation_times(123)
    with pytest.raises(ValueError, match="Wrong evaluation time label."):
        sim = Simulation(seq, sampling_rate=1.0)
        sim.set_evaluation_times("Best")

    with pytest.raises(
        ValueError,
        match="Provided evaluation-time list contains " "negative values.",
    ):
        sim = Simulation(seq, sampling_rate=1.0)
        sim.set_evaluation_times([-1, 0, sim.sampling_times[-2]])

    with pytest.raises(
        ValueError,
        match="Provided evaluation-time list extends "
        "further than sequence duration.",
    ):
        sim = Simulation(seq, sampling_rate=1.0)
        sim.set_evaluation_times([0, sim.sampling_times[-1] + 10])

    sim = Simulation(seq, sampling_rate=1.0)
    with pytest.warns(
        DeprecationWarning, match="Setting `evaluation_times` is deprecated"
    ):
        sim.evaluation_times = "Full"
    sim.set_evaluation_times("Full")
    assert sim._eval_times_instruction == "Full"
    np.testing.assert_almost_equal(
        sim._eval_times_array,
        sim.sampling_times,
    )

    sim = Simulation(seq, sampling_rate=1.0)
    sim.set_evaluation_times("Minimal")
    np.testing.assert_almost_equal(
        sim._eval_times_array,
        np.array([sim.sampling_times[0], sim._tot_duration / 1000]),
    )

    sim = Simulation(seq, sampling_rate=1.0)
    sim.set_evaluation_times(
        [
            0,
            sim.sampling_times[-3],
            sim._tot_duration / 1000,
        ]
    )
    np.testing.assert_almost_equal(
        sim._eval_times_array,
        np.array([0, sim.sampling_times[-3], sim._tot_duration / 1000]),
    )

    sim.set_evaluation_times([])
    np.testing.assert_almost_equal(
        sim._eval_times_array,
        np.array([0, sim._tot_duration / 1000]),
    )

    sim.set_evaluation_times(0.0001)
    np.testing.assert_almost_equal(
        sim._eval_times_array,
        np.array([0, sim._tot_duration / 1000]),
    )

    sim = Simulation(seq, sampling_rate=1.0)
    sim.set_evaluation_times([sim.sampling_times[-10], sim.sampling_times[-3]])
    np.testing.assert_almost_equal(
        sim._eval_times_array,
        np.array(
            [
                0,
                sim.sampling_times[-10],
                sim.sampling_times[-3],
                sim._tot_duration / 1000,
            ]
        ),
    )

    sim = Simulation(seq, sampling_rate=1.0)
    sim.set_evaluation_times(0.4)
    np.testing.assert_almost_equal(
        sim.sampling_times[
            np.linspace(
                0,
                len(sim.sampling_times) - 1,
                int(0.4 * len(sim.sampling_times)),
                dtype=int,
            )
        ],
        sim._eval_times_array,
    )


def test_config():
    np.random.seed(123)
    reg = Register.from_coordinates([(0, 0), (0, 5)], prefix="q")
    seq = Sequence(reg, Chadoq2)
    seq.declare_channel("ch0", "rydberg_global")
    duration = 2500
    pulse = Pulse.ConstantPulse(duration, np.pi, 0.0 * 2 * np.pi, 0)
    seq.add(pulse, "ch0")
    sim = Simulation(seq, config=SimConfig(noise="SPAM"))
    sim.reset_config()
    assert sim.config == SimConfig()
    sim.show_config()
    with pytest.raises(ValueError, match="not a valid"):
        sim.set_config("bad_config")
    clean_ham = sim.get_hamiltonian(123)
    new_cfg = SimConfig(noise="doppler", temperature=10000)
    sim.set_config(new_cfg)
    assert sim.config == new_cfg
    noisy_ham = sim.get_hamiltonian(123)
    assert (
        noisy_ham[0, 0] != clean_ham[0, 0]
        and noisy_ham[3, 3] == clean_ham[3, 3]
    )
    sim.set_config(SimConfig(noise="amplitude"))
    noisy_amp_ham = sim.get_hamiltonian(123)
    assert (
        noisy_amp_ham[0, 0] == clean_ham[0, 0]
        and noisy_amp_ham[0, 1] != clean_ham[0, 1]
    )


def test_noise(seq, matrices):
    np.random.seed(3)
    sim2 = Simulation(
        seq, sampling_rate=0.01, config=SimConfig(noise=("SPAM"), eta=0.9)
    )
    assert sim2.run().sample_final_state() == Counter(
        {"000": 857, "110": 73, "100": 70}
    )
    with pytest.raises(NotImplementedError, match="Cannot include"):
        sim2.set_config(SimConfig(noise="dephasing"))
    with pytest.raises(NotImplementedError, match="Cannot include"):
        sim2.set_config(SimConfig(noise="depolarizing"))
    with pytest.raises(NotImplementedError, match="Cannot include"):
        sim2.set_config(
            SimConfig(
                noise="eff_noise",
                eff_noise_opers=[matrices["I"]],
                eff_noise_probs=[1.0],
            )
        )
    assert sim2.config.spam_dict == {
        "eta": 0.9,
        "epsilon": 0.01,
        "epsilon_prime": 0.05,
    }
    assert sim2.samples["Global"] == {}
    assert any(sim2._bad_atoms.values())
    for basis in ("ground-rydberg", "digital"):
        for t in sim2._bad_atoms:
            if not sim2._bad_atoms[t]:
                continue
            for qty in ("amp", "det", "phase"):
                assert np.all(sim2.samples["Local"][basis][t][qty] == 0.0)


def test_dephasing():
    np.random.seed(123)
    reg = Register.from_coordinates([(0, 0)], prefix="q")
    seq = Sequence(reg, Chadoq2)
    seq.declare_channel("ch0", "rydberg_global")
    duration = 2500
    pulse = Pulse.ConstantPulse(duration, np.pi, 0, 0)
    seq.add(pulse, "ch0")
    sim = Simulation(
        seq, sampling_rate=0.01, config=SimConfig(noise="dephasing")
    )
    assert sim.run().sample_final_state() == Counter({"0": 595, "1": 405})
    assert len(sim._collapse_ops) != 0
    with pytest.warns(UserWarning, match="first-order"):
        reg = Register.from_coordinates([(0, 0), (0, 10)], prefix="q")
        seq2 = Sequence(reg, Chadoq2)
        seq2.declare_channel("ch0", "rydberg_global")
        seq2.add(pulse, "ch0")
        sim = Simulation(
            seq2,
            sampling_rate=0.01,
            config=SimConfig(noise="dephasing", dephasing_prob=0.5),
        )


def test_depolarizing():
    np.random.seed(123)
    reg = Register.from_coordinates([(0, 0)], prefix="q")
    seq = Sequence(reg, Chadoq2)
    seq.declare_channel("ch0", "rydberg_global")
    duration = 2500
    pulse = Pulse.ConstantPulse(duration, np.pi, 0, 0)
    seq.add(pulse, "ch0")
    sim = Simulation(
        seq, sampling_rate=0.01, config=SimConfig(noise="depolarizing")
    )
    assert sim.run().sample_final_state() == Counter({"0": 587, "1": 413})
    trace_2 = sim.run().states[-1] ** 2
    assert np.trace(trace_2) < 1 and not np.isclose(np.trace(trace_2), 1)
    assert len(sim._collapse_ops) != 0
    with pytest.warns(UserWarning, match="first-order"):
        reg = Register.from_coordinates([(0, 0), (0, 10)], prefix="q")
        seq2 = Sequence(reg, Chadoq2)
        seq2.declare_channel("ch0", "rydberg_global")
        seq2.add(pulse, "ch0")
        sim = Simulation(
            seq2,
            sampling_rate=0.01,
            config=SimConfig(noise="depolarizing", depolarizing_prob=0.5),
        )


def test_eff_noise(matrices):
    np.random.seed(123)
    reg = Register.from_coordinates([(0, 0)], prefix="q")
    seq = Sequence(reg, Chadoq2)
    seq.declare_channel("ch0", "rydberg_global")
    duration = 2500
    pulse = Pulse.ConstantPulse(duration, np.pi, 0, 0)
    seq.add(pulse, "ch0")
    sim = Simulation(
        seq,
        sampling_rate=0.01,
        config=SimConfig(
            noise="eff_noise",
            eff_noise_opers=[matrices["I"], matrices["Z"]],
            eff_noise_probs=[0.975, 0.025],
        ),
    )
    sim_dph = Simulation(
        seq, sampling_rate=0.01, config=SimConfig(noise="dephasing")
    )
    assert (
        sim._collapse_ops == sim_dph._collapse_ops
        and sim.run().states[-1] == sim_dph.run().states[-1]
    )
    assert len(sim._collapse_ops) != 0
    with pytest.warns(UserWarning, match="first-order"):
        reg = Register.from_coordinates([(0, 0), (0, 10)], prefix="q")
        seq2 = Sequence(reg, Chadoq2)
        seq2.declare_channel("ch0", "rydberg_global")
        seq2.add(pulse, "ch0")
        sim = Simulation(
            seq2,
            sampling_rate=0.01,
            config=SimConfig(
                noise="eff_noise",
                eff_noise_opers=[matrices["I"], matrices["Z"]],
                eff_noise_probs=[0.5, 0.5],
            ),
        )


def test_add_config(matrices):
    reg = Register.from_coordinates([(0, 0)], prefix="q")
    seq = Sequence(reg, Chadoq2)
    seq.declare_channel("ch0", "rydberg_global")
    duration = 2500
    pulse = Pulse.ConstantPulse(duration, np.pi, 0.0 * 2 * np.pi, 0)
    seq.add(pulse, "ch0")
    sim = Simulation(
        seq, sampling_rate=0.01, config=SimConfig(noise="SPAM", eta=0.5)
    )
    with pytest.raises(ValueError, match="is not a valid"):
        sim.add_config("bad_cfg")
    sim.add_config(
        SimConfig(
            noise=(
                "SPAM",
                "doppler",
                "eff_noise",
            ),
            eff_noise_opers=[matrices["I"], matrices["X"]],
            eff_noise_probs=[0.4, 0.6],
            temperature=20000,
        )
    )
    assert (
        "doppler" in sim.config.noise
        and "SPAM" in sim.config.noise
        and "eff_noise" in sim.config.noise
    )
    assert sim.config.eta == 0.5
    assert sim.config.temperature == 20000.0e-6
    sim.set_config(SimConfig(noise="doppler", laser_waist=175.0))
    sim.add_config(
        SimConfig(noise=("SPAM", "amplitude", "dephasing"), laser_waist=172.0)
    )
    assert (
        "amplitude" in sim.config.noise
        and "dephasing" in sim.config.noise
        and "SPAM" in sim.config.noise
    )
    assert sim.config.laser_waist == 172.0
    sim.set_config(SimConfig(noise="SPAM", eta=0.5))
    sim.add_config(SimConfig(noise="depolarizing"))
    assert "depolarizing" in sim.config.noise


def test_concurrent_pulses():
    reg = Register({"q0": (0, 0)})
    seq = Sequence(reg, Chadoq2)

    seq.declare_channel("ch_local", "rydberg_local", initial_target="q0")
    seq.declare_channel("ch_global", "rydberg_global")

    pulse = Pulse.ConstantPulse(20, 10, 0, 0)

    seq.add(pulse, "ch_local")
    seq.add(pulse, "ch_global", protocol="no-delay")

    # Clean simulation
    sim_no_noise = Simulation(seq)

    # Noisy simulation
    sim_with_noise = Simulation(seq)
    config_doppler = SimConfig(noise=("doppler"))
    sim_with_noise.set_config(config_doppler)

    for t in sim_no_noise.evaluation_times:
        ham_no_noise = sim_no_noise.get_hamiltonian(t)
        ham_with_noise = sim_with_noise.get_hamiltonian(t)
        assert ham_no_noise[0, 1] == ham_with_noise[0, 1]


def test_get_xy_hamiltonian():
    simple_reg = Register.from_coordinates(
        [[0, 10], [10, 0], [0, 0]], prefix="atom"
    )
    detun = 1.0
    amp = 3.0
    rise = Pulse.ConstantPulse(1500, amp, detun, 0.0)
    simple_seq = Sequence(simple_reg, MockDevice)
    simple_seq.declare_channel("ch0", "mw_global")
    simple_seq.set_magnetic_field(0, 1.0, 0.0)
    simple_seq.add(rise, "ch0")

    assert np.isclose(np.linalg.norm(simple_seq.magnetic_field[0:2]), 1)

    simple_sim = Simulation(simple_seq, sampling_rate=0.03)
    with pytest.raises(
        ValueError, match="less than or equal to the sequence duration"
    ):
        simple_sim.get_hamiltonian(1650)
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        simple_sim.get_hamiltonian(-10)
    # Constant detuning, so |ud><du| term is C_3/r^3 - 2*detuning for any time
    simple_ham = simple_sim.get_hamiltonian(143)
    assert simple_ham[1, 2] == 0.5 * MockDevice.interaction_coeff_xy / 10**3
    assert (
        np.abs(
            simple_ham[1, 4]
            - (-2 * 0.5 * MockDevice.interaction_coeff_xy / 10**3)
        )
        < 1e-10
    )
    assert simple_ham[0, 1] == 0.5 * amp
    assert simple_ham[3, 3] == -2 * detun


def test_run_xy():
    simple_reg = Register.from_coordinates([[10, 0], [0, 0]], prefix="atom")
    detun = 1.0
    amp = 3.0
    rise = Pulse.ConstantPulse(1500, amp, detun, 0.0)
    simple_seq = Sequence(simple_reg, MockDevice)
    simple_seq.declare_channel("ch0", "mw_global")
    simple_seq.add(rise, "ch0")

    sim = Simulation(simple_seq, sampling_rate=0.01)

    good_initial_array = np.r_[1, np.zeros(sim.dim**sim._size - 1)]
    good_initial_qobj = qutip.tensor(
        [qutip.basis(sim.dim, 0) for _ in range(sim._size)]
    )
    sim.set_initial_state(good_initial_array)
    assert sim.initial_state == good_initial_qobj
    sim.run()
    sim.set_initial_state(good_initial_qobj)
    sim.run()

    assert not hasattr(sim._seq, "_measurement")
    simple_seq.measure(basis="XY")
    sim.run()
    assert sim._seq._measurement == "XY"


def test_noisy_xy():
    np.random.seed(15092021)
    simple_reg = Register.square(2, prefix="atom")
    detun = 1.0
    amp = 3.0
    rise = Pulse.ConstantPulse(1500, amp, detun, 0.0)
    simple_seq = Sequence(simple_reg, MockDevice)
    simple_seq.declare_channel("ch0", "mw_global")
    simple_seq.add(rise, "ch0")

    sim = Simulation(simple_seq, sampling_rate=0.01)
    with pytest.raises(
        NotImplementedError, match="mode 'XY' does not support simulation of"
    ):
        sim.set_config(SimConfig(("SPAM", "doppler")))

    sim.set_config(SimConfig("SPAM", eta=0.4))
    assert sim._bad_atoms == {
        "atom0": True,
        "atom1": False,
        "atom2": True,
        "atom3": False,
    }
    with pytest.raises(
        NotImplementedError, match="simulation of noise types: amplitude"
    ):
        sim.add_config(SimConfig("amplitude"))


def test_mask_nopulses():
    """Check interaction between SLM mask and a simulation with no pulses."""
    reg = Register({"q0": (0, 0), "q1": (10, 10), "q2": (-10, -10)})
    for channel_type in ["mw_global", "rydberg_global"]:
        seq_empty = Sequence(reg, MockDevice)
        if channel_type == "mw_global":
            seq_empty.set_magnetic_field(0, 1.0, 0.0)
        seq_empty.declare_channel("ch", channel_type)
        seq_empty.delay(duration=100, channel="ch")
        masked_qubits = ["q2"]
        seq_empty.config_slm_mask(masked_qubits)
        sim_empty = Simulation(seq_empty)

        assert seq_empty._slm_mask_time == []
        assert sim_empty._seq._slm_mask_time == []


def test_mask_equals_remove():
    """Check that masking is equivalent to removing the masked qubits.

    A global pulse acting on three qubits of which one is masked, should be
    equivalent to acting on a register with only the two unmasked qubits.
    """
    reg_three = Register({"q0": (0, 0), "q1": (10, 10), "q2": (-10, -10)})
    reg_two = Register({"q0": (0, 0), "q1": (10, 10)})
    pulse = Pulse.ConstantPulse(100, 10, 0, 0)
    local_pulse = Pulse.ConstantPulse(200, 10, 0, 0)

    for channel_type in ["mw_global", "rydberg_global", "raman_global"]:
        # Masked simulation
        seq_masked = Sequence(reg_three, MockDevice)
        if channel_type == "mw_global":
            seq_masked.set_magnetic_field(0, 1.0, 0.0)
        else:
            # Add a local channel acting on a masked qubit (has no effect)
            seq_masked.declare_channel(
                "local",
                channel_type[: -len("global")] + "local",
                initial_target="q2",
            )
            seq_masked.add(local_pulse, "local")
        seq_masked.declare_channel("ch_masked", channel_type)
        masked_qubits = ["q2"]
        seq_masked.config_slm_mask(masked_qubits)
        seq_masked.add(pulse, "ch_masked")
        sim_masked = Simulation(seq_masked)
        # Simulation cannot be run on a device not having an SLM mask
        with pytest.raises(
            ValueError,
            match="Samples use SLM mask but device does not have one.",
        ):
            QutipEmulator(sampler.sample(seq_masked), reg_three, IroiseMVP)
        # Simulation cannot be run on a register not defining "q2"
        with pytest.raises(
            ValueError,
            match="The ids of qubits targeted in SLM mask",
        ):
            QutipEmulator(sampler.sample(seq_masked), reg_two, MockDevice)
        # Simulation on reduced register
        seq_two = Sequence(reg_two, MockDevice)
        if channel_type == "mw_global":
            seq_two.set_magnetic_field(0, 1.0, 0.0)
        seq_two.declare_channel("ch_two", channel_type)
        if channel_type != "mw_global":
            seq_two.delay(local_pulse.duration, "ch_two")
        seq_two.add(pulse, "ch_two")
        sim_two = Simulation(seq_two)

        # Check equality
        for t in sim_two.sampling_times:
            ham_masked = sim_masked.get_hamiltonian(t)
            ham_two = sim_two.get_hamiltonian(t)
            assert ham_masked == qutip.tensor(ham_two, qutip.qeye(2))


def test_mask_two_pulses():
    """Similar to test_mask_equals_remove, but with more pulses afterwards.

    Three global pulses act on a three qubit register, with one qubit masked
    during the first pulse.
    """
    reg_three = Register({"q0": (0, 0), "q1": (10, 10), "q2": (-10, -10)})
    reg_two = Register({"q0": (0, 0), "q1": (10, 10)})
    pulse = Pulse.ConstantPulse(100, 10, 0, 0)
    no_pulse = Pulse.ConstantPulse(100, 0, 0, 0)

    for channel_type in ["mw_global", "rydberg_global", "raman_global"]:
        # Masked simulation
        seq_masked = Sequence(reg_three, MockDevice)
        seq_masked.declare_channel("ch_masked", channel_type)
        masked_qubits = ["q2"]
        seq_masked.config_slm_mask(masked_qubits)
        seq_masked.add(pulse, "ch_masked")  # First pulse: masked
        seq_masked.add(pulse, "ch_masked")  # Second pulse: unmasked
        seq_masked.add(pulse, "ch_masked")  # Third pulse: unmasked
        sim_masked = Simulation(seq_masked)

        # Unmasked simulation on full register
        seq_three = Sequence(reg_three, MockDevice)
        seq_three.declare_channel("ch_three", channel_type)
        seq_three.add(no_pulse, "ch_three")
        seq_three.add(pulse, "ch_three")
        seq_three.add(pulse, "ch_three")
        sim_three = Simulation(seq_three)

        # Unmasked simulation on reduced register
        seq_two = Sequence(reg_two, MockDevice)
        seq_two.declare_channel("ch_two", channel_type)
        seq_two.add(pulse, "ch_two")
        seq_two.add(no_pulse, "ch_two")
        seq_two.add(no_pulse, "ch_two")
        sim_two = Simulation(seq_two)

        ti = seq_masked._slm_mask_time[0]
        tf = seq_masked._slm_mask_time[1]
        for t in sim_masked.sampling_times:
            ham_masked = sim_masked.get_hamiltonian(t)
            ham_three = sim_three.get_hamiltonian(t)
            ham_two = sim_two.get_hamiltonian(t)
            if ti <= t <= tf:
                assert ham_masked == qutip.tensor(ham_two, qutip.qeye(2))
            else:
                assert ham_masked == ham_three


def test_mask_local_channel():
    seq_ = Sequence(Register.square(2, prefix="q"), MockDevice)
    seq_.declare_channel("rydberg_global", "rydberg_global")
    pulse = Pulse.ConstantPulse(1000, 10, 0, 0)
    seq_.config_slm_mask(["q0", "q3"])
    seq_.add(pulse, "rydberg_global")

    seq_.declare_channel("raman_local", "raman_local", initial_target="q0")
    pulse2 = Pulse.ConstantPulse(1000, 10, -5, np.pi)
    seq_.add(pulse2, "raman_local", protocol="no-delay")

    assert seq_._slm_mask_time == [0, 1000]
    assert seq_._slm_mask_targets == {"q0", "q3"}

    sim = Simulation(seq_)
    for qty in ("amp", "det", "phase"):
        assert np.all(sim.samples["Local"]["digital"]["q0"][qty] == 0.0)
    assert "q3" not in sim.samples["Local"]["digital"]


def test_effective_size_intersection():
    simple_reg = Register.square(2, prefix="atom")
    rise = Pulse.ConstantPulse(1500, 0, 0, 0)
    for channel_type in ["mw_global", "rydberg_global"]:
        np.random.seed(15092021)
        seq = Sequence(simple_reg, MockDevice)
        seq.declare_channel("ch0", channel_type)
        seq.add(rise, "ch0")
        seq.config_slm_mask(["atom0"])

        sim = Simulation(seq, sampling_rate=0.01)
        sim.set_config(SimConfig("SPAM", eta=0.4))
        assert sim._bad_atoms == {
            "atom0": True,
            "atom1": False,
            "atom2": True,
            "atom3": False,
        }
        assert sim.get_hamiltonian(0) != 0 * sim.build_operator(
            [("I", "global")]
        )


def test_effective_size_disjoint():
    simple_reg = Register.square(2, prefix="atom")
    rise = Pulse.ConstantPulse(1500, 0, 0, 0)
    for channel_type in ["mw_global", "rydberg_global", "raman_global"]:
        np.random.seed(15092021)
        seq = Sequence(simple_reg, MockDevice)
        seq.declare_channel("ch0", channel_type)
        seq.add(rise, "ch0")
        seq.config_slm_mask(["atom1"])
        sim = Simulation(seq, sampling_rate=0.01)
        sim.set_config(SimConfig("SPAM", eta=0.4))
        assert sim._bad_atoms == {
            "atom0": True,
            "atom1": False,
            "atom2": True,
            "atom3": False,
        }
        assert sim.get_hamiltonian(0) == 0 * sim.build_operator(
            [("I", "global")]
        )


def test_simulation_with_modulation(mod_device, reg, patch_plt_show):
    seq = Sequence(reg, mod_device)
    seq.declare_channel("ch0", "rydberg_global")
    seq.config_slm_mask({"control1"})
    pulse1 = Pulse.ConstantPulse(120, 1, 0, 2.0)
    seq.add(pulse1, "ch0")

    with pytest.raises(
        NotImplementedError,
        match="Simulation of sequences combining an SLM mask and output "
        "modulation is not supported.",
    ):
        Simulation(seq, with_modulation=True)

    seq = Sequence(reg, mod_device)
    seq.declare_channel("ch0", "rydberg_global")
    seq.declare_channel("ch1", "raman_local", initial_target="target")
    seq.add(pulse1, "ch1")
    seq.target("control1", "ch1")
    seq.add(pulse1, "ch1")
    seq.add(pulse1, "ch0")
    ch1_obj = seq.declared_channels["ch1"]
    pulse1_mod_samples = ch1_obj.modulate(pulse1.amplitude.samples)
    mod_dt = pulse1.duration + pulse1.fall_time(ch1_obj)
    assert pulse1_mod_samples.size == mod_dt

    sim_config = SimConfig(("amplitude", "doppler"))
    sim = Simulation(seq, with_modulation=True, config=sim_config)

    assert sim.samples["Global"] == {}  # All samples stored in local
    raman_samples = sim.samples["Local"]["digital"]
    # Local pulses
    for qid, time_slice in [
        ("target", slice(0, mod_dt)),
        ("control1", slice(mod_dt, 2 * mod_dt)),
    ]:
        np.testing.assert_allclose(
            raman_samples[qid]["amp"][time_slice],
            pulse1_mod_samples,
            atol=1e-2,
        )
        np.testing.assert_equal(
            raman_samples[qid]["det"][time_slice], sim._doppler_detune[qid]
        )
        np.testing.assert_allclose(
            raman_samples[qid]["phase"][time_slice], pulse1.phase
        )

    def pos_factor(qid):
        r = np.linalg.norm(reg.qubits[qid])
        w0 = sim_config.laser_waist
        return np.exp(-((r / w0) ** 2))

    # Global pulse
    time_slice = slice(2 * mod_dt, 3 * mod_dt)
    rydberg_samples = sim.samples["Local"]["ground-rydberg"]
    noise_amp_base = rydberg_samples["target"]["amp"][time_slice] / (
        pulse1_mod_samples * pos_factor("target")
    )
    for qid in reg.qubit_ids:
        np.testing.assert_allclose(
            rydberg_samples[qid]["amp"][time_slice],
            pulse1_mod_samples * noise_amp_base * pos_factor(qid),
        )
        np.testing.assert_equal(
            rydberg_samples[qid]["det"][time_slice], sim._doppler_detune[qid]
        )
        np.testing.assert_allclose(
            rydberg_samples[qid]["phase"][time_slice], pulse1.phase
        )

    with pytest.raises(
        ValueError,
        match="Can't draw the interpolation points when the sequence "
        "is modulated",
    ):
        sim.draw(draw_interp_pts=True)

    # Drawing with modulation
    sim.draw()
