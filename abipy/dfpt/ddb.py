# coding: utf-8
"""DDB File."""
from __future__ import print_function, division, unicode_literals

import os
import tempfile
import numpy as np

from monty.collections import AttrDict
from monty.functools import lazy_property
from pymatgen.io.abinitio.tasks import AnaddbTask, TaskManager
from abipy.core.mixins import Has_Structure
from abipy.core.symmetries import SpaceGroup
from abipy.core.structure import Structure
from abipy.htc.input import AnaddbInput

import logging
logger = logging.getLogger(__name__)


class TaskException(Exception):
    """
    Exceptions raised when we try to execute AnaddbTasks in the :class:`DdbFile` methods

    A TaskException has a reference to the task and to the :class:`EventsReport`.
    """
    def __init__(self, *args, **kwargs):
        self.task = kwargs.pop("task")
        self.report = kwargs.pop("report")
        super(TaskException, self).__init__(*args, **kwargs)

    def __str__(self):
        lines = ["\nworkdir = %s" % self.task.workdir]
        app = lines.append

        if self.report.errors: 
            app("Found %d errors" % len(self.report.errors))
            lines += [str(err) for err in self.report.errors]

        if self.report.bugs: 
            app("Found %d bugs" % len(self.report.bugs))
            lines += [str(bug) for bug in self.report.bugs]

        return "\n".join(lines)


class DdbFile(Has_Structure):
    """
    This object provides an interface to the DDB file produced by ABINIT
    as well as methods to compute phonon band structures, phonon DOS, thermodinamical properties ...
    """
    @classmethod
    def from_file(cls, filepath):
        return cls(filepath)

    def __init__(self, filepath):
        self.filepath = os.path.abspath(filepath)

    def __enter__(self):
        # Open the file
        self._file
        return self

    def __iter__(self):
        return iter(self._file)

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Activated at the end of the with statement. It automatically closes the file."""
        self.close()

    @lazy_property
    def _file(self):
        return open(self.filepath, mode="rt")

    def close(self):
        try:
            self._file.close()
        except:
            pass

    def seek(self, offset, whence=0):
        """Set the file's current position, like stdio's fseek()."""
        self._file.seek(offset, whence)

    @lazy_property
    def structure(self):
        structure = Structure.from_abivars(**self.header)
        # FIXME: has_timerev is always True
        spgid, has_timerev, h = 0, True, self.header
        structure.set_spacegroup(SpaceGroup(spgid, h.symrel, h.tnons, h.symafm, has_timerev))
        return structure

    @lazy_property
    def header(self):
        """
        Dictionary with the values reported in the header section. 
        Use ddb.header.ecut to access its values
        """
        return self._parse_header()

    def _parse_header(self):
        """Parse the header sections. Returns :class:`AttrDict` dictionary."""
        #ixc         7
        #kpt  0.00000000000000D+00  0.00000000000000D+00  0.00000000000000D+00
        #     0.25000000000000D+00  0.00000000000000D+00  0.00000000000000D+00
        self.seek(0)
        keyvals, in_header = [], False
        for i, line in enumerate(self):
            line = line.strip()
            if not line: continue
            if "Version" in line:
                # +DDB, Version number    100401
                version = int(line.split()[-1])

            if line == "Description of the potentials (KB energies)":
                # Skip section with psps info.
                break
            if i == 6: in_header = True

            if in_header:
                # Python does not support exp format with D 
                line = line.replace("D+", "E+").replace("D-", "E-")
                tokens = line.split()
                key = None
                try:
                    float(tokens[0])
                    parse = float if "." in tokens[0] else int
                    keyvals[-1][1].extend(map(parse, tokens))
                except ValueError:
                    # We have a new key
                    key = tokens.pop(0)
                    parse = float if "." in tokens[0] else int
                    keyvals.append((key, map(parse, tokens)))

        h = AttrDict(version=version)
        for key, value in keyvals:
            if len(value) == 1: value = value[0]
            h[key] = value

        # Convert to array. Note that znucl is converted into integer
        # to avoid problems with pymatgen routines that expect integral Z
        # This of course will break any code for alchemical mixing.
        arrays = {
            "kpt": dict(shape=(h.nkpt, 3), dtype=np.double),
            "rprim": dict(shape=(3, 3), dtype=np.double),
            "symrel": dict(shape=(h.nsym, 3, 3), dtype=np.int),
            "tnons": dict(shape=(h.nsym, 3), dtype=np.double),
            "xred":  dict(shape=(h.natom, 3), dtype=np.double),
            "znucl": dict(shape=(-1,), dtype=np.int),
        }

        for k, ainfo in arrays.items():
            h[k] = np.reshape(np.array(h[k], dtype=ainfo["dtype"]), ainfo["shape"])

        # Transpose symrel because Abinit write matrices by colums.
        h.symrel = np.array([s.T for s in h.symrel])
        
        return h

    @lazy_property
    def qpoints(self):
        """`ndarray` with the list of q-points in reduced coordinates."""
        return self._read_qpoints()

    def _read_qpoints(self):
        """Read the list q-points from the DDB file. Returns `ndarray`"""
        # 2nd derivatives (non-stat.)  - # elements :      36
        # qpt  2.50000000E-01  0.00000000E+00  0.00000000E+00   1.0

        # Since there are multiple occurrences of qpt in the DDB file
        # we use seen to remove duplicates.
        self.seek(0)
        tokens, seen = [], set()

        for line in self:
            line = line.strip()
            if line.startswith("qpt") and line not in seen:
                seen.add(line)
                tokens.append(line.replace("qpt", ""))

        qpoints, weights = [], []
        for tok in tokens:
            nums = map(float, tok.split())
            qpoints.append(nums[:3])
            weights.append(nums[3])

        return np.reshape(qpoints, (-1,3))

    @lazy_property
    def guessed_ngqpt(self):
        """
        This function tries to figure out the value of ngqpt from the list of 
        points reported in the DDB file.

        .. warning::
            
            The mesh may not be correct if the DDB file contains points belonging 
            to different meshes.
        """
        # Build the union of the stars of the q-points
        all_qpoints = np.empty((len(self.qpoints) * len(self.structure.spacegroup), 3))
        count = 0
        for qpoint in self.qpoints:
            for op in self.structure.spacegroup:
                all_qpoints[count] = op.rotate_k(qpoint, wrap_tows=False)
                count += 1

        for q in all_qpoints: 
            q[q == 0] = np.inf
        #print(all_qpoints)

        smalls = np.abs(all_qpoints).min(axis=0)
        smalls[smalls == 0] = 1
        ngqpt = np.rint(1 / smalls)
        ngqpt[ngqpt == 0] = 1
        #print("smalls: ", smalls, "ngqpt", ngqpt)

        return np.array(ngqpt, dtype=np.int)

    def calc_phmodes_at_qpoint(self, qpoint=None, asr=2, chneut=1, dipdip=1, 
                               workdir=None, manager=None, verbose=0, **kwargs):
        """
        Execute anaddb to compute phonon modes at the given q-point.

        Args:
            asr, chneut, dipdp: Anaddb input variable. See official documentation.
            workdir: Working directory. If None, a temporary directory is created.
            manager: :class:`TaskManager` object. If None, the object is initialized from the configuration file
            verbose: verbosity level. Set it to a value > 0 to get more information
            kwargs: Additional variables you may want to pass to Anaddb.
        """
        if qpoint is None:
            qpoint = self.qpoints[0] 
            if len(self.qpoints) != 1:
                raise ValueError("%s contains %s qpoints and the choice is ambiguous.\n" 
                                 "Please specify the qpoint in calc_phmodes_at_qpoint" % (self, len(self.qpoints)))

        inp = AnaddbInput.modes_at_qpoint(self.structure, qpoint, asr=asr, chneut=chneut, dipdip=dipdip)

        if manager is None: manager = TaskManager.from_user_config()
        if workdir is None: workdir = tempfile.mkdtemp()
        if verbose: 
            print("workdir:", workdir)
            print("ANADDB INPUT:\n", inp)

        task = AnaddbTask(inp, self.filepath, workdir=workdir, manager=manager.to_shell_manager(mpi_procs=1))
        task.start_and_wait(autoparal=False)

        report = task.get_event_report()
        if not report.run_completed:
            raise TaskException(task=task, report=report)

        return task.open_phbst()

    def calc_phbands_and_dos(self, ngqpt=None, ndivsm=20, nqsmall=10, workdir=None, manager=None, verbose=0, **kwargs):
        """
        Execute anaddb to compute phonon band structure and phonon DOS

        Args:
            asr, chneut, dipdp: Anaddb input variable. See official documentation.
            workdir: Working directory. If None, a temporary directory is created.
            manager: :class:`TaskManager` object. If None, the object is initialized from the configuration file
            verbose: verbosity level. Set it to a value > 0 to get more information
            kwargs: Additional variables you may want to pass to Anaddb.
        """
        if ngqpt is None: ngqpt = self.guessed_ngqpt

        inp = AnaddbInput.phbands_and_dos(
            self.structure, ngqpt, ndivsm, nqsmall, 
            q1shft=(0,0,0), qptbounds=None, asr=2, chneut=0, dipdip=1, dos_method="tetra", **kwargs)

        if manager is None: manager = TaskManager.from_user_config()
        if workdir is None: workdir = tempfile.mkdtemp()
        if verbose: 
            print("workdir:", workdir)
            print("ANADDB INPUT:\n", inp)

        task = AnaddbTask(inp, self.filepath, workdir=workdir, manager=manager.to_shell_manager(mpi_procs=1))
        task.start_and_wait(autoparal=False)

        report = task.get_event_report()
        if not report.run_completed:
            raise TaskException(task=task, report=report)

        return task.open_phbst(), task.open_phdos()

    def calc_thermo(self, nqsmall, ngqpt=None, workdir=None, manager=None, verbose=0, **kwargs):
        """
        Execute anaddb to compute tehrmodinamical properties.

        Args:
            asr, chneut, dipdp: Anaddb input variable. See official documentation.
            workdir: Working directory. If None, a temporary directory is created.
            manager: :class:`TaskManager` object. If None, the object is initialized from the configuration file
            verbose: verbosity level. Set it to a value > 0 to get more information
            kwargs: Additional variables you may want to pass to Anaddb.
        """
        if ngqpt is None: ngqpt = self.guessed_ngqpt

        inp = AnaddbInput.thermo(self.structure, ngqpt, nqsmall, q1shft=(0, 0, 0), nchan=1250, nwchan=5, thmtol=0.5,
               ntemper=199, temperinc=5, tempermin=5., asr=2, chneut=1, dipdip=1, ngrids=10, **kwargs)

        if manager is None: manager = TaskManager.from_user_config()
        if workdir is None: workdir = tempfile.mkdtemp()
        if verbose: 
            print("workdir:", workdir)
            print("ANADDB INPUT:\n", inp)

        task = AnaddbTask(inp, self.filepath, workdir=workdir, manager=manager.to_shell_manager(mpi_procs=1))
        task.start_and_wait(autoparal=False)

        report = task.get_event_report()
        if not report.run_completed:
            raise TaskException(task=task, report=report)

        #return task.open_phbst()
