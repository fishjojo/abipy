from __future__ import division, print_function, unicode_literals, absolute_import

import os
import tempfile

from monty.termcolor import cprint
from pymatgen.io.abinit.abiobjects import *
from .events import EventsParser, autodoc_event_handlers
from abipy.flowtk.qadapters import show_qparams, all_qtypes
from pymatgen.io.abinit.netcdf import NetcdfReader
from abipy.flowtk.launcher import PyFlowScheduler, PyLauncher
from pymatgen.io.abinit.pseudos import Pseudo, PseudoTable, PseudoParser
from abipy.flowtk.wrappers import Mrgscr, Mrgddb, Mrggkk, Cut3D, Fold2Bloch
from abipy.flowtk.nodes import Status
from abipy.flowtk.tasks import *
from abipy.flowtk.tasks import EphTask, ElasticTask
from abipy.flowtk.works import *
from abipy.flowtk.flows import (Flow, G0W0WithQptdmFlow, bandstructure_flow, PhononFlow,
    g0w0_flow, phonon_flow, phonon_conv_flow, NonLinearCoeffFlow)
from pymatgen.io.abinit.abitimer import AbinitTimerParser, AbinitTimerSection
from pymatgen.io.abinit.abiinspect import GroundStateScfCycle, D2DEScfCycle

#from abipy.flowtk.works import *
#from abipy.flowtk.gs_works import EosWork
from abipy.flowtk.dfpt_works import NscfDdksWork, ElasticWork


def flow_main(main):  # pragma: no cover
    """
    This decorator is used to decorate main functions producing `Flows`.
    It adds the initialization of the logger and an argument parser that allows one to select
    the loglevel, the workdir of the flow as well as the YAML file with the parameters of the `TaskManager`.
    The main function shall have the signature:

        main(options)

    where options in the container with the command line options generated by `ArgumentParser`.

    Args:
        main: main function.
    """
    from functools import wraps

    @wraps(main)
    def wrapper(*args, **kwargs):
        # Build the parse and parse input args.
        parser = build_flow_main_parser()
        options = parser.parse_args()

        # loglevel is bound to the string value obtained from the command line argument.
        # Convert to upper case to allow the user to specify --loglevel=DEBUG or --loglevel=debug
        import logging
        numeric_level = getattr(logging, options.loglevel.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError('Invalid log level: %s' % options.loglevel)
        logging.basicConfig(level=numeric_level)

        # Istantiate the manager.
        options.manager = TaskManager.as_manager(options.manager)

        if options.tempdir:
            options.workdir = tempfile.mkdtemp()
            print("Working in temporary directory", options.workdir)

        def execute():
            """This is the function that performs the work depending on options."""
            flow = main(options)

            if options.plot:
                flow.plot_networkx(tight_layout=True, with_edge_labels=True)

            if options.graphviz:
                graph = flow.get_graphviz() #engine=options.engine)
                directory = tempfile.mkdtemp()
                print("Producing source files in:", directory)
                graph.view(directory=directory, cleanup=False)

            if options.abivalidate:
                print("Validating flow input files...")
                isok, errors = flow.abivalidate_inputs()
                if not isok:
                    for e in errors:
                        if e.retcode == 0: continue
                        lines = e.log_file.readlines()
                        i = len(lines) - 50 if len(lines) >= 50 else 0
                        print("Last 50 line from logfile:")
                        print("".join(lines[i:]))
                    raise RuntimeError("flow.abivalidate_input failed. See messages above.")
                else:
                    print("Validation succeeded")

            if options.remove and os.path.isdir(flow.workdir):
                print("Removing old directory:", flow.workdir)
                import shutil
                shutil.rmtree(flow.workdir)

            if options.dry_run:
                print("Dry-run mode.")
                retcode = 0
            elif options.scheduler:
                retcode = flow.make_scheduler().start()
                if retcode == 0:
                    retcode = 0 if flow.all_ok else 1
            elif options.batch:
                retcode = flow.batch()
            else:
                # Default behaviour.
                retcode = flow.build_and_pickle_dump()

            cprint("Return code: %d" % retcode, "red" if retcode != 0 else "green")
            return retcode

        if options.prof:
            # Profile execute
            import pstats, cProfile
            cProfile.runctx("execute()", globals(), locals(), "Profile.prof")
            s = pstats.Stats("Profile.prof")
            s.strip_dirs().sort_stats("time").print_stats()
            return 0
        else:
            return execute()

    return wrapper


def build_flow_main_parser():
    """
    Build and return the parser used in the abipy/data/runs scripts.
    """
    import argparse
    parser = argparse.ArgumentParser()

    parser.add_argument('--loglevel', default="ERROR", type=str,
                        help="set the loglevel. Possible values: CRITICAL, ERROR (default), WARNING, INFO, DEBUG")
    parser.add_argument("-w", '--workdir', default="", type=str, help="Working directory of the flow.")
    parser.add_argument("-m", '--manager', default=None,
                        help="YAML file with the parameters of the task manager. "
                             "Default None i.e. the manager is read from standard locations: "
                             "working directory first then ~/.abinit/abipy/manager.yml.")
    parser.add_argument("-s", '--scheduler', action="store_true", default=False,
                        help="Run the flow with the scheduler")
    parser.add_argument("-b", '--batch', action="store_true", default=False, help="Run the flow in batch mode")
    parser.add_argument("-r", "--remove", default=False, action="store_true", help="Remove old flow workdir")
    parser.add_argument("-p", "--plot", default=False, action="store_true", help="Plot flow with networkx.")
    parser.add_argument("-g", "--graphviz", default=False, action="store_true", help="Plot flow with graphviz.")
    parser.add_argument("-d", "--dry-run", default=False, action="store_true", help="Don't write directory with flow.")
    parser.add_argument("-a", "--abivalidate", default=False, action="store_true", help="Call Abinit to validate input files.")
    parser.add_argument("-t", "--tempdir", default=False, action="store_true", help="Execute flow in temporary directory.")
    parser.add_argument("--prof", action="store_true", default=False, help="Profile code wth cProfile ")

    return parser
