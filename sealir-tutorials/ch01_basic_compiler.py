# ---
# jupyter:
#   jupytext:
#     text_representation:
#       extension: .py
#       format_name: light
#       format_version: '1.5'
#       jupytext_version: 1.16.7
#   kernelspec:
#     display_name: sealir_basic_compiler
#     language: python
#     name: python3
# ---

# # Ch 1. Basic Compiler
#
# ## AST frontend and LLVM backend

from __future__ import annotations

import builtins
from collections import ChainMap

from llvmlite import binding as llvm
from llvmlite import ir
from sealir import ase, rvsdg
from sealir.llvm_pyapi_backend import (
    CodegenCtx,
    CodegenState,
    JitCallable,
    PythonAPI,
    _codegen_loop,
)
from sealir.rvsdg import grammar as rg

# ## The frontend
#
# The frontend accepts a Python function object, reads its source code, and
# parses its Abstract Syntax Tree (AST). It then transforms the AST into a
# Regionalized Value-State Dependence Graph (RVSDG), a representation that
# simplifies further intermediate representation (IR) processing. The RVSDG uses
# a data-flow centric encoding in which control-flow constructs are mapped into
# regions. These regions function much like ordinary operations, with clearly
# defined sets of input and output ports. Additionally, state is explicitly
# encoded as I/O, so that every operation maintains a pure operational
# appearance.


def frontend(fn):
    """
    Frontend code is all encapsulated in sealir.rvsdg.restructure_source
    """
    rvsdg_expr, dbginfo = rvsdg.restructure_source(fn)

    return rvsdg_expr, dbginfo


# Here's a simple function to illustrate the frontend

if __name__ == "__main__":

    def exercise_frontend_simple(x, y):
        return x + y

    rvsdg_expr, dbg = frontend(exercise_frontend_simple)
    print(rvsdg.format_rvsdg(rvsdg_expr))

# The function’s RVSDG-IR includes a region:
#
# ```
# $0 = Region[236] <- !io x y
# {
#     ...
# } [302] -> !io=$1[0] !ret=$1[1] x=$0[1] y=$0[2]
# ```
#
# The region has three input ports: `!io`, `x`, and `y`.
# Its output ports are `!io`, `!ret`, `x`, and `y`.
#
# Here, `!io` represents the state. Because Python functions are imperative,
# each function carries an implicit state that must be updated for any
# side-effect.
#
# The `!ret` port holds the function’s return value.
#
# The `x` and `y` output ports convey the region’s internal value state, but the
# function node ignores these values.
#
# The single operation in the function is:
#
# ```
#  $1 = PyBinOp + $0[0] $0[1], $0[2]
# ```
#
# This is a Python binary operation. It uses input operands from `$0`, which is
# the header of the function’s region. In this notation, `$0[n]` refers to a
# specific port: `$0[0]` corresponds to `!io`, `$0[1]` to `x`, and `$0[2]` to
# `y`.
#
#

# Below is a more intricate example that requires restructuring of the
# control flow. This is due to the use of the `break` statement to exit a
# for-loop.
#
# RVSDG enforces structured control flow. Only three types of control-flow
# regions are permitted:
#
# - Linear region, as `Region {...}`;
# - If-Else region, as `If {...} Else {...} EndIf`;
# - Loop region, as `Loop {...} EndLoop`.

if __name__ == "__main__":

    def exercise_frontend_loop_if_break(x, y):
        c = 0
        for i in range(x):
            if i > y:
                break
            c += i
        return c

    rvsdg_expr, dbg = frontend(exercise_frontend_loop_if_break)
    print(rvsdg.format_rvsdg(rvsdg_expr))


# Observations from the RVSDG-IR above:
#
# * The for-loop is restructured into a `Loop` composed of `If-Else` regions.
# * The `Loop` region is tail-controlled; its first output port acts as the loop
#   condition. See `!_loopcond_0002`.
# * An extra `If-Else` follows the loop to adjust the value of `i`.
#
# The beauty of the structured control-flow is that everything can be
# encapsulated as a plain data-flow operation, including any region.
# Everything is just an operation with some input and output ports.
# This simplifies the rest of the compiler.

# ## The backend
#
# SealIR includes a lightweight LLVM backend that emits the Python C-API, which
# executes the RVSDG-IR. The example below demonstrates how to use this backend


def _determine_arity(root: ase.SExpr) -> int:
    """Helper function to get the arity of the function node"""
    match root:
        case rg.Func(args=rg.Args() as args):
            return len(args.arguments)
        case _:
            raise TypeError(root._head)


def backend(root, ns=builtins.__dict__, codegen_extension=None):
    """
    Emit LLVM using Python C-API.

    root: the RVSDG expression for the function
    ns: is the dictionary of global names. A JIT is assumed. Object pointer for
        each key is used.
    codegen_extension: Optional. If defined, it is the function to call when an
        unknown operation is encountered by the code generator. This argument
        is used in the later chapters.

    Warning:

    - This is for testing only.
    - Does NOT do proper memory management.
    - Does NOT do proper error handling.
    """

    llvm.initialize()
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()

    mod = ir.Module()

    ll_byte = ir.IntType(8)
    ll_pyobject_ptr = ll_byte.as_pointer()
    # Make LLVM function
    arity = _determine_arity(root)

    assert arity >= 1
    actual_num_args = arity
    fnty = ir.FunctionType(
        ll_pyobject_ptr, [ll_pyobject_ptr] * actual_num_args
    )
    fn = ir.Function(mod, fnty, name="foo")

    # init entry block and builder
    builder = ir.IRBuilder(fn.append_basic_block())
    retval_slot = builder.alloca(ll_pyobject_ptr)
    builder.store(ll_pyobject_ptr(None), retval_slot)  # init retval to NULL

    bb_main = builder.append_basic_block()
    builder.branch(bb_main)
    builder.position_at_end(bb_main)

    ctx = CodegenCtx(
        llvm_module=mod,
        llvm_func=fn,
        builder=builder,
        pyapi=PythonAPI(builder),
        global_ns=ChainMap(ns, __builtins__),
        codegen_extension=codegen_extension,
    )

    # Emit the function body
    ase.traverse(root, _codegen_loop, CodegenState(context=ctx))
    return mod


# The following function takes a LLVM module and JIT compile it for execution:


def jit_compile(mod, rvsdg_expr):
    """JIT compile LLVM module into an executable function for this process."""
    llvm_ir = str(mod)

    # Create JIT
    lljit = llvm.create_lljit_compiler()
    rt = (
        llvm.JITLibraryBuilder()
        .add_ir(llvm_ir)
        .export_symbol("foo")
        .add_current_process()
        .link(lljit, "foo")
    )
    ptr = rt["foo"]
    arity = _determine_arity(rvsdg_expr)
    return JitCallable.from_pointer(rt, ptr, arity)


# The following is a simple compiler pipeline:


def compiler_pipeline(fn, *, verbose=False):
    rvsdg_expr, dbginfo = frontend(fn)

    if verbose:
        print("Frontend: Debug Info on RVSDG".center(80, "="))
        print(dbginfo.show_sources())

        print("Frontend: RVSDG".center(80, "="))
        print(rvsdg.format_rvsdg(rvsdg_expr))

    llmod = backend(rvsdg_expr)

    if verbose:
        print("Backend: LLVM".center(80, "="))
        print(llmod)

    jt = jit_compile(llmod, rvsdg_expr)
    return jt


def run_test(fn, jt, args, *, verbose=False):
    res = jt(*args)

    if verbose:
        print("JIT: output".center(80, "="))
        print(res)

    assert res == fn(*args)
    return res


# The following puts everything together. Running the frontend to generate
# RVSDG-IR. Then, emitting LLVM using the backend. Finally, JIT'ing it into
# executable code and verifying it.

if __name__ == "__main__":

    def sum_ints(n):
        c = 0
        for i in range(n):
            c += i
        return c

    jt = compiler_pipeline(sum_ints, verbose=True)
    run_test(sum_ints, jt, (12,), verbose=True)
