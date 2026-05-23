from __future__ import annotations

import re

import numpy as np


ELEMENT_DATA: dict[str, dict[str, float]] = {
    "Al": {"atomic_number": 13.0, "atomic_weight": 26.9815, "group": 13.0, "period": 3.0, "pauling_electronegativity": 1.61, "covalent_radius_A": 1.21},
    "B": {"atomic_number": 5.0, "atomic_weight": 10.81, "group": 13.0, "period": 2.0, "pauling_electronegativity": 2.04, "covalent_radius_A": 0.84},
    "Ba": {"atomic_number": 56.0, "atomic_weight": 137.327, "group": 2.0, "period": 6.0, "pauling_electronegativity": 0.89, "covalent_radius_A": 2.15},
    "Br": {"atomic_number": 35.0, "atomic_weight": 79.904, "group": 17.0, "period": 4.0, "pauling_electronegativity": 2.96, "covalent_radius_A": 1.20},
    "C": {"atomic_number": 6.0, "atomic_weight": 12.011, "group": 14.0, "period": 2.0, "pauling_electronegativity": 2.55, "covalent_radius_A": 0.76},
    "Ca": {"atomic_number": 20.0, "atomic_weight": 40.078, "group": 2.0, "period": 4.0, "pauling_electronegativity": 1.00, "covalent_radius_A": 1.76},
    "Ce": {"atomic_number": 58.0, "atomic_weight": 140.116, "group": 3.0, "period": 6.0, "pauling_electronegativity": 1.12, "covalent_radius_A": 2.04},
    "Cl": {"atomic_number": 17.0, "atomic_weight": 35.45, "group": 17.0, "period": 3.0, "pauling_electronegativity": 3.16, "covalent_radius_A": 1.02},
    "Co": {"atomic_number": 27.0, "atomic_weight": 58.9332, "group": 9.0, "period": 4.0, "pauling_electronegativity": 1.88, "covalent_radius_A": 1.26},
    "Cs": {"atomic_number": 55.0, "atomic_weight": 132.905, "group": 1.0, "period": 6.0, "pauling_electronegativity": 0.79, "covalent_radius_A": 2.44},
    "Cu": {"atomic_number": 29.0, "atomic_weight": 63.546, "group": 11.0, "period": 4.0, "pauling_electronegativity": 1.90, "covalent_radius_A": 1.32},
    "Eu": {"atomic_number": 63.0, "atomic_weight": 151.964, "group": 3.0, "period": 6.0, "pauling_electronegativity": 1.20, "covalent_radius_A": 1.98},
    "F": {"atomic_number": 9.0, "atomic_weight": 18.998, "group": 17.0, "period": 2.0, "pauling_electronegativity": 3.98, "covalent_radius_A": 0.57},
    "Fe": {"atomic_number": 26.0, "atomic_weight": 55.845, "group": 8.0, "period": 4.0, "pauling_electronegativity": 1.83, "covalent_radius_A": 1.32},
    "H": {"atomic_number": 1.0, "atomic_weight": 1.008, "group": 1.0, "period": 1.0, "pauling_electronegativity": 2.20, "covalent_radius_A": 0.31},
    "Hf": {"atomic_number": 72.0, "atomic_weight": 178.49, "group": 4.0, "period": 6.0, "pauling_electronegativity": 1.30, "covalent_radius_A": 1.75},
    "I": {"atomic_number": 53.0, "atomic_weight": 126.904, "group": 17.0, "period": 5.0, "pauling_electronegativity": 2.66, "covalent_radius_A": 1.39},
    "K": {"atomic_number": 19.0, "atomic_weight": 39.0983, "group": 1.0, "period": 4.0, "pauling_electronegativity": 0.82, "covalent_radius_A": 2.03},
    "La": {"atomic_number": 57.0, "atomic_weight": 138.905, "group": 3.0, "period": 6.0, "pauling_electronegativity": 1.10, "covalent_radius_A": 2.07},
    "Li": {"atomic_number": 3.0, "atomic_weight": 6.94, "group": 1.0, "period": 2.0, "pauling_electronegativity": 0.98, "covalent_radius_A": 1.28},
    "Mg": {"atomic_number": 12.0, "atomic_weight": 24.305, "group": 2.0, "period": 3.0, "pauling_electronegativity": 1.31, "covalent_radius_A": 1.41},
    "Mn": {"atomic_number": 25.0, "atomic_weight": 54.938, "group": 7.0, "period": 4.0, "pauling_electronegativity": 1.55, "covalent_radius_A": 1.39},
    "Mo": {"atomic_number": 42.0, "atomic_weight": 95.95, "group": 6.0, "period": 5.0, "pauling_electronegativity": 2.16, "covalent_radius_A": 1.54},
    "N": {"atomic_number": 7.0, "atomic_weight": 14.007, "group": 15.0, "period": 2.0, "pauling_electronegativity": 3.04, "covalent_radius_A": 0.71},
    "Na": {"atomic_number": 11.0, "atomic_weight": 22.99, "group": 1.0, "period": 3.0, "pauling_electronegativity": 0.93, "covalent_radius_A": 1.66},
    "Nb": {"atomic_number": 41.0, "atomic_weight": 92.906, "group": 5.0, "period": 5.0, "pauling_electronegativity": 1.60, "covalent_radius_A": 1.64},
    "Nd": {"atomic_number": 60.0, "atomic_weight": 144.242, "group": 3.0, "period": 6.0, "pauling_electronegativity": 1.14, "covalent_radius_A": 2.01},
    "Ni": {"atomic_number": 28.0, "atomic_weight": 58.6934, "group": 10.0, "period": 4.0, "pauling_electronegativity": 1.91, "covalent_radius_A": 1.24},
    "O": {"atomic_number": 8.0, "atomic_weight": 15.999, "group": 16.0, "period": 2.0, "pauling_electronegativity": 3.44, "covalent_radius_A": 0.66},
    "P": {"atomic_number": 15.0, "atomic_weight": 30.974, "group": 15.0, "period": 3.0, "pauling_electronegativity": 2.19, "covalent_radius_A": 1.07},
    "Pd": {"atomic_number": 46.0, "atomic_weight": 106.42, "group": 10.0, "period": 5.0, "pauling_electronegativity": 2.20, "covalent_radius_A": 1.39},
    "S": {"atomic_number": 16.0, "atomic_weight": 32.06, "group": 16.0, "period": 3.0, "pauling_electronegativity": 2.58, "covalent_radius_A": 1.05},
    "Si": {"atomic_number": 14.0, "atomic_weight": 28.085, "group": 14.0, "period": 3.0, "pauling_electronegativity": 1.90, "covalent_radius_A": 1.11},
    "Sr": {"atomic_number": 38.0, "atomic_weight": 87.62, "group": 2.0, "period": 5.0, "pauling_electronegativity": 0.95, "covalent_radius_A": 1.95},
    "Tb": {"atomic_number": 65.0, "atomic_weight": 158.925, "group": 3.0, "period": 6.0, "pauling_electronegativity": 1.10, "covalent_radius_A": 1.94},
    "Ti": {"atomic_number": 22.0, "atomic_weight": 47.867, "group": 4.0, "period": 4.0, "pauling_electronegativity": 1.54, "covalent_radius_A": 1.60},
    "V": {"atomic_number": 23.0, "atomic_weight": 50.942, "group": 5.0, "period": 4.0, "pauling_electronegativity": 1.63, "covalent_radius_A": 1.53},
    "W": {"atomic_number": 74.0, "atomic_weight": 183.84, "group": 6.0, "period": 6.0, "pauling_electronegativity": 2.36, "covalent_radius_A": 1.62},
    "Y": {"atomic_number": 39.0, "atomic_weight": 88.906, "group": 3.0, "period": 5.0, "pauling_electronegativity": 1.22, "covalent_radius_A": 1.80},
    "Zn": {"atomic_number": 30.0, "atomic_weight": 65.38, "group": 12.0, "period": 4.0, "pauling_electronegativity": 1.65, "covalent_radius_A": 1.22},
    "Zr": {"atomic_number": 40.0, "atomic_weight": 91.224, "group": 4.0, "period": 5.0, "pauling_electronegativity": 1.33, "covalent_radius_A": 1.75},
}


_ELEMENT_DATA_EXPANDED: dict[str, dict[str, float]] = {
    "Al": {"atomic_number": 13, "atomic_weight": 26.982, "group": 13, "period": 3, "pauling_electronegativity": 1.61, "covalent_radius_A": 1.21, "vdw_radius_A": 1.84, "first_ionization_energy_eV": 5.986, "electron_affinity_eV": 0.433, "common_oxidation_state": 3, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.535, "oxide_formation_enthalpy_kJ_mol_O": -558.6, "standard_reduction_potential_V": -1.662, "oxide_band_gap_eV": 8.8},
    "B": {"atomic_number": 5, "atomic_weight": 10.81, "group": 13, "period": 2, "pauling_electronegativity": 2.04, "covalent_radius_A": 0.84, "vdw_radius_A": 1.92, "first_ionization_energy_eV": 8.298, "electron_affinity_eV": 0.280, "common_oxidation_state": 3, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.27, "oxide_formation_enthalpy_kJ_mol_O": -422.2, "standard_reduction_potential_V": -0.890, "oxide_band_gap_eV": 6.2},
    "Ba": {"atomic_number": 56, "atomic_weight": 137.327, "group": 2, "period": 6, "pauling_electronegativity": 0.89, "covalent_radius_A": 2.15, "vdw_radius_A": 2.68, "first_ionization_energy_eV": 5.212, "electron_affinity_eV": 0.145, "common_oxidation_state": 2, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 1.35, "oxide_formation_enthalpy_kJ_mol_O": -553.5, "standard_reduction_potential_V": -2.912, "oxide_band_gap_eV": 3.9},
    "Br": {"atomic_number": 35, "atomic_weight": 79.904, "group": 17, "period": 4, "pauling_electronegativity": 2.96, "covalent_radius_A": 1.20, "vdw_radius_A": 1.85, "first_ionization_energy_eV": 11.814, "electron_affinity_eV": 3.364, "common_oxidation_state": -1, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 1.96, "oxide_formation_enthalpy_kJ_mol_O": 0.0, "standard_reduction_potential_V": 1.066, "oxide_band_gap_eV": 0.0},
    "C": {"atomic_number": 6, "atomic_weight": 12.011, "group": 14, "period": 2, "pauling_electronegativity": 2.55, "covalent_radius_A": 0.76, "vdw_radius_A": 1.70, "first_ionization_energy_eV": 11.260, "electron_affinity_eV": 1.263, "common_oxidation_state": 4, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.16, "oxide_formation_enthalpy_kJ_mol_O": -196.7, "standard_reduction_potential_V": 0.207, "oxide_band_gap_eV": 0.0},
    "Ca": {"atomic_number": 20, "atomic_weight": 40.078, "group": 2, "period": 4, "pauling_electronegativity": 1.00, "covalent_radius_A": 1.76, "vdw_radius_A": 2.31, "first_ionization_energy_eV": 6.113, "electron_affinity_eV": 0.025, "common_oxidation_state": 2, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 1.00, "oxide_formation_enthalpy_kJ_mol_O": -635.1, "standard_reduction_potential_V": -2.868, "oxide_band_gap_eV": 6.9},
    "Ce": {"atomic_number": 58, "atomic_weight": 140.116, "group": 3, "period": 6, "pauling_electronegativity": 1.12, "covalent_radius_A": 2.04, "vdw_radius_A": 2.42, "first_ionization_energy_eV": 5.539, "electron_affinity_eV": 0.55, "common_oxidation_state": 4, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.87, "oxide_formation_enthalpy_kJ_mol_O": -544.4, "standard_reduction_potential_V": 1.72, "oxide_band_gap_eV": 3.2},
    "Cl": {"atomic_number": 17, "atomic_weight": 35.45, "group": 17, "period": 3, "pauling_electronegativity": 3.16, "covalent_radius_A": 1.02, "vdw_radius_A": 1.75, "first_ionization_energy_eV": 12.968, "electron_affinity_eV": 3.613, "common_oxidation_state": -1, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 1.81, "oxide_formation_enthalpy_kJ_mol_O": 0.0, "standard_reduction_potential_V": 1.358, "oxide_band_gap_eV": 0.0},
    "Co": {"atomic_number": 27, "atomic_weight": 58.933, "group": 9, "period": 4, "pauling_electronegativity": 1.88, "covalent_radius_A": 1.26, "vdw_radius_A": 2.00, "first_ionization_energy_eV": 7.881, "electron_affinity_eV": 0.662, "common_oxidation_state": 2, "d_electron_count_common_oxide": 7, "ionic_radius_shannon_common_ox_cn6_A": 0.745, "oxide_formation_enthalpy_kJ_mol_O": -237.9, "standard_reduction_potential_V": -0.280, "oxide_band_gap_eV": 2.4},
    "Cs": {"atomic_number": 55, "atomic_weight": 132.905, "group": 1, "period": 6, "pauling_electronegativity": 0.79, "covalent_radius_A": 2.44, "vdw_radius_A": 3.43, "first_ionization_energy_eV": 3.894, "electron_affinity_eV": 0.472, "common_oxidation_state": 1, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 1.67, "oxide_formation_enthalpy_kJ_mol_O": -346.0, "standard_reduction_potential_V": -3.026, "oxide_band_gap_eV": 2.0},
    "Cu": {"atomic_number": 29, "atomic_weight": 63.546, "group": 11, "period": 4, "pauling_electronegativity": 1.90, "covalent_radius_A": 1.32, "vdw_radius_A": 1.40, "first_ionization_energy_eV": 7.726, "electron_affinity_eV": 1.236, "common_oxidation_state": 2, "d_electron_count_common_oxide": 9, "ionic_radius_shannon_common_ox_cn6_A": 0.73, "oxide_formation_enthalpy_kJ_mol_O": -157.3, "standard_reduction_potential_V": 0.342, "oxide_band_gap_eV": 1.2},
    "Eu": {"atomic_number": 63, "atomic_weight": 151.964, "group": 3, "period": 6, "pauling_electronegativity": 1.20, "covalent_radius_A": 1.98, "vdw_radius_A": 2.33, "first_ionization_energy_eV": 5.670, "electron_affinity_eV": 0.116, "common_oxidation_state": 3, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.947, "oxide_formation_enthalpy_kJ_mol_O": -550.5, "standard_reduction_potential_V": -1.991, "oxide_band_gap_eV": 4.4},
    "F": {"atomic_number": 9, "atomic_weight": 18.998, "group": 17, "period": 2, "pauling_electronegativity": 3.98, "covalent_radius_A": 0.57, "vdw_radius_A": 1.47, "first_ionization_energy_eV": 17.423, "electron_affinity_eV": 3.401, "common_oxidation_state": -1, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 1.33, "oxide_formation_enthalpy_kJ_mol_O": 0.0, "standard_reduction_potential_V": 2.866, "oxide_band_gap_eV": 0.0},
    "Fe": {"atomic_number": 26, "atomic_weight": 55.845, "group": 8, "period": 4, "pauling_electronegativity": 1.83, "covalent_radius_A": 1.32, "vdw_radius_A": 2.04, "first_ionization_energy_eV": 7.902, "electron_affinity_eV": 0.151, "common_oxidation_state": 3, "d_electron_count_common_oxide": 5, "ionic_radius_shannon_common_ox_cn6_A": 0.645, "oxide_formation_enthalpy_kJ_mol_O": -274.7, "standard_reduction_potential_V": -0.037, "oxide_band_gap_eV": 2.0},
    "H": {"atomic_number": 1, "atomic_weight": 1.008, "group": 1, "period": 1, "pauling_electronegativity": 2.20, "covalent_radius_A": 0.31, "vdw_radius_A": 1.20, "first_ionization_energy_eV": 13.598, "electron_affinity_eV": 0.754, "common_oxidation_state": 1, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.0, "oxide_formation_enthalpy_kJ_mol_O": -285.8, "standard_reduction_potential_V": 0.000, "oxide_band_gap_eV": 0.0},
    "Hf": {"atomic_number": 72, "atomic_weight": 178.49, "group": 4, "period": 6, "pauling_electronegativity": 1.30, "covalent_radius_A": 1.75, "vdw_radius_A": 2.23, "first_ionization_energy_eV": 6.825, "electron_affinity_eV": 0.0, "common_oxidation_state": 4, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.71, "oxide_formation_enthalpy_kJ_mol_O": -558.8, "standard_reduction_potential_V": -1.700, "oxide_band_gap_eV": 5.7},
    "I": {"atomic_number": 53, "atomic_weight": 126.904, "group": 17, "period": 5, "pauling_electronegativity": 2.66, "covalent_radius_A": 1.39, "vdw_radius_A": 1.98, "first_ionization_energy_eV": 10.451, "electron_affinity_eV": 3.059, "common_oxidation_state": -1, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 2.20, "oxide_formation_enthalpy_kJ_mol_O": 0.0, "standard_reduction_potential_V": 0.536, "oxide_band_gap_eV": 0.0},
    "K": {"atomic_number": 19, "atomic_weight": 39.098, "group": 1, "period": 4, "pauling_electronegativity": 0.82, "covalent_radius_A": 2.03, "vdw_radius_A": 2.75, "first_ionization_energy_eV": 4.341, "electron_affinity_eV": 0.501, "common_oxidation_state": 1, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 1.38, "oxide_formation_enthalpy_kJ_mol_O": -361.5, "standard_reduction_potential_V": -2.924, "oxide_band_gap_eV": 4.0},
    "La": {"atomic_number": 57, "atomic_weight": 138.905, "group": 3, "period": 6, "pauling_electronegativity": 1.10, "covalent_radius_A": 2.07, "vdw_radius_A": 2.43, "first_ionization_energy_eV": 5.577, "electron_affinity_eV": 0.557, "common_oxidation_state": 3, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 1.032, "oxide_formation_enthalpy_kJ_mol_O": -597.9, "standard_reduction_potential_V": -2.379, "oxide_band_gap_eV": 5.5},
    "Li": {"atomic_number": 3, "atomic_weight": 6.94, "group": 1, "period": 2, "pauling_electronegativity": 0.98, "covalent_radius_A": 1.28, "vdw_radius_A": 1.82, "first_ionization_energy_eV": 5.392, "electron_affinity_eV": 0.618, "common_oxidation_state": 1, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.76, "oxide_formation_enthalpy_kJ_mol_O": -597.9, "standard_reduction_potential_V": -3.040, "oxide_band_gap_eV": 6.6},
    "Mg": {"atomic_number": 12, "atomic_weight": 24.305, "group": 2, "period": 3, "pauling_electronegativity": 1.31, "covalent_radius_A": 1.41, "vdw_radius_A": 1.73, "first_ionization_energy_eV": 7.646, "electron_affinity_eV": 0.0, "common_oxidation_state": 2, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.72, "oxide_formation_enthalpy_kJ_mol_O": -601.6, "standard_reduction_potential_V": -2.372, "oxide_band_gap_eV": 7.8},
    "Mn": {"atomic_number": 25, "atomic_weight": 54.938, "group": 7, "period": 4, "pauling_electronegativity": 1.55, "covalent_radius_A": 1.39, "vdw_radius_A": 2.05, "first_ionization_energy_eV": 7.434, "electron_affinity_eV": 0.0, "common_oxidation_state": 2, "d_electron_count_common_oxide": 5, "ionic_radius_shannon_common_ox_cn6_A": 0.83, "oxide_formation_enthalpy_kJ_mol_O": -385.2, "standard_reduction_potential_V": -1.185, "oxide_band_gap_eV": 3.6},
    "Mo": {"atomic_number": 42, "atomic_weight": 95.95, "group": 6, "period": 5, "pauling_electronegativity": 2.16, "covalent_radius_A": 1.54, "vdw_radius_A": 2.17, "first_ionization_energy_eV": 7.092, "electron_affinity_eV": 0.746, "common_oxidation_state": 6, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.59, "oxide_formation_enthalpy_kJ_mol_O": -248.4, "standard_reduction_potential_V": -0.200, "oxide_band_gap_eV": 3.0},
    "N": {"atomic_number": 7, "atomic_weight": 14.007, "group": 15, "period": 2, "pauling_electronegativity": 3.04, "covalent_radius_A": 0.71, "vdw_radius_A": 1.55, "first_ionization_energy_eV": 14.534, "electron_affinity_eV": 0.0, "common_oxidation_state": -3, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 1.46, "oxide_formation_enthalpy_kJ_mol_O": 0.0, "standard_reduction_potential_V": -3.040, "oxide_band_gap_eV": 0.0},
    "Na": {"atomic_number": 11, "atomic_weight": 22.990, "group": 1, "period": 3, "pauling_electronegativity": 0.93, "covalent_radius_A": 1.66, "vdw_radius_A": 2.27, "first_ionization_energy_eV": 5.139, "electron_affinity_eV": 0.548, "common_oxidation_state": 1, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 1.02, "oxide_formation_enthalpy_kJ_mol_O": -414.2, "standard_reduction_potential_V": -2.714, "oxide_band_gap_eV": 4.7},
    "Nb": {"atomic_number": 41, "atomic_weight": 92.906, "group": 5, "period": 5, "pauling_electronegativity": 1.60, "covalent_radius_A": 1.64, "vdw_radius_A": 2.18, "first_ionization_energy_eV": 6.759, "electron_affinity_eV": 0.893, "common_oxidation_state": 5, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.64, "oxide_formation_enthalpy_kJ_mol_O": -305.6, "standard_reduction_potential_V": -0.644, "oxide_band_gap_eV": 3.4},
    "Nd": {"atomic_number": 60, "atomic_weight": 144.242, "group": 3, "period": 6, "pauling_electronegativity": 1.14, "covalent_radius_A": 2.01, "vdw_radius_A": 2.39, "first_ionization_energy_eV": 5.525, "electron_affinity_eV": 0.100, "common_oxidation_state": 3, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.983, "oxide_formation_enthalpy_kJ_mol_O": -602.6, "standard_reduction_potential_V": -2.323, "oxide_band_gap_eV": 5.0},
    "Ni": {"atomic_number": 28, "atomic_weight": 58.693, "group": 10, "period": 4, "pauling_electronegativity": 1.91, "covalent_radius_A": 1.24, "vdw_radius_A": 1.63, "first_ionization_energy_eV": 7.640, "electron_affinity_eV": 1.156, "common_oxidation_state": 2, "d_electron_count_common_oxide": 8, "ionic_radius_shannon_common_ox_cn6_A": 0.69, "oxide_formation_enthalpy_kJ_mol_O": -239.7, "standard_reduction_potential_V": -0.257, "oxide_band_gap_eV": 3.7},
    "O": {"atomic_number": 8, "atomic_weight": 15.999, "group": 16, "period": 2, "pauling_electronegativity": 3.44, "covalent_radius_A": 0.66, "vdw_radius_A": 1.52, "first_ionization_energy_eV": 13.618, "electron_affinity_eV": 1.461, "common_oxidation_state": -2, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 1.40, "oxide_formation_enthalpy_kJ_mol_O": 0.0, "standard_reduction_potential_V": 1.229, "oxide_band_gap_eV": 0.0},
    "P": {"atomic_number": 15, "atomic_weight": 30.974, "group": 15, "period": 3, "pauling_electronegativity": 2.19, "covalent_radius_A": 1.07, "vdw_radius_A": 1.80, "first_ionization_energy_eV": 10.487, "electron_affinity_eV": 0.747, "common_oxidation_state": 5, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.38, "oxide_formation_enthalpy_kJ_mol_O": -285.0, "standard_reduction_potential_V": -0.276, "oxide_band_gap_eV": 0.0},
    "Pd": {"atomic_number": 46, "atomic_weight": 106.42, "group": 10, "period": 5, "pauling_electronegativity": 2.20, "covalent_radius_A": 1.39, "vdw_radius_A": 1.63, "first_ionization_energy_eV": 8.337, "electron_affinity_eV": 0.562, "common_oxidation_state": 2, "d_electron_count_common_oxide": 8, "ionic_radius_shannon_common_ox_cn6_A": 0.86, "oxide_formation_enthalpy_kJ_mol_O": -85.4, "standard_reduction_potential_V": 0.915, "oxide_band_gap_eV": 1.0},
    "S": {"atomic_number": 16, "atomic_weight": 32.06, "group": 16, "period": 3, "pauling_electronegativity": 2.58, "covalent_radius_A": 1.05, "vdw_radius_A": 1.80, "first_ionization_energy_eV": 10.360, "electron_affinity_eV": 2.077, "common_oxidation_state": -2, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 1.84, "oxide_formation_enthalpy_kJ_mol_O": 0.0, "standard_reduction_potential_V": -0.476, "oxide_band_gap_eV": 0.0},
    "Si": {"atomic_number": 14, "atomic_weight": 28.085, "group": 14, "period": 3, "pauling_electronegativity": 1.90, "covalent_radius_A": 1.11, "vdw_radius_A": 2.10, "first_ionization_energy_eV": 8.152, "electron_affinity_eV": 1.390, "common_oxidation_state": 4, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.40, "oxide_formation_enthalpy_kJ_mol_O": -455.1, "standard_reduction_potential_V": -0.857, "oxide_band_gap_eV": 9.0},
    "Sr": {"atomic_number": 38, "atomic_weight": 87.62, "group": 2, "period": 5, "pauling_electronegativity": 0.95, "covalent_radius_A": 1.95, "vdw_radius_A": 2.49, "first_ionization_energy_eV": 5.695, "electron_affinity_eV": 0.048, "common_oxidation_state": 2, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 1.18, "oxide_formation_enthalpy_kJ_mol_O": -592.0, "standard_reduction_potential_V": -2.899, "oxide_band_gap_eV": 5.9},
    "Tb": {"atomic_number": 65, "atomic_weight": 158.925, "group": 3, "period": 6, "pauling_electronegativity": 1.10, "covalent_radius_A": 1.94, "vdw_radius_A": 2.33, "first_ionization_energy_eV": 5.864, "electron_affinity_eV": 0.100, "common_oxidation_state": 3, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.923, "oxide_formation_enthalpy_kJ_mol_O": -622.0, "standard_reduction_potential_V": -2.280, "oxide_band_gap_eV": 3.8},
    "Ti": {"atomic_number": 22, "atomic_weight": 47.867, "group": 4, "period": 4, "pauling_electronegativity": 1.54, "covalent_radius_A": 1.60, "vdw_radius_A": 2.11, "first_ionization_energy_eV": 6.828, "electron_affinity_eV": 0.079, "common_oxidation_state": 4, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.605, "oxide_formation_enthalpy_kJ_mol_O": -472.0, "standard_reduction_potential_V": -0.370, "oxide_band_gap_eV": 3.2},
    "V": {"atomic_number": 23, "atomic_weight": 50.942, "group": 5, "period": 4, "pauling_electronegativity": 1.63, "covalent_radius_A": 1.53, "vdw_radius_A": 2.07, "first_ionization_energy_eV": 6.746, "electron_affinity_eV": 0.525, "common_oxidation_state": 5, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.54, "oxide_formation_enthalpy_kJ_mol_O": -310.1, "standard_reduction_potential_V": 1.000, "oxide_band_gap_eV": 2.3},
    "W": {"atomic_number": 74, "atomic_weight": 183.84, "group": 6, "period": 6, "pauling_electronegativity": 2.36, "covalent_radius_A": 1.62, "vdw_radius_A": 2.18, "first_ionization_energy_eV": 7.864, "electron_affinity_eV": 0.816, "common_oxidation_state": 6, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.60, "oxide_formation_enthalpy_kJ_mol_O": -281.0, "standard_reduction_potential_V": -0.090, "oxide_band_gap_eV": 2.6},
    "Y": {"atomic_number": 39, "atomic_weight": 88.906, "group": 3, "period": 5, "pauling_electronegativity": 1.22, "covalent_radius_A": 1.80, "vdw_radius_A": 2.32, "first_ionization_energy_eV": 6.217, "electron_affinity_eV": 0.307, "common_oxidation_state": 3, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.90, "oxide_formation_enthalpy_kJ_mol_O": -635.1, "standard_reduction_potential_V": -2.372, "oxide_band_gap_eV": 5.6},
    "Zn": {"atomic_number": 30, "atomic_weight": 65.38, "group": 12, "period": 4, "pauling_electronegativity": 1.65, "covalent_radius_A": 1.22, "vdw_radius_A": 1.39, "first_ionization_energy_eV": 9.394, "electron_affinity_eV": 0.0, "common_oxidation_state": 2, "d_electron_count_common_oxide": 10, "ionic_radius_shannon_common_ox_cn6_A": 0.74, "oxide_formation_enthalpy_kJ_mol_O": -350.5, "standard_reduction_potential_V": -0.763, "oxide_band_gap_eV": 3.3},
    "Zr": {"atomic_number": 40, "atomic_weight": 91.224, "group": 4, "period": 5, "pauling_electronegativity": 1.33, "covalent_radius_A": 1.75, "vdw_radius_A": 2.23, "first_ionization_energy_eV": 6.634, "electron_affinity_eV": 0.426, "common_oxidation_state": 4, "d_electron_count_common_oxide": 0, "ionic_radius_shannon_common_ox_cn6_A": 0.72, "oxide_formation_enthalpy_kJ_mol_O": -550.3, "standard_reduction_potential_V": -1.553, "oxide_band_gap_eV": 5.0},
}

for _symbol, _data in _ELEMENT_DATA_EXPANDED.items():
    ELEMENT_DATA.setdefault(_symbol, {}).update({key: float(value) for key, value in _data.items()})

_ANIONS = {"O", "N", "F", "Cl", "Br", "I", "S"}
_NEUTRAL = {"C", "H"}


FORMULA_ALIASES: dict[str, str] = {
    "BEA": "Al2Si30O64",
    "ZSM-5": "Al2Si94O192",
}


def parse_formula_to_counts(formula: str) -> dict[str, float] | None:
    compact = str(formula or "").strip()
    compact = FORMULA_ALIASES.get(compact, compact)
    if not compact or any(token in compact for token in {"/", ".", "[", "]", "(", ")", "+", "-", "@", "="}):
        return None
    matches = list(re.finditer(r"([A-Z][a-z]?)(\d*(?:\.\d+)?)", compact))
    if not matches:
        return None
    if "".join(match.group(0) for match in matches) != compact:
        return None
    counts: dict[str, float] = {}
    for match in matches:
        symbol = match.group(1)
        if symbol not in ELEMENT_DATA:
            return None
        count = float(match.group(2)) if match.group(2) else 1.0
        counts[symbol] = counts.get(symbol, 0.0) + count
    return counts or None


def element_descriptor(symbol: str, name: str) -> float | None:
    data = ELEMENT_DATA.get(str(symbol or "").strip())
    if not data:
        return None
    if name in data:
        return float(data[name])
    if name == "vdw_radius_A":
        return None
    return None


def formula_descriptor(formula: str, name: str) -> float | None:
    counts = parse_formula_to_counts(formula)
    if not counts:
        return None
    total_atoms = float(sum(counts.values()))
    if total_atoms <= 0:
        return None
    formula_weight = sum(ELEMENT_DATA[symbol]["atomic_weight"] * count for symbol, count in counts.items())
    if name == "formula_weight_g_mol_formula_unit":
        return float(formula_weight)
    weighted = {
        key: sum(ELEMENT_DATA[symbol][key] * count for symbol, count in counts.items()) / total_atoms
        for key in ("atomic_number", "atomic_weight", "pauling_electronegativity")
    }
    if name == "mean_atomic_number":
        return float(weighted["atomic_number"])
    if name == "mean_atomic_weight":
        return float(weighted["atomic_weight"])
    if name == "mean_pauling_electronegativity":
        return float(weighted["pauling_electronegativity"])
    if name == "std_pauling_electronegativity":
        values = np.asarray(
            [ELEMENT_DATA[symbol]["pauling_electronegativity"] for symbol in counts for _ in range(int(max(counts[symbol], 1)))],
            dtype=float,
        )
        return float(np.std(values)) if len(values) else 0.0
    if name == "oxygen_atomic_fraction":
        return float(counts.get("O", 0.0) / total_atoms)
    if name == "metal_atomic_fraction":
        cations = sum(count for symbol, count in counts.items() if symbol not in _ANIONS and symbol not in _NEUTRAL)
        return float(cations / total_atoms)
    if name == "oxygen_to_metal_ratio":
        cations = sum(count for symbol, count in counts.items() if symbol not in _ANIONS and symbol not in _NEUTRAL)
        if cations <= 0:
            return 0.0
        return float(counts.get("O", 0.0) / cations)
    return None
