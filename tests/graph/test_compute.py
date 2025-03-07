from __future__ import annotations

from functools import partial

import numpy as np
import torch
from pymatgen.core import Lattice, Structure

from matgl.ext.pymatgen import Structure2Graph, get_element_list
from matgl.graph.compute import (
    compute_pair_vector_and_distance,
    compute_theta,
    compute_theta_and_phi,
    create_line_graph,
)


def _loop_indices(bond_atom_indices, pair_dist, cutoff=4.0):
    bin_count = np.bincount(bond_atom_indices[:, 0], minlength=bond_atom_indices[-1, 0] + 1)
    indices = []
    start = 0
    for bcont in bin_count:
        for i in range(bcont):
            for j in range(bcont):
                if start + i == start + j:
                    continue
                if pair_dist[start + i] > cutoff or pair_dist[start + j] > cutoff:
                    continue
                indices.append([start + i, start + j])
        start += bcont
    return np.array(indices)


def _calculate_cos_loop(graph, threebody_cutoff=4.0):
    """
    Calculate the cosine theta of triplets using loops
    Args:
        graph: List
    Returns: a list of cosine theta values.
    """
    _, _, n_sites = torch.unique(graph.edges()[0], return_inverse=True, return_counts=True)
    start_index = 0
    cos = []
    for n_site in n_sites:
        for i in range(n_site):
            for j in range(n_site):
                if i == j:
                    continue
                vi = graph.edata["bond_vec"][i + start_index].numpy()
                vj = graph.edata["bond_vec"][j + start_index].numpy()
                di = np.linalg.norm(vi)
                dj = np.linalg.norm(vj)
                if (di <= threebody_cutoff) and (dj <= threebody_cutoff):
                    cos.append(vi.dot(vj) / np.linalg.norm(vi) / np.linalg.norm(vj))
        start_index += n_site
    return cos


class TestCompute:
    def test_compute_pair_vector(self, graph_Mo):
        s1, g1, state1 = graph_Mo
        bv, bd = compute_pair_vector_and_distance(g1)
        g1.edata["bond_vec"] = bv
        d = torch.linalg.norm(g1.edata["bond_vec"], axis=1)

        _, _, _, d2 = s1.get_neighbor_list(r=5.0)

        np.testing.assert_array_almost_equal(np.sort(d), np.sort(d2))

    def test_compute_pair_vector_for_molecule(self, graph_CH4):
        s2, g2, state2 = graph_CH4
        bv, bd = compute_pair_vector_and_distance(g2)
        g2.edata["bond_vec"] = bv
        d = torch.linalg.norm(g2.edata["bond_vec"], axis=1)

        d2 = np.array(
            [
                1.089,
                1.089,
                1.089,
                1.089,
                1.089,
                1.089,
                1.089,
                1.089,
                1.77833,
                1.77833,
                1.77833,
                1.77833,
                1.77833,
                1.77833,
                1.77833,
                1.77833,
                1.77833,
                1.77833,
                1.77833,
                1.77833,
            ]
        )

        np.testing.assert_array_almost_equal(np.sort(d), np.sort(d2))

    def test_compute_angle(self, graph_Mo, graph_CH4):
        s1, g1, state1 = graph_Mo
        bv, bd = compute_pair_vector_and_distance(g1)
        g1.edata["bond_vec"] = bv
        g1.edata["bond_dist"] = bd
        cos_loop = _calculate_cos_loop(g1, 4.0)

        line_graph = create_line_graph(g1, 4.0)
        line_graph.apply_edges(compute_theta_and_phi)
        np.testing.assert_array_almost_equal(
            np.sort(np.array(cos_loop)), np.sort(np.array(line_graph.edata["cos_theta"]))
        )

        # test only compute theta
        line_graph.apply_edges(compute_theta)
        np.testing.assert_array_almost_equal(
            np.sort(np.arccos(np.array(cos_loop))), np.sort(np.array(line_graph.edata["theta"])), decimal=4
        )

        # test only compute theta with cosine
        _ = line_graph.edata.pop("cos_theta")
        line_graph.apply_edges(partial(compute_theta, cosine=True))
        np.testing.assert_array_almost_equal(
            np.sort(np.array(cos_loop)), np.sort(np.array(line_graph.edata["cos_theta"]))
        )

        s2, g2, state2 = graph_CH4

        bv, bd = compute_pair_vector_and_distance(g2)
        g2.edata["bond_vec"] = bv
        g2.edata["bond_dist"] = bd
        cos_loop = _calculate_cos_loop(g2, 2.0)

        line_graph = create_line_graph(g2, 2.0)
        line_graph.apply_edges(compute_theta_and_phi)
        np.testing.assert_array_almost_equal(
            np.sort(np.array(cos_loop)), np.sort(np.array(line_graph.edata["cos_theta"]))
        )

        # test only compute theta
        line_graph.apply_edges(compute_theta)
        np.testing.assert_array_almost_equal(
            np.sort(np.arccos(np.array(cos_loop))), np.sort(np.array(line_graph.edata["theta"]))
        )

        # test only compute theta with cosine
        _ = line_graph.edata.pop("cos_theta")
        line_graph.apply_edges(partial(compute_theta, cosine=True))
        np.testing.assert_array_almost_equal(
            np.sort(np.array(cos_loop)), np.sort(np.array(line_graph.edata["cos_theta"]))
        )

    def test_compute_three_body(self, graph_AcAla3NHMe):
        mol1, g1, _ = graph_AcAla3NHMe
        bv, bd = compute_pair_vector_and_distance(g1)
        g1.edata["bond_vec"] = bv
        g1.edata["bond_dist"] = bd
        line_graph = create_line_graph(g1, 5.0)
        line_graph.apply_edges(compute_theta_and_phi)
        np.testing.assert_allclose(line_graph.edata["triple_bond_lengths"].detach().numpy()[0], 1.777829)


def test_line_graph_extensive():
    structure = Structure.from_spacegroup("Fm-3m", Lattice.cubic(6.0 / np.sqrt(2)), ["Fe"], [[0, 0, 0]])

    element_types = get_element_list([structure])
    converter = Structure2Graph(element_types=element_types, cutoff=5.0)
    g1, _ = converter.get_graph(structure)
    bond_vec, bond_dist = compute_pair_vector_and_distance(g1)
    g1.edata["bond_dist"] = bond_dist
    g1.edata["bond_vec"] = bond_vec

    supercell = structure.copy()
    supercell.make_supercell([2, 1, 1])
    g2, _ = converter.get_graph(supercell)
    bond_vec, bond_dist = compute_pair_vector_and_distance(g2)
    g2.edata["bond_dist"] = bond_dist
    g2.edata["bond_vec"] = bond_vec

    lg1 = create_line_graph(g1, 3.0)
    lg2 = create_line_graph(g2, 3.0)

    assert 2 * g1.number_of_nodes() == g2.number_of_nodes()
    assert 2 * g1.number_of_edges() == g2.number_of_edges()
    assert 2 * lg1.number_of_nodes() == lg2.number_of_nodes()
    assert 2 * lg1.number_of_edges() == lg2.number_of_edges()
