# ---
# jupyter:
#   jupytext:
#     text_representation:
#       extension: .py
#       format_name: light
#       format_version: '1.5'
#       jupytext_version: 1.16.7
#   kernelspec:
#     display_name: Python 3 (ipykernel)
#     language: python
#     name: python3
# ---

# ## Ch 6.
#

from __future__ import annotations

import ctypes
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

import mlir.dialects.arith as arith
import mlir.dialects.cf as cf
import mlir.dialects.func as func
import mlir.dialects.scf as scf
import mlir.execution_engine as execution_engine
import mlir.ir as ir
import mlir.passmanager as passmanager
import mlir.runtime as runtime
import numpy as np
from sealir import ase
from sealir.rvsdg import grammar as rg
from sealir.rvsdg import internal_prefix

from ch03_egraph_program_rewrites import (
    run_test,
)
from ch04_1_typeinfer_ifelse import (
    Attributes,
    Compiler,
)
from ch04_1_typeinfer_ifelse import (
    ExtendEGraphToRVSDG as ConditionalExtendGraphtoRVSDG,
)
from ch04_1_typeinfer_ifelse import (
    Int64,
    MyCostModel,
    NbOp_Add_Float64,
    NbOp_Add_Int64,
    NbOp_CastI64ToF64,
    NbOp_Div_Int64,
    NbOp_Gt_Int64,
    NbOp_Lt_Int64,
    NbOp_Sub_Float64,
    NbOp_Sub_Int64,
    NbOp_Type,
    SExpr,
    TypeInt64,
)
from ch04_1_typeinfer_ifelse import base_ruleset as if_else_ruleset
from ch04_1_typeinfer_ifelse import (
    ruleset_type_infer_float,
    setup_argtypes,
)
from ch04_2_typeinfer_loops import (
    ExtendEGraphToRVSDG as LoopExtendEGraphToRVSDG,
)
from ch04_2_typeinfer_loops import (
    NbOp_Not_Int64,
)
from ch04_2_typeinfer_loops import base_ruleset as loop_ruleset

_DEBUG = False


@dataclass(frozen=True)
class LowerStates(ase.TraverseState):
    push: Callable
    get_region_args: Callable
    function_block: func.FuncOp
    constant_block: ir.Block


function_name = "func"


class Backend:
    def __init__(self):
        self.context = context = ir.Context()
        self.f32 = ir.F32Type.get(context=context)
        self.f64 = ir.F64Type.get(context=context)
        self.i32 = ir.IntegerType.get_signless(32, context=context)
        self.i64 = ir.IntegerType.get_signless(64, context=context)
        self.boo = ir.IntegerType.get_signless(1, context=context)

    def lower_type(self, ty: NbOp_Type):
        match ty:
            case NbOp_Type("Int64"):
                return self.i64
            case NbOp_Type("Float64"):
                return self.f64
            case NbOp_Type("Float32"):
                return self.f32
        raise NotImplementedError(f"unknown type: {ty}")

    def lower(self, root: rg.Func, argtypes):
        context = self.context
        self.loc = loc = ir.Location.unknown(context=context)
        self.module = module = ir.Module.create(loc=loc)

        # Get the module body pointer so we can insert content into the
        # module.
        self.module_body = module_body = ir.InsertionPoint(module.body)
        # Convert SealIR types to MLIR types.
        input_types = tuple([self.lower_type(x) for x in argtypes])
        output_types = (
            self.lower_type(
                Attributes(root.body.begin.attrs).get_return_type(root.body)
            ),
        )

        with context, loc, module_body:
            # Constuct a function that emits a callable C-interface.
            fun = func.FuncOp(function_name, (input_types, output_types))
            fun.attributes["llvm.emit_c_interface"] = ir.UnitAttr.get()

            # Define two blocks within the function, a constant block to
            # define all the constants and a function block for the
            # actual content. This is done to prevent non-dominant use
            # of constants. (Use of a constant when declaration is done in
            # a region that isn't initialized.)
            const_block = fun.add_entry_block()
            fun.body.blocks.append(*[], arg_locs=None)
            func_block = fun.body.blocks[1]

        # Define entry points of both the blocks.
        constant_entry = ir.InsertionPoint(const_block)
        function_entry = ir.InsertionPoint(func_block)

        region_args = []

        @contextmanager
        def push(arg_values):
            region_args.append(tuple(arg_values))
            try:
                yield
            finally:
                region_args.pop()

        def get_region_args():
            return region_args[-1]

        with context, loc, function_entry:
            memo = ase.traverse(
                root,
                self.lower_expr,
                LowerStates(
                    push=push,
                    get_region_args=get_region_args,
                    function_block=fun,
                    constant_block=constant_entry,
                ),
            )

        # Use a break to jump from the constant block to the function block.
        # note that this is being inserted at end of constant block after the
        # Function construction when all the constants have been initialized.
        with context, loc, constant_entry:
            cf.br([], fun.body.blocks[1])

        return module

    def run_passes(self, module):
        module.dump()

        if _DEBUG:
            module.context.enable_multithreading(False)
        if _DEBUG:
            pass_man.enable_ir_printing()

        pass_man = passmanager.PassManager(context=module.context)
        pass_man.add("convert-linalg-to-loops")
        pass_man.add("convert-scf-to-cf")
        pass_man.add("finalize-memref-to-llvm")
        pass_man.add("convert-math-to-libm")
        pass_man.add("convert-func-to-llvm")
        pass_man.add("convert-index-to-llvm")
        pass_man.add("reconcile-unrealized-casts")
        pass_man.enable_verifier(True)
        pass_man.run(module.operation)
        # Output LLVM-dialect MLIR
        module.dump()
        return module

    def lower_expr(self, expr: SExpr, state: LowerStates):
        match expr:
            case rg.Func(args=args, body=body):
                names = {
                    argspec.name: state.function_block.arguments[i]
                    for i, argspec in enumerate(args.arguments)
                }
                argvalues = []
                for k in body.begin.inports:
                    if k == internal_prefix("io"):
                        v = arith.constant(self.i32, 0)
                    else:
                        v = names[k]
                    argvalues.append(v)

                with state.push(argvalues):
                    outs = yield body

                portnames = [p.name for p in body.ports]
                retval = outs[portnames.index(internal_prefix("ret"))]
                func.ReturnOp([retval])
            case rg.RegionBegin(inports=ins):
                portvalues = []
                for i, k in enumerate(ins):
                    pv = state.get_region_args()[i]
                    portvalues.append(pv)
                return tuple(portvalues)

            case rg.RegionEnd(
                begin=rg.RegionBegin() as begin,
                ports=ports,
            ):
                yield begin
                portvalues = []
                for p in ports:
                    pv = yield p.value
                    portvalues.append(pv)
                return tuple(portvalues)

            case rg.ArgRef(idx=int(idx), name=str(name)):
                return state.function_block.arguments[idx]

            case rg.Unpack(val=source, idx=int(idx)):
                ports = yield source
                return ports[idx]

            case rg.DbgValue(value=value):
                val = yield value
                return val

            case rg.PyInt(int(ival)):
                with state.constant_block:
                    const = arith.constant(self.i64, ival)
                return const

            case rg.PyBool(int(ival)):
                with state.constant_block:
                    const = arith.constant(self.boo, ival)
                return const

            case rg.PyFloat(float(fval)):
                with state.constant_block:
                    const = arith.constant(self.f64, fval)
                return const

            case NbOp_Gt_Int64(lhs, rhs):
                lhs = yield lhs
                rhs = yield rhs
                return arith.cmpi(4, lhs, rhs)

            case NbOp_Add_Int64(lhs, rhs):
                lhs = yield lhs
                rhs = yield rhs
                return arith.addi(lhs, rhs)

            case NbOp_Sub_Int64(lhs, rhs):
                lhs = yield lhs
                rhs = yield rhs
                return arith.subi(lhs, rhs)

            case NbOp_Add_Float64(lhs, rhs):
                lhs = yield lhs
                rhs = yield rhs
                return arith.addf(lhs, rhs)
            case NbOp_Sub_Float64(lhs, rhs):
                lhs = yield lhs
                rhs = yield rhs
                return arith.subf(lhs, rhs)
            case NbOp_Lt_Int64(lhs, rhs):
                lhs = yield lhs
                rhs = yield rhs
                return arith.cmpi(2, lhs, rhs)
            case NbOp_Sub_Int64(lhs, rhs):
                lhs = yield lhs
                rhs = yield rhs
                return arith.subi(lhs, rhs)

            case NbOp_CastI64ToF64(operand):
                val = yield operand
                return arith.sitofp(self.f64, val)
            case NbOp_Div_Int64(lhs, rhs):
                lhs = yield lhs
                rhs = yield rhs

                return arith.divf(
                    arith.sitofp(self.f64, lhs), arith.sitofp(self.f64, rhs)
                )
            ##### more
            case NbOp_Not_Int64(operand):
                # Implement unary not
                opval = yield operand
                return arith.cmpi(0, opval, arith.constant(self.i64, 0))
            case rg.PyBool(val):
                return arith.constant(self.boo, val)

            case rg.PyInt(val):
                return arith.constant(self.i64, val)

            case rg.IfElse(
                cond=cond, body=body, orelse=orelse, operands=operands
            ):
                condval = yield cond

                # process operands
                rettys = Attributes(body.begin.attrs)
                result_tys = []
                for i in range(0, rettys.num_output_types() + 1):
                    out_ty = rettys.get_output_type(i)
                    if out_ty is not None:
                        match out_ty.name:
                            case "Int64":
                                result_tys.append(self.i64)
                            case "Float64":
                                result_tys.append(self.f64)
                            case "Bool":
                                result_tys.append(self.boo)
                    else:
                        result_tys.append(self.i32)

                if_op = scf.IfOp(
                    cond=condval, results_=result_tys, hasElse=bool(orelse)
                )

                with ir.InsertionPoint(if_op.then_block):
                    value_else = yield body
                    scf.YieldOp([x for x in value_else])

                with ir.InsertionPoint(if_op.else_block):
                    value_else = yield orelse
                    scf.YieldOp([x for x in value_else])

                return if_op.results
            case rg.Loop(body=rg.RegionEnd() as body, operands=operands):
                rettys = Attributes(body.begin.attrs)
                # process operands
                ops = []
                for op in operands:
                    ops.append((yield op))

                result_tys = []
                for i in range(1, rettys.num_output_types() + 1):
                    out_ty = rettys.get_output_type(i)
                    if out_ty is not None:
                        match out_ty.name:
                            case "Int64":
                                result_tys.append(self.i64)
                            case "Float64":
                                result_tys.append(self.f64)
                            case "Bool":
                                result_tys.append(self.boo)
                    else:
                        result_tys.append(self.i32)

                while_op = scf.WhileOp(
                    results_=result_tys, inits=[op for op in ops]
                )
                before_block = while_op.before.blocks.append(*result_tys)
                after_block = while_op.after.blocks.append(*result_tys)
                new_ops = before_block.arguments

                # Before Region
                with ir.InsertionPoint(before_block), state.push(new_ops):
                    values = yield body
                    scf.ConditionOp(
                        args=[val for val in values[1:]], condition=values[0]
                    )

                # After Region
                with ir.InsertionPoint(after_block):
                    scf.YieldOp(after_block.arguments)

                while_op_res = scf._get_op_results_or_values(while_op)
                return while_op_res

            case _:
                raise NotImplementedError(expr, type(expr))

    def jit_compile(self, llmod, func_node: rg.Func, func_name="func"):
        attributes = Attributes(func_node.body.begin.attrs)
        # Convert SealIR types into MLIR types
        with self.loc:
            input_types = tuple(
                [self.lower_type(x) for x in attributes.input_types()]
            )

        output_types = (
            self.lower_type(
                Attributes(func_node.body.begin.attrs).get_return_type(
                    func_node.body
                )
            ),
        )
        return self.jit_compile_(llmod, input_types, output_types)

    @classmethod
    def jit_compile_(
        cls,
        llmod,
        input_types,
        output_types,
        function_name="func",
        exec_engine=None,
        is_ufunc=False,
        **execution_engine_params,
    ):
        # Converts the MLIR module into a JIT-callable function.
        # Use MLIR's own internal execution engine
        if exec_engine is None:
            engine = execution_engine.ExecutionEngine(
                llmod, **execution_engine_params
            )
        else:
            engine = exec_engine

        assert (
            len(output_types) == 1
        ), "Execution of functions with output arguments > 1 not supported"
        nout = len(output_types)

        # Build a wrapper function
        def jit_func(*args):
            if is_ufunc:
                input_args = args[:-nout]
                output_args = args[-nout:]
            else:
                input_args = args
                output_args = [None]
            assert len(input_args) == len(input_types)
            for arg, arg_ty in zip(input_args, input_types):
                # assert isinstance(arg, arg_ty)
                # TODO: Assert types here
                pass
            # Transform the input arguments into C-types
            # with their respective values. All inputs to
            # the internal execution engine should
            # be C-Type pointers.
            input_exec_ptrs = [
                cls.get_exec_ptr(ty, val)[0]
                for ty, val in zip(input_types, input_args)
            ]
            # Invokes the function that we built, internally calls
            # _mlir_ciface_function_name as a void pointer with the given
            # input pointers, there can only be one resulting pointer
            # appended to the end of all input pointers in the invoke call.
            res_ptr, res_val = cls.get_exec_ptr(
                output_types[0], output_args[0]
            )
            engine.invoke(function_name, *input_exec_ptrs, res_ptr)

            return cls.get_out_val(res_ptr, res_val)

        return jit_func

    @classmethod
    def get_exec_ptr(cls, mlir_ty, val):
        if isinstance(mlir_ty, ir.IntegerType):
            val = 0 if val is None else val
            ptr = ctypes.pointer(ctypes.c_int64(val))
        elif isinstance(mlir_ty, ir.F32Type):
            val = 0.0 if val is None else val
            ptr = ctypes.pointer(ctypes.c_float(val))
        elif isinstance(mlir_ty, ir.F64Type):
            val = 0.0 if val is None else val
            ptr = ctypes.pointer(ctypes.c_double(val))
        elif isinstance(mlir_ty, ir.MemRefType):
            if isinstance(mlir_ty.element_type, ir.F64Type):
                np_dtype = np.float64
            elif isinstance(mlir_ty.element_type, ir.F32Type):
                np_dtype = np.float32
            else:
                raise TypeError(
                    "The current array element type is not supported"
                )

            if val is None:
                if not mlir_ty.has_static_shape:
                    raise ValueError(f"{mlir_ty} does not have static shape")
                val = np.zeros(mlir_ty.shape, dtype=np_dtype)

            ptr = ctypes.pointer(
                ctypes.pointer(runtime.get_ranked_memref_descriptor(val))
            )

        return ptr, val

    @classmethod
    def get_out_val(cls, res_ptr, res_val):
        if isinstance(res_val, np.ndarray):
            return res_val
        else:
            return res_ptr.contents.value


# + [markdown] jp-MarkdownHeadingCollapsed=true
# Example 1: simple if-else
# -


def example_1(a, b):
    if a > b:
        z = a - b
    else:
        z = b - a
    return z + a


compiler = Compiler(
    ConditionalExtendGraphtoRVSDG, Backend(), MyCostModel(), True
)

if __name__ == "__main__":
    llvm_module, func_egraph = compiler.lower_py_fn(
        example_1,
        argtypes=(Int64, Int64),
        ruleset=(if_else_ruleset | setup_argtypes(TypeInt64, TypeInt64)),
    )
    compiler.run_backend_passes(llvm_module)
    jit_func = compiler.compile_module(llvm_module, func_egraph)
    args = (10, 33)
    run_test(example_1, jit_func, args, verbose=True)
    args = (7, 3)
    run_test(example_1, jit_func, args, verbose=True)


# ## Example 2: add `float()`


def example_2(a, b):
    if a > b:
        z = float(a - b)
    else:
        z = float(b) - 1 / a
    return z - float(a)


# Add rules for `float()`


if __name__ == "__main__":
    llvm_module, func_egraph = compiler.lower_py_fn(
        example_2,
        argtypes=(Int64, Int64),
        ruleset=(
            if_else_ruleset
            | setup_argtypes(TypeInt64, TypeInt64)
            | ruleset_type_infer_float  # < --- added for float()
        ),
    )
    compiler.run_backend_passes(llvm_module)
    jit_func = compiler.compile_module(llvm_module, func_egraph)
    args = (10, 33)
    run_test(example_2, jit_func, args, verbose=True)
    args = (7, 3)
    run_test(example_2, jit_func, args, verbose=True)

# ## Example 3: Simple while loop example


def example_3(init, n):
    c = float(init)
    i = 0
    while i < n:
        c = c + float(i)
        i = i + 1
    return c


compiler = Compiler(LoopExtendEGraphToRVSDG, Backend(), MyCostModel(), True)

if __name__ == "__main__":
    llvm_module, func_egraph = compiler.lower_py_fn(
        example_3,
        argtypes=(Int64, Int64),
        ruleset=loop_ruleset | setup_argtypes(TypeInt64, TypeInt64),
    )
    compiler.run_backend_passes(llvm_module)
    jit_func = compiler.compile_module(llvm_module, func_egraph)

    run_test(example_3, jit_func, (10, 7), verbose=True)


# ## Example 4: Nested Loop example


def example_4(init, n):
    c = float(init)
    i = 0
    while i < n:
        j = 0
        while j < i:
            c = c + float(j)
            j = j + 1
        i = i + 1
    return c


if __name__ == "__main__":
    llvm_module, func_egraph = compiler.lower_py_fn(
        example_4,
        argtypes=(Int64, Int64),
        ruleset=loop_ruleset | setup_argtypes(TypeInt64, TypeInt64),
    )
    compiler.run_backend_passes(llvm_module)
    jit_func = compiler.compile_module(llvm_module, func_egraph)

    run_test(example_4, jit_func, (10, 7), verbose=True)
