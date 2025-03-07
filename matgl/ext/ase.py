"""Interfaces to the Atomic Simulation Environment package for dynamic simulations."""

from __future__ import annotations

import collections
import contextlib
import io
import pickle
import sys
from enum import Enum
from typing import TYPE_CHECKING, Literal

import ase.optimize as opt
import numpy as np
import pandas as pd
import scipy.sparse as sp
import torch
from ase import Atoms, units
from ase.calculators.calculator import Calculator, all_changes
from ase.constraints import ExpCellFilter
from ase.md import Langevin
from ase.md.andersen import Andersen
from ase.md.npt import NPT
from ase.md.nptberendsen import Inhomogeneous_NPTBerendsen, NPTBerendsen
from ase.md.nvtberendsen import NVTBerendsen
from pymatgen.core.structure import Molecule, Structure
from pymatgen.io.ase import AseAtomsAdaptor
from pymatgen.optimization.neighbors import find_points_in_spheres

import matgl
from matgl.graph.converters import GraphConverter

if TYPE_CHECKING:
    import dgl
    from ase.io import Trajectory
    from ase.optimize.optimize import Optimizer

    from matgl.apps.pes import Potential


class OPTIMIZERS(Enum):
    """An enumeration of optimizers for used in."""

    fire = opt.fire.FIRE
    bfgs = opt.bfgs.BFGS
    lbfgs = opt.lbfgs.LBFGS
    lbfgslinesearch = opt.lbfgs.LBFGSLineSearch
    mdmin = opt.mdmin.MDMin
    scipyfmincg = opt.sciopt.SciPyFminCG
    scipyfminbfgs = opt.sciopt.SciPyFminBFGS
    bfgslinesearch = opt.bfgslinesearch.BFGSLineSearch


class Atoms2Graph(GraphConverter):
    """Construct a DGL graph from ASE Atoms."""

    def __init__(
        self,
        element_types: tuple[str, ...],
        cutoff: float = 5.0,
    ):
        """Init Atoms2Graph from element types and cutoff radius.

        Args:
            element_types: List of elements present in dataset for graph conversion. This ensures all graphs are
                constructed with the same dimensionality of features.
            cutoff: Cutoff radius for graph representation
        """
        self.element_types = tuple(element_types)
        self.cutoff = cutoff

    def get_graph(self, atoms: Atoms) -> tuple[dgl.DGLGraph, list]:
        """Get a DGL graph from an input Atoms.

        Args:
            atoms: Atoms object.

        Returns:
            g: DGL graph
            state_attr: state features
        """
        numerical_tol = 1.0e-8
        pbc = np.array([1, 1, 1], dtype=int)
        element_types = self.element_types
        lattice_matrix = np.array(atoms.get_cell()) if atoms.pbc.all() else np.zeros((1, 3, 3))
        volume = atoms.get_volume() if atoms.pbc.all() else 0.0
        cart_coords = atoms.get_positions()
        if atoms.pbc.all():
            src_id, dst_id, images, bond_dist = find_points_in_spheres(
                cart_coords,
                cart_coords,
                r=self.cutoff,
                pbc=pbc,
                lattice=lattice_matrix,
                tol=numerical_tol,
            )
            exclude_self = (src_id != dst_id) | (bond_dist > numerical_tol)
            src_id, dst_id, images, bond_dist = (
                src_id[exclude_self],
                dst_id[exclude_self],
                images[exclude_self],
                bond_dist[exclude_self],
            )
        else:
            dist = np.linalg.norm(cart_coords[:, None, :] - cart_coords[None, :, :], axis=-1)
            adj = sp.csr_matrix(dist <= self.cutoff) - sp.eye(len(atoms.get_positions()), dtype=np.bool_)
            adj = adj.tocoo()
            src_id = adj.row
            dst_id = adj.col
        g, state_attr = super().get_graph_from_processed_structure(
            atoms,
            src_id,
            dst_id,
            images if atoms.pbc.all() else np.zeros((len(adj.row), 3)),
            [lattice_matrix] if atoms.pbc.all() else lattice_matrix,
            element_types,
            cart_coords,
            is_atoms=True,
        )
        g.ndata["volume"] = torch.tensor([volume] * g.num_nodes(), dtype=matgl.float_th)
        return g, state_attr


class M3GNetCalculator(Calculator):
    """M3GNet calculator for ASE."""

    implemented_properties = ("energy", "free_energy", "forces", "stress", "hessian")

    def __init__(
        self,
        potential: Potential,
        state_attr: torch.Tensor | None = None,
        stress_weight: float = 1.0,
        **kwargs,
    ):
        """
        Init M3GNetCalculator with a Potential.

        Args:
            potential (Potential): m3gnet.models.Potential
            state_attr (tensor): State attribute
            compute_stress (bool): whether to calculate the stress
            stress_weight (float): the stress weight.
            **kwargs: Kwargs pass through to super().__init__().
        """
        super().__init__(**kwargs)
        self.potential = potential
        self.compute_stress = potential.calc_stresses
        self.compute_hessian = potential.calc_hessian
        self.stress_weight = stress_weight
        self.state_attr = state_attr
        self.element_types = potential.model.element_types  # type: ignore
        self.cutoff = potential.model.cutoff

    def calculate(
        self,
        atoms: Atoms | None = None,
        properties: list | None = None,
        system_changes: list | None = None,
    ):
        """
        Perform calculation for an input Atoms.

        Args:
            atoms (ase.Atoms): ase Atoms object
            properties (list): list of properties to calculate
            system_changes (list): monitor which properties of atoms were
                changed for new calculation. If not, the previous calculation
                results will be loaded.
        """
        properties = properties or ["energy"]
        system_changes = system_changes or all_changes
        super().calculate(atoms=atoms, properties=properties, system_changes=system_changes)
        graph, state_attr_default = Atoms2Graph(self.element_types, self.cutoff).get_graph(atoms)  # type: ignore
        if self.state_attr is not None:
            energies, forces, stresses, hessians = self.potential(graph, self.state_attr)
        else:
            energies, forces, stresses, hessians = self.potential(graph, state_attr_default)
        self.results.update(
            energy=energies.detach().cpu().numpy(),
            free_energy=energies.detach().cpu().numpy(),
            forces=forces.detach().cpu().numpy(),
        )
        if self.compute_stress:
            self.results.update(stress=stresses.detach().cpu().numpy() * self.stress_weight)
        if self.compute_hessian:
            self.results.update(hessian=hessians.detach().cpu().numpy())


class Relaxer:
    """Relaxer is a class for structural relaxation."""

    def __init__(
        self,
        potential: Potential | None = None,
        state_attr: torch.Tensor | None = None,
        optimizer: Optimizer | str = "FIRE",
        relax_cell: bool = True,
        stress_weight: float = 0.01,
    ):
        """
        Args:
            potential (Potential): a M3GNet potential, a str path to a saved model or a short name for saved model
            that comes with M3GNet distribution
            state_attr (torch.Tensor): State attr.
            optimizer (str or ase Optimizer): the optimization algorithm.
            Defaults to "FIRE"
            relax_cell (bool): whether to relax the lattice cell
            stress_weight (float): the stress weight for relaxation.
        """
        self.optimizer: Optimizer = OPTIMIZERS[optimizer.lower()].value if isinstance(optimizer, str) else optimizer
        self.calculator = M3GNetCalculator(
            potential=potential, state_attr=state_attr, stress_weight=stress_weight  # type: ignore
        )
        self.relax_cell = relax_cell
        self.potential = potential
        self.ase_adaptor = AseAtomsAdaptor()

    def relax(
        self,
        atoms: Atoms | Structure | Molecule,
        fmax: float = 0.1,
        steps: int = 500,
        traj_file: str | None = None,
        interval: int = 1,
        verbose: bool = False,
        **kwargs,
    ):
        """
        Relax an input Atoms.

        Args:
            atoms (Atoms | Structure | Molecule): the atoms for relaxation
            fmax (float): total force tolerance for relaxation convergence.
            Here fmax is a sum of force and stress forces
            steps (int): max number of steps for relaxation
            traj_file (str): the trajectory file for saving
            interval (int): the step interval for saving the trajectories
            verbose (bool): Whether to have verbose output.
            kwargs: Kwargs pass-through to optimizer.
        """
        if isinstance(atoms, (Structure, Molecule)):
            atoms = self.ase_adaptor.get_atoms(atoms)
        atoms.set_calculator(self.calculator)
        stream = sys.stdout if verbose else io.StringIO()
        with contextlib.redirect_stdout(stream):
            obs = TrajectoryObserver(atoms)
            if self.relax_cell:
                atoms = ExpCellFilter(atoms)
            optimizer = self.optimizer(atoms, **kwargs)
            optimizer.attach(obs, interval=interval)
            optimizer.run(fmax=fmax, steps=steps)
            obs()
        if traj_file is not None:
            obs.save(traj_file)
        if isinstance(atoms, ExpCellFilter):
            atoms = atoms.atoms

        return {
            "final_structure": self.ase_adaptor.get_structure(atoms),
            "trajectory": obs,
        }


class TrajectoryObserver(collections.abc.Sequence):
    """Trajectory observer is a hook in the relaxation process that saves the
    intermediate structures.
    """

    def __init__(self, atoms: Atoms) -> None:
        """
        Init the Trajectory Observer from a Atoms.

        Args:
            atoms (Atoms): Structure to observe.
        """
        self.atoms = atoms
        self.energies: list[float] = []
        self.forces: list[np.ndarray] = []
        self.stresses: list[np.ndarray] = []
        self.atom_positions: list[np.ndarray] = []
        self.cells: list[np.ndarray] = []

    def __call__(self) -> None:
        """The logic for saving the properties of an Atoms during the relaxation."""
        self.energies.append(float(self.atoms.get_potential_energy()))
        self.forces.append(self.atoms.get_forces())
        self.stresses.append(self.atoms.get_stress())
        self.atom_positions.append(self.atoms.get_positions())
        self.cells.append(self.atoms.get_cell()[:])

    def __getitem__(self, item):
        return self.energies[item], self.forces[item], self.stresses[item], self.cells[item], self.atom_positions[item]

    def __len__(self):
        return len(self.energies)

    def as_pandas(self) -> pd.DataFrame:
        """Returns: DataFrame of energies, forces, streeses, cells and atom_positions."""
        return pd.DataFrame(
            {
                "energies": self.energies,
                "forces": self.forces,
                "stresses": self.stresses,
                "cells": self.cells,
                "atom_positions": self.atom_positions,
            }
        )

    def save(self, filename: str) -> None:
        """Save the trajectory to file.

        Args:
            filename (str): filename to save the trajectory.
        """
        out = {
            "energy": self.energies,
            "forces": self.forces,
            "stresses": self.stresses,
            "atom_positions": self.atom_positions,
            "cell": self.cells,
            "atomic_number": self.atoms.get_atomic_numbers(),
        }
        with open(filename, "wb") as file:
            pickle.dump(out, file)


class MolecularDynamics:
    """Molecular dynamics class."""

    def __init__(
        self,
        atoms: Atoms,
        potential: Potential,
        state_attr: torch.Tensor | None = None,
        ensemble: Literal["nvt", "nvt_langevin", "nvt_andersen", "npt", "npt_berendsen", "npt_nose_hoover"] = "nvt",
        temperature: int = 300,
        timestep: float = 1.0,
        pressure: float = 1.01325 * units.bar,
        taut: float | None = None,
        taup: float | None = None,
        friction: float = 1.0e-3,
        andersen_prob: float = 1.0e-2,
        ttime: float = 25.0,
        pfactor: float = 75.0**2.0,
        external_stress: float | np.ndarray | None = None,
        compressibility_au: float | None = None,
        trajectory: str | Trajectory | None = None,
        logfile: str | None = None,
        loginterval: int = 1,
        append_trajectory: bool = False,
        mask: tuple | np.ndarray | None = None,
    ):
        """
        Init the MD simulation.

        Args:
            atoms (Atoms): atoms to run the MD
            potential (Potential): potential for calculating the energy, force,
            stress of the atoms
            state_attr (torch.Tensor): State attr.
            ensemble (str): choose from 'nvt' or 'npt'. NPT is not tested,
            use with extra caution
            temperature (float): temperature for MD simulation, in K
            timestep (float): time step in fs
            pressure (float): pressure in eV/A^3
            taut (float): time constant for Berendsen temperature coupling
            taup (float): time constant for pressure coupling
            friction (float): friction coefficient for nvt_langevin, typically set to 1e-4 to 1e-2
            andersen_prob (float): random collision probility for nvt_andersen, typically set to 1e-4 to 1e-1
            ttime (float): Characteristic timescale of the thermostat, in ASE internal units
            pfactor (float): A constant in the barostat differential equation.
            external_stress (float): The external stress in eV/A^3.
                                     Either 3x3 tensor,6-vector or a scalar representing pressure
            compressibility_au (float): compressibility of the material in A^3/eV
            trajectory (str or Trajectory): Attach trajectory object
            logfile (str): open this file for recording MD outputs
            loginterval (int): write to log file every interval steps
            append_trajectory (bool): Whether to append to prev trajectory.
            mask (np.array): either a tuple of 3 numbers (0 or 1) or a symmetric 3x3 array indicating,
                             which strain values may change for NPT simulations.
        """
        if isinstance(atoms, (Structure, Molecule)):
            atoms = AseAtomsAdaptor().get_atoms(atoms)
        self.atoms = atoms
        self.atoms.set_calculator(M3GNetCalculator(potential=potential, state_attr=state_attr))

        if taut is None:
            taut = 100 * timestep * units.fs
        if taup is None:
            taup = 1000 * timestep * units.fs
        if mask is None:
            mask = np.array([(1, 0, 0), (0, 1, 0), (0, 0, 1)])
        if external_stress is None:
            external_stress = 0.0

        if ensemble.lower() == "nvt":
            self.dyn = NVTBerendsen(
                self.atoms,
                timestep * units.fs,
                temperature_K=temperature,
                taut=taut,
                trajectory=trajectory,
                logfile=logfile,
                loginterval=loginterval,
                append_trajectory=append_trajectory,
            )

        elif ensemble.lower() == "nvt_langevin":
            self.dyn = Langevin(
                self.atoms,
                timestep * units.fs,
                temperature_K=temperature,
                friction=friction,
                trajectory=trajectory,
                logfile=logfile,
                loginterval=loginterval,
                append_trajectory=append_trajectory,
            )

        elif ensemble.lower() == "nvt_andersen":
            self.dyn = Andersen(
                self.atoms,
                timestep * units.fs,
                temperature_K=temperature,
                andersen_prob=andersen_prob,
                trajectory=trajectory,
                logfile=logfile,
                loginterval=loginterval,
                append_trajectory=append_trajectory,
            )

        elif ensemble.lower() == "npt":
            """
            NPT ensemble default to Inhomogeneous_NPTBerendsen thermo/barostat
            This is a more flexible scheme that fixes three angles of the unit
            cell but allows three lattice parameter to change independently.
            """

            self.dyn = Inhomogeneous_NPTBerendsen(
                self.atoms,
                timestep * units.fs,
                temperature_K=temperature,
                pressure_au=pressure,
                taut=taut,
                taup=taup,
                compressibility_au=compressibility_au,
                trajectory=trajectory,
                logfile=logfile,
                loginterval=loginterval,
                # append_trajectory=append_trajectory,
                # this option is not supported in ASE at this point (I have sent merge request there)
            )

        elif ensemble.lower() == "npt_berendsen":
            """

            This is a similar scheme to the Inhomogeneous_NPTBerendsen.
            This is a less flexible scheme that fixes the shape of the
            cell - three angles are fixed and the ratios between the three
            lattice constants.

            """

            self.dyn = NPTBerendsen(
                self.atoms,
                timestep * units.fs,
                temperature_K=temperature,
                pressure_au=pressure,
                taut=taut,
                taup=taup,
                compressibility_au=compressibility_au,
                trajectory=trajectory,
                logfile=logfile,
                loginterval=loginterval,
                append_trajectory=append_trajectory,
            )

        elif ensemble.lower() == "npt_nose_hoover":
            self.dyn = NPT(
                self.atoms,
                timestep * units.fs,
                temperature_K=temperature,
                externalstress=external_stress,
                ttime=ttime * units.fs,
                pfactor=pfactor * units.fs,
                trajectory=trajectory,
                logfile=logfile,
                loginterval=loginterval,
                append_trajectory=append_trajectory,
                mask=mask,
            )

        else:
            raise ValueError("Ensemble not supported")

        self.trajectory = trajectory
        self.logfile = logfile
        self.loginterval = loginterval
        self.timestep = timestep

    def run(self, steps: int):
        """Thin wrapper of ase MD run.

        Args:
            steps (int): number of MD steps
        """
        self.dyn.run(steps)

    def set_atoms(self, atoms: Atoms):
        """Set new atoms to run MD.

        Args:
            atoms (Atoms): new atoms for running MD.
        """
        if isinstance(atoms, (Structure, Molecule)):
            atoms = AseAtomsAdaptor().get_atoms(atoms)
        calculator = self.atoms.calc
        self.atoms = atoms
        self.dyn.atoms = atoms
        self.dyn.atoms.set_calculator(calculator)
