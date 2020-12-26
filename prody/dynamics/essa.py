from collections import defaultdict
from os import chdir, listdir, mkdir, system
from os.path import isdir
from pickle import dump
from re import findall
from numpy import argsort, arange, array, c_, count_nonzero, hstack, mean, median, quantile, save, where
from scipy.stats import zscore, median_absolute_deviation
import matplotlib.pyplot as plt
from prody import LOGGER
from prody.atomic.functions import extendAtomicData
from .anm import ANM
from .gnm import GNM
from prody.proteins import parsePDB, writePDB
from .editing import reduceModel
from .plotting import showAtomicLines
from .signature import ModeEnsemble, saveModeEnsemble
from prody.utilities import which
from . import matchModes

__all__ = ['ESSA']


class ESSA:

    '''
    ESSA determines the essentiality score of each residue based on the extent to which it can alter the global dynamics ([KB20]_). It can also rank potentially allosteric pockets by calculating their ESSA and local hydrophibic density z-scores using Fpocket algorithm ([LGV09]_). 

    .. [KB20] Kaynak B.T., Bahar I., Doruker P., Essential site scanning analysis: A new approach for detecting sites that modulate the dispersion of protein global motions, *Comput. Struct. Biotechnol. J.* **2020** 18:1577-1586.

    .. [LGV09] Le Guilloux, V., Schmidtke P., Tuffery P., Fpocket: An open source platform for ligand pocket detection, *BMC Bioinformatics* **2009** 10:168.

    Instantiate an ESSA object.
    '''

    _single = {'GLY': 'G', 'ALA': 'A', 'LEU': 'L', 'MET': 'M',
               'PHE': 'F', 'TRP': 'W', 'LYS': 'K', 'GLN': 'Q',
               'GLU': 'E', 'SER': 'S', 'PRO': 'P', 'VAL': 'V',
               'ILE': 'I', 'CYS': 'C', 'TYR': 'Y', 'HIS': 'H',
               'ARG': 'R', 'ASN': 'N', 'ASP': 'D', 'THR': 'T'}
    
    def __init__(self):

        self._atoms = None
        self._title = None
        self._lig = None
        self._heavy = None
        self._ca = None
        self._n_modes = None
        self._enm = None
        self._cutoff = None
        self._lig = None
        self._ligres_idx = None
        self._ligres_code = None
        self._rib = None
        self._ri = None
        self._chrn = None
        self._dist = None
        self._ensemble = None
        self._labels = None
        self._zscore = None
        self._ref = None
        self._eigvals = None
        self._eigvecs = None

    def setSystem(self, atoms, **kwargs):

        '''
        Sets atoms, ligands and a cutoff distance for protein-ligand interactions.

        :arg atoms: *atoms* parsed by parsePDB

        :arg lig: String of ligands' chainIDs and resSeqs (resnum) separated by a whitespace,
            e.g., 'A 300 B 301'. Default is None.
        :type lig: str

        :arg dist: Atom-atom distance (A) to select the protein residues that are in contact with a ligand, default is 4.5 A.
        :type dist: float

        :arg lowmem: If True, a ModeEnsemble is not generated due to the lack of memory resources, and eigenvalue/eigenvectors are only stored, default it False.
        :type lowmem: bool
        '''

        self._atoms = atoms
        self._title = atoms.getTitle()
        self._lig = kwargs.pop('lig', None)
        if self._lig:
            self._ligres_idx = {}
            self._ligres_code = {}
            self._dist = kwargs.pop('dist', 4.5)

        self._heavy = atoms.select('protein and heavy and not hetatm')
        self._ca = self._heavy.ca

        self._lowmem = kwargs.pop('lowmem', False)
        if self._lowmem:
            self._eigvals = []
            self._eigvecs = []

        self._chrn = array([ch + str(rn)
                            for ch, rn in zip(self._ca.getChids(),
                                              self._ca.getResnums())])

        self._rib = all(self._ca.getResindices() == arange(self._ca.numAtoms()))
        if not self._rib:
            self._ri = {v: k for k, v in enumerate(self._ca.getResindices())}

        # --- resindices of protein residues that are within dist A of ligands --- #

        if self._lig:
            ligs = self._lig.split()
            ligs = list(zip(ligs[::2], ligs[1::2]))
            for chid, resnum in ligs:
                key = ''.join(chid + str(resnum))
                sel_lig = 'calpha and not hetatm and (same residue as ' \
                          f'exwithin {self._dist} of (chain {chid} and resnum {resnum}))'
                self._ligres_idx[key] = self._atoms.select(sel_lig).getResindices()

            for k, v in self._ligres_idx.items():
                atoms = self._ca.select('resindex ' + ' '.join([str(i) for i in v]))
                tmp0 = defaultdict(list)
                for ch, rn in zip(atoms.getChids(), atoms.getResnums()):
                    tmp0[ch].append(str(rn))
                tmp1 = {ch: ' '.join(rn) for ch, rn in tmp0.items()}
                self._ligres_code[k] = [f'chain {ch} and resnum {rn}' for ch, rn in tmp1.items()]

    def scanResidues(self, n_modes=10, enm='gnm', cutoff=None):

        '''
        Scans residues to generate ESSA z-scores.

        :arg n_modes: Number of global modes.
        :type n_modes: int

        :arg enm: Type of elastic network model, 'gnm' or 'anm', default is 'gnm'.
        :type enm: str

        :arg cutoff: Cutoff distance (A) for pairwise interactions, default is 10 A for GNM and 15 A for ANM.
        :type cutoff: float
        '''

        self._n_modes = n_modes
        self._enm = enm
        self._cutoff = cutoff

        self._ensemble = ModeEnsemble(f'{self._title}')
        self._ensemble.setAtoms(self._ca)
        self._labels = ['ref']

        # --- reference model --- #

        self._reference()

        # --- perturbed models --- #

        LOGGER.progress(msg='', steps=(self._ca.numAtoms()))
        for i in self._ca.getResindices():
            LOGGER.update(step=i+1, msg=f'scanning residue {i+1}')
            self._perturbed(i)

        if self._lowmem:
            self._eigvals = array(self._eigvals)
            self._eigvecs = array(self._eigvecs)

        # --- ESSA computation part --- #

        if self._lowmem:
            denom = self._eigvals[0]
            num = self._eigvals[1:] - denom
        else:
            self._ensemble.setLabels(self._labels)
            self._ensemble.match()

            denom = self._ensemble[0].getEigvals()
            num = self._ensemble[1:].getEigvals() - denom

        eig_diff = num / denom * 100
        eig_diff_mean = mean(eig_diff, axis=1)

        self._zscore = zscore(eig_diff_mean)

        if self._lig:
            self._zs_lig = {}
            for k, v in self._ligres_idx.items():
                if self._rib:
                    self._zs_lig[k] = [array(v), self._zscore[v]]
                else:
                    vv = [self._ri[i] for i in v]
                    self._zs_lig[k] = [array(vv), self._zscore[vv]]

    def _reference(self):

        if self._enm == 'gnm':
            ca_enm = GNM('ca')
            if self._cutoff is not None:
                ca_enm.buildKirchhoff(self._ca, cutoff=self._cutoff)
            else:
                ca_enm.buildKirchhoff(self._ca)
                self._cutoff = ca_enm.getCutoff()

        if self._enm == 'anm':
            ca_enm = ANM('ca')
            if self._cutoff is not None:
                ca_enm.buildHessian(self._ca, cutoff=self._cutoff)
            else:
                ca_enm.buildHessian(self._ca)
                self._cutoff = ca_enm.getCutoff()

        ca_enm.calcModes(n_modes=self._n_modes)

        if self._lowmem:
            self._ref = ca_enm
            self._eigvals.append(ca_enm.getEigvals())
            self._eigvecs.append(ca_enm.getEigvecs())
        else:
            self._ensemble.addModeSet(ca_enm[:])

    def _perturbed(self, arg):

        sel = f'calpha or resindex {arg}'
        tmp = self._heavy.select(sel)

        if self._enm == 'gnm':
            tmp_enm = GNM(f'res_{arg}')
            tmp_enm.buildKirchhoff(tmp, cutoff=self._cutoff)

        if self._enm == 'anm':
            tmp_enm = ANM(f'res_{arg}')
            tmp_enm.buildHessian(tmp, cutoff=self._cutoff)

        tmp_enm_red, _ = reduceModel(tmp_enm, tmp, self._ca)
        tmp_enm_red.calcModes(n_modes=self._n_modes)
        tmp_enm_red.setTitle(tmp_enm_red.getTitle().split()[0])

        if self._lowmem:
            _, matched = matchModes(self._ref, tmp_enm_red)
            self._eigvals.append(matched.getEigvals())
            self._eigvecs.append(matched.getEigvecs())
        else:
            self._ensemble.addModeSet(tmp_enm_red[:])

        self._labels.append(tmp_enm_red.getTitle())

    def getESSAZscores(self):

        'Returns ESSA z-scores.'

        return self._zscore

    def getESSAEnsemble(self):

        'Returns ESSA mode ensemble, comprised of ENMS calculated for each scanned/perturbed residue.'

        if self._lowmem:
            LOGGER.warn('ModeEnsemble was not generated due to lowmem=True')
        else:
            return self._ensemble[:]
    
    def saveESSAEnsemble(self):

        'Saves ESSA mode ensemble, comprised of ENMS calculated for each scanned/perturbed residue.'

        if self._lowmem:
            LOGGER.warn('ModeEnsemble was not generated due to lowmem=True')
        else:
            saveModeEnsemble(self._ensemble, filename=f'{self._title}_{self._enm}')

    def saveESSAZscores(self):

        'Saves ESSA z-scores to a binary file in Numpy `.npy` format.'

        save(f'{self._title}_{self._enm}_zs', self._zscore)

    def writeESSAZscoresToPDB(self):

        'Writes a pdb file with ESSA z-scores placed in the B-factor column.'

        writePDB(f'{self._title}_{self._enm}_zs', self._heavy,
                 beta=extendAtomicData(self._zscore, self._ca, self._heavy)[0])

    def getEigvals(self):

        'Returns eigenvalues of the matched modes.'

        if self._lowmem:
            return self._eigvals
        else:
            return self._ensemble.getEigvals()

    def getEigvecs(self):

        'Returns eigenvectors of the matched modes.'

        if self._lowmem:
            return self._eigvecs
        else:
            return self._ensemble.getEigvecs()

    def saveEigvals(self):

        'Saves eigenvalues of the matched modes in Numpy `.npy` format.'

        if self._lowmem:
            save(f'{self._title}_{self._enm}_eigvals', self._eigvals)
        else:
            save(f'{self._title}_{self._enm}_eigvals', self._ensemble.getEigvals())

    def saveEigvecs(self):

        'Saves eigenvectors of the matched modes in Numpy `.npy` format.'

        if self._lowmem:
            save(f'{self._title}_{self._enm}_eigvecs', self._eigvecs)
        else:
            save(f'{self._title}_{self._enm}_eigvecs', self._ensemble.getEigvecs())

    def getLigandResidueESSAZscores(self):

        'Returns ESSA Z-scores and their indices (0-based) of the residues interacting with ligands as dictionary. The keys of which are their chan ids and residue numbers.'
        
        if self._lig:
            return self._zs_lig
        else:
            LOGGER.warning('No ligand provided.')
        
    def getLigandResidueIndices(self):

        'Returns residue indices of the residues interacting with ligands.'

        if self._lig:
            return self._ligres_idx
        else:
            LOGGER.warning('No ligand provided.')

    def getLigandResidueCodes(self):

        'Returns chain ids and residue numbers of the residues interacting with ligands.'

        if self._lig:
            return self._ligres_code
        else:
            LOGGER.warning('No ligand provided.')

    def saveLigandResidueCodes(self):

        'Saves chain ids and residue numbers of the residues interacting with ligands.'

        if self._lig:
            with open(f'{self._title}_ligand_rescodes.txt', 'w') as f:
                for k, v in self._ligres_code.items():
                    f.write(k + '\n')
                    for x in v:
                        f.write(x + '\n')
        else:
            LOGGER.warning('No ligand provided.')

    def _codes(self, arg):

        sel = self._ca.select(f'resindex {arg}')
        try:
            return self._single[sel.getResnames()[0]] + str(sel.getResnums()[0])
        except KeyError:
            return sel.getResnames()[0] + str(sel.getResnums()[0])

    def showESSAProfile(self, q=.75, rescode=False, sel=None):

        '''
        Shows ESSA profile.

        :arg q: Quantile value to plot a baseline for z-scores, default is 0.75. If it is set to 0.0, then the baseline is not drawn.
        :type q: float

        :arg rescode: If it is True, the ligand interacting residues with ESSA scores larger than the baseline are denoted by their single letter codes and chain ids on the profile. If quantile vaue is 0.0, then all ligand-interacting residues denoted by their codes.
        :type rescode: bool

        :arg sel: It is a selection string, default is None. If it is provided, then selected residues are shown with their single letter codes and residue numbers on the profile. For example, 'chain A and resnum 33 47'.
        :type sel: str
        '''

        showAtomicLines(self._zscore, atoms=self._ca, c='k', linewidth=1.)

        if self._lig:
            for k in self._zs_lig.keys():
                plt.scatter(*self._zs_lig[k], label=k)
                if rescode:
                    if q != 0.0:
                        idx = where(self._zs_lig[k][1] >= quantile(self._zscore, q=q))[0]
                        if idx.size > 0:
                            _x = self._zs_lig[k][0][idx]
                            _y = self._zs_lig[k][1][idx]
                            _i = self._ligres_idx[k][idx]
                        else:
                            break
                    else:
                        _x = self._zs_lig[k][0]
                        _y = self._zs_lig[k][1]
                        _i = self._ligres_idx[k]
                    for x, y, i in zip(_x, _y, _i):
                        plt.text(x, y, self._codes(i), color='r')
            plt.legend()

        if q != 0.0:
            plt.hlines(quantile(self._zscore, q=q),
                       xmin=0., xmax=self._ca.numAtoms(),
                       linestyle='--', color='c')

        if sel:
            idx = self._ca.select(sel).getResindices()
            if self._rib:
                _x = idx
                zs_sel = self._zscore[_x]
            else:
                _x = [self._ri[i] for i in idx]
                zs_sel = self._zscore[_x]

            plt.scatter(_x, zs_sel)
            for x, y, i in zip(_x, zs_sel, idx):
                plt.text(x, y, self._codes(i), color='r')
            

        plt.xlabel('Residue')
        plt.ylabel('Z-Score')

        plt.tight_layout()

    def scanPockets(self):

        'Generates ESSA z-scores for pockets and parses pocket features. It requires both Fpocket 3.0 and Pandas being installed in your system.'

        fpocket = which('fpocket')

        if fpocket is None:
            LOGGER.warning('Fpocket 3.0 was not found, please install it.')
            return None

        try:
            from pandas import Index, DataFrame
        except ImportError as ie:
            LOGGER.warning(ie.__str__() + ' was found, please install it.')
            return None

        rcr = {(i, j): k if self._rib else self._ri[k]
               for i, j, k in zip(self._ca.getChids(),
                                  self._ca.getResnums(),
                                  self._ca.getResindices())}

        writePDB(f'{self._title}_pro', self._heavy)

        direc = f'{self._title}_pro_out'
        if not isdir(direc):
            system(f'fpocket -f {self._title}_pro.pdb')

        chdir(direc + '/pockets')
        l = [x for x in listdir('.') if x.endswith('.pdb')]
        l.sort(key=lambda x:int(x.partition('_')[0][6:]))

        ps = []
        for x in l:
            with open(x, 'r') as f:
                tmp0 = f.read()
                tmp1 = [(x[1].strip(), float(x[2])) for x in findall(r'(\w+\s\w+\s*-\s*)(.+):\s*([\d.-]+)(\n)', tmp0)]
            fea, sco = list(zip(*tmp1))
            ps.append(sco)
        pdbs = parsePDB(l)
        chdir('../..')

        # ----- # ----- #

        ps = array(ps)

        pcn = {int(pdb.getTitle().partition('_')[0][6:]):
               set(zip(pdb.getChids().tolist(),
                       pdb.getResnums().tolist())) for pdb in pdbs}
        pi = {p: [rcr[x] for x in crn] for p, crn in pcn.items()}

        pzs_max = {k: max(self._zscore[v]) for k, v in pi.items()}
        pzs_med = {k: median(self._zscore[v]) for k, v in pi.items()}

        # ----- # ----- #

        indices = Index(range(1, ps.shape[0] + 1), name='Pocket #')

        columns = Index(fea, name='Feature')

        self._df = DataFrame(index=indices, columns=columns, data=ps)

        # ----- # ----- #

        columns_zs = Index(['ESSA_max',
                            'ESSA_med',
                            'LHD'],
                           name='Z-score')

        zps = c_[list(pzs_max.values())]
        zps = hstack((zps, c_[list(pzs_med.values())]))
        zps = hstack((zps, zscore(self._df[['Local hydrophobic density Score']])))


        self._df_zs = DataFrame(index=indices, columns=columns_zs, data=zps)

    def rankPockets(self):

        'Ranks pockets in terms of their allosteric potential, based on their ESSA z-scores (max/median) with local hydrophobic density (LHD) screening.'

        from pandas import DataFrame, Index

        lhd = self._df_zs.loc[:, 'LHD']
        n = count_nonzero(lhd >= 0.)
        q = quantile(lhd, q=.85)

        # ----- # ------ #

        s_max = ['ESSA_max', 'LHD']

        zf_max = self._df_zs[s_max].copy()

        if n >= lhd.size // 4:
            f_max = zf_max.iloc[:, 1] >= 0.
        else:
            f_max = zf_max.iloc[:, 1] >= q

        zf_max = zf_max[f_max]

        zf_max.iloc[:, 0] = zf_max.iloc[:, 0].round(1)
        zf_max.iloc[:, 1] = zf_max.iloc[:, 1].round(2)

        self._idx_max = zf_max.sort_values(s_max, ascending=False).index

        # ----- # ----- #

        s_med = ['ESSA_med',
                 'LHD']

        zf_med = self._df_zs[s_med].copy()

        if n >= lhd.size // 4:
            f_med = zf_med.iloc[:, 1] >= 0.
        else:
            f_med = zf_med.iloc[:, 1] >= q

        zf_med = zf_med[f_med]

        zf_med.iloc[:, 0] = zf_med.iloc[:, 0].round(1)
        zf_med.iloc[:, 1] = zf_med.iloc[:, 1].round(2)

        self._idx_med = zf_med.sort_values(s_med, ascending=False).index

        ranks = Index(range(1, zf_max.shape[0] + 1), name='Allosteric potential / Rank')

        self._pocket_ranks = DataFrame(index=ranks, columns=['Pocket # (ESSA_max & LHD)',
                                                             'Pocket # (ESSA_med & LHD)'])

        self._pocket_ranks.iloc[:, 0] = self._idx_max
        self._pocket_ranks.iloc[:, 1] = self._idx_med

    def getPocketFeatures(self):

        'Returns pocket features as a Pandas dataframe.'

        return self._df

    def getPocketZscores(self):

        'Returns ESSA and local hydrophobic density (LHD) z-scores for pockets as a Pandas dataframe.'

        return self._df_zs

    def getPocketRanks(self):

        'Returns pocket ranks (allosteric potential).'

        return self._pocket_ranks
    
    def showPocketZscores(self):

        'Plots maximum/median ESSA and local hydrophobic density (LHD) z-scores for pockets.'

        with plt.style.context({'xtick.major.size': 10, 'xtick.labelsize': 30,
                                'ytick.major.size': 10, 'ytick.labelsize': 30,
                                'axes.labelsize': 35, 'legend.fontsize': 25,
                                'legend.title_fontsize': 0}):
            self._df_zs[['ESSA_max',
                         'ESSA_med',
                         'LHD']].plot.bar(figsize=(25, 10))
            plt.xticks(rotation=0)
            plt.xlabel('Pocket')
            plt.ylabel('Z-score')
            plt.tight_layout()

    def savePocketFeatures(self):

        'Saves pocket features to a pickle `.pkl` file.'

        self._df.to_pickle(f'{self._title}_pocket_features.pkl')

    def savePocketZscores(self):

        'Saves ESSA and local hydrophobic density (LHD) z-scores of pockets to a pickle `.pkl` file.'

        self._df_zs.to_pickle(f'{self._title}_pocket_zscores.pkl')

    def savePocketRanks(self):

        'Saves pocket ranks to a binary file in Numpy `.npy` format.'

        save(f'{self._title}_{self._enm}_pocket_ranks_wrt_ESSAmax_LHD',
             self._idx_max)
        save(f'{self._title}_{self._enm}_pocket_ranks_wrt_ESSAmed_LHD',
             self._idx_med)
        
    def writePocketRanksToCSV(self):

        'Writes pocket ranks to a `.csv` file.'

        self._pocket_ranks.to_csv(f'{self._title}_{self._enm}_pocket_ranks.csv', index=False)
