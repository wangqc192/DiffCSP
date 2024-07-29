import argparse
import os
import time
from pathlib import Path

import chemparse
import numpy as np
import pandas as pd
import torch
from p_tqdm import p_map
from pymatgen.core.lattice import Lattice
from pymatgen.core.structure import Structure
from pymatgen.io.cif import CifWriter
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
from pyxtal.symmetry import Group
from torch.optim import Adam
from torch.utils.data import Dataset
from torch_geometric.data import Batch, Data
from torch_geometric.loader import DataLoader
from tqdm import tqdm

# LOCALFOLDER
from eval_utils import get_crystals_list, lattices_to_params_shape, load_model  # isort: skip

chemical_symbols = [
    # 0
    'X',
    # 1
    'H', 'He',
    # 2
    'Li', 'Be', 'B', 'C', 'N', 'O', 'F', 'Ne',
    # 3
    'Na', 'Mg', 'Al', 'Si', 'P', 'S', 'Cl', 'Ar',
    # 4
    'K', 'Ca', 'Sc', 'Ti', 'V', 'Cr', 'Mn', 'Fe', 'Co', 'Ni', 'Cu', 'Zn',
    'Ga', 'Ge', 'As', 'Se', 'Br', 'Kr',
    # 5
    'Rb', 'Sr', 'Y', 'Zr', 'Nb', 'Mo', 'Tc', 'Ru', 'Rh', 'Pd', 'Ag', 'Cd',
    'In', 'Sn', 'Sb', 'Te', 'I', 'Xe',
    # 6
    'Cs', 'Ba', 'La', 'Ce', 'Pr', 'Nd', 'Pm', 'Sm', 'Eu', 'Gd', 'Tb', 'Dy',
    'Ho', 'Er', 'Tm', 'Yb', 'Lu',
    'Hf', 'Ta', 'W', 'Re', 'Os', 'Ir', 'Pt', 'Au', 'Hg', 'Tl', 'Pb', 'Bi',
    'Po', 'At', 'Rn',
    # 7
    'Fr', 'Ra', 'Ac', 'Th', 'Pa', 'U', 'Np', 'Pu', 'Am', 'Cm', 'Bk',
    'Cf', 'Es', 'Fm', 'Md', 'No', 'Lr',
    'Rf', 'Db', 'Sg', 'Bh', 'Hs', 'Mt', 'Ds', 'Rg', 'Cn', 'Nh', 'Fl', 'Mc',
    'Lv', 'Ts', 'Og']  # fmt: skip

def diffusion(loader, model, step_lr):

    frac_coords = []
    num_atoms = []
    atom_types = []
    lattices = []
    input_data_list = []
    for idx, batch in enumerate(loader):

        if torch.cuda.is_available():
            batch.cuda()
        outputs, traj = model.sample(batch, step_lr = step_lr)
        frac_coords.append(outputs['frac_coords'].detach().cpu())
        num_atoms.append(outputs['num_atoms'].detach().cpu())
        atom_types.append(outputs['atom_types'].detach().cpu())
        lattices.append(outputs['lattices'].detach().cpu())

    frac_coords = torch.cat(frac_coords, dim=0)
    num_atoms = torch.cat(num_atoms, dim=0)
    atom_types = torch.cat(atom_types, dim=0)
    lattices = torch.cat(lattices, dim=0)
    lengths, angles = lattices_to_params_shape(lattices)

    return (
        frac_coords, atom_types, lattices, lengths, angles, num_atoms
    )

class SampleDataset(Dataset):

    def __init__(self, formula, num_evals):
        super().__init__()
        self.formula = formula
        self.num_evals = num_evals
        self.get_structure()

    def get_structure(self):
        self.composition = chemparse.parse_formula(self.formula)
        chem_list = []
        for elem in self.composition:
            num_int = int(self.composition[elem])
            chem_list.extend([chemical_symbols.index(elem)] * num_int)
        self.chem_list = chem_list

    def __len__(self) -> int:
        return self.num_evals

    def __getitem__(self, index):
        return Data(
            atom_types=torch.LongTensor(self.chem_list),
            num_atoms=len(self.chem_list),
            num_nodes=len(self.chem_list),
        )

def get_pymatgen(crystal_array):
    frac_coords = crystal_array['frac_coords']
    atom_types = crystal_array['atom_types']
    lengths = crystal_array['lengths']
    angles = crystal_array['angles']
    try:
        structure = Structure(
            lattice=Lattice.from_parameters(
                *(lengths.tolist() + angles.tolist())),
            species=atom_types, coords=frac_coords, coords_are_cartesian=False)
        return structure
    except:
        return None


def load_formula_tabular_file(formula_file):
    with open(formula_file, "r") as f:
        line = f.readline().split()
        if 'formula' not in line:
            print("First line inferred NOT a HEADER, assume no header line")
            header = None
        else:
            header = 0
    formula_tabular = pd.read_csv(formula_file, sep=r'\s+', header=header)
    if header is None:
        print("Assume first column as formulas")
        formula_list = formula_tabular[0].astype(str).tolist()
        if len(formula_tabular.columns) > 1:
            print("Assume second column as num_evals")
            num_evals_list = formula_tabular[1].astype(int).tolist()
        else:
            num_evals_list = None
    else:
        formula_list = formula_tabular["formula"].tolist()
        if "num_evals" in formula_tabular.columns:
            num_evals_list = formula_tabular["num_evals"].astype(int).tolist()
        else:
            num_evals_list = None
    return formula_list, num_evals_list


def main(args):
    print("Loading model...")
    model_path = Path(args.model_path)
    model, _, cfg = load_model(
        model_path, load_data=False)
    if torch.cuda.is_available():
        model.to('cuda')

    if args.formula_file is not None:
        print(f"Trying reading sampling formula and num_evals from '{args.formula_file}'...")
        formula_list, num_evals_list = load_formula_tabular_file(args.formula_file)
        if num_evals_list is None:
            num_evals_list = [args.num_evals for _ in formula_list]
    else:
        formula_list = [args.formula]
        num_evals_list = [args.num_evals]

    for formula, num_evals in zip(formula_list, num_evals_list):
        tar_dir = os.path.join(args.save_path, formula)
        os.makedirs(tar_dir, exist_ok=True)

        print(f'Sampling {formula} times {num_evals}...')

        test_set = SampleDataset(formula, num_evals)
        test_loader = DataLoader(test_set, batch_size = min(args.batch_size, num_evals))

        start_time = time.time()
        (frac_coords, atom_types, lattices, lengths, angles, num_atoms) = diffusion(test_loader, model, args.step_lr)

        crystal_list = get_crystals_list(frac_coords, atom_types, lengths, angles, num_atoms)

        strcuture_list = p_map(get_pymatgen, crystal_list)

        for i,structure in enumerate(strcuture_list):
            tar_file = os.path.join(tar_dir, f"{formula}_{i+1}.cif")
            if structure is not None:
                writer = CifWriter(structure)
                writer.write_file(tar_file)
            else:
                print(f"{i+1} Error Structure.")



if __name__ == '__main__':
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-m', '--model_path', required=True, help="Directory of model, '`pwd`' for example.")
    parser.add_argument('-d', '--save_path', required=True, help="Directory to save results, subdir named by formula.")
    formula_group = parser.add_mutually_exclusive_group(required=True)
    formula_group.add_argument('-f', '--formula')
    formula_group.add_argument('-F', '--formula_file', help="Formula tabular file with HEADER `formula` and `num_evals`(optional), split by WHITESPACE characters.")  # fmt: skip
    parser.add_argument('-n', '--num_evals', default=1, type=int, help="Sampling times of each formula.")
    parser.add_argument('-B', '--batch_size', default=500, type=int, help="How to split sampling times of each formula.")
    parser.add_argument('--step_lr', default=1e-5, type=float, help="step_lr for SDE/ODE.")

    args = parser.parse_args()


    main(args)