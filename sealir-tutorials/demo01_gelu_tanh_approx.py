# # Demo 1: Tanh Approximation for GELU activation layer
#
# (Depends on Ch.06)
#
# The deep learning community has been building domain specific compilers
# because traditional compilers do not know the domain specific rewrites,
# and extending these compilers is very time consuming and challenging.
#
# In this demo, we show how easy it is to encode pade44 approximation for
# `tanh()` that is used in GELU activation function:
#
# $$ \text{GELU}(x) \approx 0.5x \left(1 + \tanh\left(\sqrt{\frac{2}{\pi}}
# \left(x + 0.044715x^3\right)\right)\right) $$

# import needed modules:

import mlir.ir as ir
import numpy as np
from egglog import (
    Expr,
    StringLike,
    Vec,
    function,
    i64,
    i64Like,
    rewrite,
    rule,
    ruleset,
    set_,
    subsume,
    union,
)
from sealir import rvsdg
from sealir.eqsat.py_eqsat import (
    Py_AddIO,
    Py_AttrIO,
    Py_Call,
    Py_DivIO,
    Py_LoadGlobal,
    Py_MulIO,
    Py_PowIO,
)
from sealir.eqsat.rvsdg_eqsat import (
    Term,
    TermList,
)
from sealir.rvsdg import grammar as rg

from ch04_1_typeinfer_ifelse import (
    ExtendEGraphToRVSDG as ch04_1_ExtendEGraphToRVSDG,
)
from ch04_1_typeinfer_ifelse import (
    Grammar,
    NbOp_Base,
    String,
    Type,
    TypeFloat64,
    TypeInt64,
    TypeVar,
    make_rules_for_binop,
    setup_argtypes,
)
from ch05_typeinfer_array import (
    ExtendEGraphToRVSDG,
)
from ch05_typeinfer_array import MyCostModel as ch06_CostModel
from ch05_typeinfer_array import (
    base_ruleset,
    Compiler,
)
from ch06_mlir_backend import LowerStates, run_test
from ch07_mlir_ufunc import ufunc_vectorize, Float32, Backend as UfuncBackend, TypeFloat32

# ## The GELU function


def gelu_tanh_forward(a):
    dt = np.float32
    result = (
        dt(0.5)
        * a
        * (
            dt(1)
            + np.tanh(np.sqrt(dt(2) / dt(np.pi)) * (a + dt(0.044715) * a**3))
        )
    )
    return result


# ## First, we define new features needed for the above function

# ### Type inference for NumPy module


class Module(Expr):
    def __init__(self, name: StringLike): ...

    def toType(self) -> Type: ...


@function
def ModuleGetAttr(mod: Module, attrname: StringLike) -> Term: ...


@ruleset
def facts_numpy_module(io: Term, name: String, op: Term, args: Vec[Term]):

    yield rule(
        op == Py_LoadGlobal(io, name),
        name == String("np"),
    ).then(set_(TypeVar(op).getType()).to(Module("numpy").toType()))

    # ------ attributes ------
    numpy_mod = Module("numpy")

    def unary_func(fname, target_func):
        return rule(
            op
            == (
                stmt := Py_Call(
                    func=ModuleGetAttr(numpy_mod, fname),
                    io=io,
                    args=TermList(args),
                )
            ),
            args.length() == i64(1),
        ).then(
            subsume(stmt),
            union(op.getPort(0)).with_(io),
            union(op.getPort(1)).with_(target_func(args[0])),
        )

    # np.pi
    const_pi = ModuleGetAttr(numpy_mod, "pi")
    yield rewrite(
        const_pi,
        subsume=True,
    ).to(Term.LiteralF64(np.pi))
    # np.float32
    yield unary_func("float32", Npy_float32)
    # np.sqrt
    yield unary_func("sqrt", Npy_sqrt)
    # np.tanh
    yield unary_func("tanh", Npy_tanh)


# ### Type inference for NumPy operations


@function
def Npy_float32(val: Term) -> Term: ...
@function
def Npy_sqrt(val: Term) -> Term: ...
@function
def Npy_tanh(val: Term) -> Term: ...


@function
def Npy_cast_f64_to_f32(val: Term) -> Term: ...
@function
def Npy_cast_i64_to_f32(val: Term) -> Term: ...
@function
def Npy_sqrt_float32(val: Term) -> Term: ...
@function
def Npy_tanh_float32(val: Term) -> Term: ...


@ruleset
def ruleset_typeinfer_numpy_functions(res: Term, arg: Term):
    # float32()
    yield rewrite(Npy_float32(arg), subsume=True).to(
        Npy_cast_f64_to_f32(arg),
        TypeVar(arg).getType() == TypeFloat64,
    )
    yield rewrite(Npy_float32(arg), subsume=True).to(
        Npy_cast_i64_to_f32(arg),
        TypeVar(arg).getType() == TypeInt64,
    )

    for fn in [Npy_cast_f64_to_f32, Npy_cast_i64_to_f32]:
        yield rule(
            res == fn(arg),
        ).then(set_(TypeVar(res).getType()).to(TypeFloat32))
    # others

    for func, typed_func in [
        (Npy_sqrt, Npy_sqrt_float32),
        (Npy_tanh, Npy_tanh_float32),
    ]:
        yield rewrite(func(arg), subsume=True).to(
            typed_func(arg),
            TypeVar(arg).getType() == TypeFloat32,
        )
        yield rule(
            res == typed_func(arg),
        ).then(set_(TypeVar(res).getType()).to(TypeFloat32))


# ### Handle `module.attr`


@ruleset
def ruleset_module(
    io: Term, name: String, modname: String, op: Term, obj: Term
):
    # Getattribute
    yield rule(
        op == Py_AttrIO(io, obj, name),
        TypeVar(obj).getType() == Module(modname).toType(),
    ).then(
        # Shortcut io
        union(op.getPort(0)).with_(io),
        # Setup getattr
        union(op.getPort(1)).with_(ModuleGetAttr(Module(modname), name)),
    )


# # Type inference for `float32` operations


# +
@function
def Nb_Add_Float32(lhs: Term, rhs: Term) -> Term: ...


@function
def Nb_Mul_Float32(lhs: Term, rhs: Term) -> Term: ...


@function
def Nb_Div_Float32(lhs: Term, rhs: Term) -> Term: ...


@function
def Nb_Pow_Float32_Int64(lhs: Term, rhs: Term) -> Term: ...


# -


@ruleset
def ruleset_typeinfer_f32_ops(res: Term, x: Term, y: Term):
    yield from make_rules_for_binop(
        Py_AddIO, TypeFloat32, TypeFloat32, Nb_Add_Float32, TypeFloat32
    )
    yield from make_rules_for_binop(
        Py_MulIO, TypeFloat32, TypeFloat32, Nb_Mul_Float32, TypeFloat32
    )
    yield from make_rules_for_binop(
        Py_DivIO, TypeFloat32, TypeFloat32, Nb_Div_Float32, TypeFloat32
    )
    yield from make_rules_for_binop(
        Py_PowIO, TypeFloat32, TypeInt64, Nb_Pow_Float32_Int64, TypeFloat32
    )


additional_rules = (
    facts_numpy_module
    | ruleset_module
    | ruleset_typeinfer_numpy_functions
    | ruleset_typeinfer_f32_ops
)

# ### Extend the RVSDG Grammar

SExpr = rvsdg.grammar.SExpr


class NbOp_F64_to_F32(NbOp_Base):
    operand: SExpr


class NbOp_I64_to_F32(NbOp_Base):
    operand: SExpr


class NpyOp_Sqrt_Float32(NbOp_Base):
    operand: SExpr


class NpyOp_Tanh_Float32(NbOp_Base):
    operand: SExpr


class NbOp_Mul_Float32(NbOp_Base):
    lhs: SExpr
    rhs: SExpr

class NbOp_Div_Float32(NbOp_Base):
    lhs: SExpr
    rhs: SExpr


class NbOp_Add_Float32(NbOp_Base):
    lhs: SExpr
    rhs: SExpr


class NbOp_Pow_Float32_Int64(NbOp_Base):
    lhs: SExpr
    rhs: SExpr


class NbOp_module(NbOp_Base):
    name: str


# -


class ExtendEGraphToRVSDG(ch04_1_ExtendEGraphToRVSDG):

    def handle_Term(self, op: str, children: dict | list, grm: Grammar):

        match op, children:
            case "Py_Float", {"val": float(arg)}:
                return grm.write(rg.PyFloat(arg))

            case "Npy_cast_f64_to_f32", {"val": expr}:
                return grm.write(NbOp_F64_to_F32(expr))

            case "Npy_cast_i64_to_f32", {"val": expr}:
                return grm.write(NbOp_I64_to_F32(expr))

            case "Nb_Mul_Float32", {"lhs": lhs, "rhs": rhs}:
                return grm.write(NbOp_Mul_Float32(lhs=lhs, rhs=rhs))
            case "Nb_Add_Float32", {"lhs": lhs, "rhs": rhs}:
                return grm.write(NbOp_Add_Float32(lhs=lhs, rhs=rhs))
            case "Nb_Div_Float32", {"lhs": lhs, "rhs": rhs}:
                return grm.write(NbOp_Div_Float32(lhs=lhs, rhs=rhs))
            case "Nb_Pow_Float32_Int64", {"lhs": lhs, "rhs": rhs}:
                return grm.write(NbOp_Pow_Float32_Int64(lhs=lhs, rhs=rhs))
            case "Npy_sqrt_float32", {"val": val}:
                return grm.write(NpyOp_Sqrt_Float32(val))
            case "Npy_tanh_float32", {"val": val}:
                return grm.write(NpyOp_Tanh_Float32(val))
            # ---
            case "ModuleGetAttr", {"mod": mod, "attrname": str(attrname)}:
                return grm.write(rg.Undef(str(op)))
            case _:
                # Use parent's implementation for other terms.
                return super().handle_Term(op, children, grm)

    def handle_Module(
        self, key: str, op: str, children: dict | list, grm: Grammar
    ):
        return grm.write(rg.Undef(str(key)))


# ### Extend the backend


class Backend(UfuncBackend):
    def __init__(self):
        super().__init__()
        self.f32 = ir.F32Type.get(context=self.context)

    def get_mlir_type(self, seal_ty):
        match seal_ty.name:
            case "Float32":
                return self.f32
        return super().get_mlir_type(seal_ty)

    def lower_expr(self, expr: SExpr, state: LowerStates):
        import mlir.dialects.arith as arith
        import mlir.dialects.math as math

        match expr:
            case NbOp_Add_Float32(lhs, rhs):
                lhs = yield lhs
                rhs = yield rhs
                return arith.addf(lhs, rhs)
            case NbOp_Mul_Float32(lhs, rhs):
                lhs = yield lhs
                rhs = yield rhs
                return arith.mulf(lhs, rhs)
            case NbOp_Div_Float32(lhs, rhs):
                lhs = yield lhs
                rhs = yield rhs
                return arith.divf(lhs, rhs)
            case NbOp_F64_to_F32(val):
                val = yield val
                return arith.truncf(self.f32, val)
            case NbOp_I64_to_F32(val):
                val = yield val
                return arith.sitofp(self.f32, val)
            case NpyOp_Tanh_Float32(val):
                val = yield val
                return math.tanh(val)
            case NpyOp_Sqrt_Float32(val):
                val = yield val
                return math.sqrt(val)
            case NbOp_Pow_Float32_Int64(val, p):
                val = yield val
                p = yield p
                return math.powf(val, arith.sitofp(val.type, p))
            case rg.Undef(str(name)):
                return arith.constant(self.i32, 0)
        return (yield from super().lower_expr(expr, state))


# ## Cost Model
#
# Assign higher cost for the Transcendental functions:


class MyCostModel(ch06_CostModel):
    def get_cost_function(self, nodename, op, ty, cost, children):
        match op:
            case "Npy_tanh" | "Npy_sqrt" | "Npy_float32":
                cost = float("inf")  # suppress untyped op
            case "Npy_tanh_float32":
                cost = 100
            case "Npy_sqrt_float32":
                cost = 50
            case "Nb_Pow_Float32_Int64":
                cost = 50

        # Fallthrough to parent's cost function
        return super().get_cost_function(nodename, op, ty, cost, children)


# ### Run the baseline function
compiler = Compiler(ExtendEGraphToRVSDG, Backend(), MyCostModel(), True)

if __name__ == "__main__":
    llvm_module, func_egraph = compiler.lower_py_fn(
        gelu_tanh_forward,
        argtypes=(Float32,),
        ruleset=(
            base_ruleset | setup_argtypes(TypeFloat32) | additional_rules
        ),
    )
    compiler.run_backend_passes(llvm_module)
    jit_func = compiler.compile_module(llvm_module, func_egraph)
    run_test(gelu_tanh_forward, jit_func, (0.234,), verbose=True)


# ## Add rules to optimize


@ruleset
def pade44_tanh_expansion(x: Term):
    flt = lambda f: Npy_float32(Term.LiteralF64(float(f)))
    liti64 = Term.LiteralI64
    pow = Nb_Pow_Float32_Int64
    mul = Nb_Mul_Float32
    add = Nb_Add_Float32
    div = Nb_Div_Float32
    # Rewrite tanh(x) to the pade44 approximation
    # Note the use of pow(), they will be optimized
    # by the `pow_expansion` ruleset.
    yield rewrite(Npy_tanh_float32(x)).to(
        div(
            add(mul(flt(10), pow(x, liti64(3))), mul(flt(105), x)),
            add(
                add(pow(x, liti64(4)), mul(flt(45), pow(x, liti64(2)))),
                flt(105),
            ),
        )
    )


@ruleset
def pow_expansion(x: Term, ival: i64):
    # Rules to expand pow(x, i) to multiplcations
    powf = Nb_Pow_Float32_Int64
    lit64 = Term.LiteralI64
    mulf = Nb_Mul_Float32
    yield rewrite(powf(x, lit64(ival))).to(
        mulf(x, powf(x, lit64(ival - 1))),
        ival >= 1,
    )

    yield rewrite(powf(x, lit64(i64(0))), subsume=True).to(
        Npy_float32(Term.LiteralF64(float(1))),
    )


optimize_rules = pade44_tanh_expansion | pow_expansion

# ### Run the optimized function

if __name__ == "__main__":
    llvm_module, func_egraph = compiler.lower_py_fn(
        gelu_tanh_forward,
        argtypes=(Float32,),
        ruleset=(
            base_ruleset
            | setup_argtypes(TypeFloat32)
            | additional_rules
            | optimize_rules
        ),
    )
    compiler.run_backend_passes(llvm_module)
    jit_func = compiler.compile_module(llvm_module, func_egraph)

# ### Compare the result
#
# Since this is an approximation (and IEEE754), the results are good at
# rtol=1e-6.

if __name__ == "__main__":
    relclose = lambda x, y: np.allclose(x, y, rtol=1e-6)
    run_test(gelu_tanh_forward, jit_func, (0.234,), equal=relclose)

if __name__ == "__main__":
    vectorized_gelu = ufunc_vectorize(input_type=Float32, shape=(10,), ufunc_compiler=compiler, extra_ruleset=additional_rules)(gelu_tanh_forward)
    relclose = lambda x, y: np.allclose(x, y, rtol=1e-6)
    input_val = np.array([0.234]*10, dtype=np.float32)
    run_test(gelu_tanh_forward, vectorized_gelu, (input_val,), equal=relclose)
