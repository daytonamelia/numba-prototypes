from sealir import ase
from sealir.eqsat.rvsdg_convert import egraph_conversion
from sealir.eqsat.rvsdg_eqsat import GraphRoot
from sealir.eqsat.rvsdg_extract import egraph_extraction
from sealir.rvsdg import grammar as rg

from ch03_egraph_program_rewrites import *


def test_ch03_ifelse_fold():
    def ifelse_fold(a, b):
        c = 0
        if c:
            return a
        else:
            return b

    my_ruleset = (
        rvsdg_eqsat.ruleset_rvsdg_basic
        | ruleset_const_propagate
        | ruleset_const_fold_if_else
    )

    jt = compiler_pipeline(ifelse_fold, ruleset=my_ruleset)
    run_test(ifelse_fold, jt, (12, 34))


def test_ch03_ifelse_fold_internal():
    def ifelse_fold(a, b):
        c = 0
        if c:
            return a
        else:
            return b

    def check(fn, ruleset):
        rvsdg_expr, dbginfo = frontend(fn)

        memo = egraph_conversion(rvsdg_expr)

        func = memo[rvsdg_expr]

        egraph = EGraph()
        root = GraphRoot(func)
        egraph.let("root", root)
        egraph.run(ruleset.saturate())

        cost, extracted = egraph_extraction(egraph, rvsdg_expr)
        return [
            cur
            for ps, cur in ase.walk_descendants_depth_first_no_repeat(
                extracted
            )
            if is_if_else(cur)
        ]

    # Check there's no if-else
    def is_if_else(expr):
        match expr:
            case rg.IfElse():
                return True
        return False

    ifelse_nodes = check(
        ifelse_fold,
        ruleset=(rvsdg_eqsat.ruleset_rvsdg_basic | ruleset_const_propagate),
    )
    # folding shouldn't occur
    assert len(ifelse_nodes) == 1

    ifelse_nodes = check(
        ifelse_fold,
        ruleset=(
            rvsdg_eqsat.ruleset_rvsdg_basic
            | ruleset_const_propagate
            | ruleset_const_fold_if_else
        ),
    )
    # folding should occur
    assert len(ifelse_nodes) == 0
