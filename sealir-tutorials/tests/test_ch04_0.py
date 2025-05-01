from sealir import ase

from ch04_0_typeinfer_prelude import *


def check(fn, ruleset):
    rvsdg_expr, dbginfo = frontend(fn)

    memo = egraph_conversion(rvsdg_expr)

    func = memo[rvsdg_expr]

    egraph = EGraph()
    root = GraphRoot(func)
    egraph.let("root", root)
    egraph.run(ruleset.saturate())

    cost, extracted = egraph_extraction(
        egraph, rvsdg_expr, converter_class=ExtendEGraphToRVSDG
    )
    return extracted


def test_ch04_0_code_functioning():
    """
    Test the final code that uses everything
    """
    jt = compiler_pipeline(
        chained_additions,
        ruleset=optimized_ruleset,
        converter_class=ExtendEGraphToRVSDG,
        codegen_extension=codegen_extension,
        cost_model=MyCostModel(),
    )
    run_test(chained_additions, jt, (321, 4535))


def test_ch04_0_typeinfer():
    """
    Test type-inference rules
    """
    extracted = check(add_x_y, ruleset=typeinfer_ruleset)
    inferred_ops = [
        cur
        for ps, cur in ase.walk_descendants_depth_first_no_repeat(extracted)
        if isinstance(cur, NbOp_Unboxed_Add_Int64)
    ]
    assert len(inferred_ops) == 1


def test_ch04_0_boxing_optimization():
    """
    Count the number of boxing and unboxing operations before and after
    apply the optimized-ruleset.
    """
    walkfn = ase.walk_descendants_depth_first_no_repeat

    def get_boxing_unboxing_ops(ruleset):
        extracted = check(chained_additions, ruleset=ruleset)
        boxing_ops = [
            cur
            for ps, cur in walkfn(extracted)
            if isinstance(cur, NbOp_Box_Int64)
        ]
        unboxing_ops = [
            cur
            for ps, cur in walkfn(extracted)
            if isinstance(cur, NbOp_Unbox_Int64)
        ]
        return boxing_ops, unboxing_ops

    before_boxing_ops, before_unboxing_ops = get_boxing_unboxing_ops(
        typeinfer_ruleset
    )
    after_boxing_ops, after_unboxing_ops = get_boxing_unboxing_ops(
        optimized_ruleset
    )

    assert len(before_boxing_ops) > len(after_boxing_ops)
    assert len(before_unboxing_ops) > len(after_unboxing_ops)
