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

# # Ch 8. GPU offloading for MLIR ufunc operations
#

# In this chapter, we'll look at type inference for array operations.

from __future__ import annotations

import ctypes
from collections import namedtuple
from ctypes.util import find_library

import mlir.execution_engine as execution_engine
import mlir.ir as ir
import mlir.passmanager as passmanager
import mlir.runtime as runtime
import numpy as np
from numba import cuda

from ch04_2_typeinfer_loops import (
    Compiler,
    MyCostModel,
)
from ch05_typeinfer_array import NbOp_ArrayType
from ch06_mlir_backend import Backend as _Backend
from ch06_mlir_backend import ConditionalExtendGraphtoRVSDG, NbOp_Type
from ch07_mlir_ufunc import Float64, ufunc_vectorize

_DEBUG = True


class GPUBackend(_Backend):
    # Lower symbolic array to respective memref.
    # Note: This is not used within ufunc builder,
    # since it has explicit declaration of the respective
    # MLIR memrefs.
    def lower_type(self, ty: NbOp_Type):
        match ty:
            case NbOp_ArrayType(
                dtype=dtype,
                ndim=int(ndim),
                datalayout=str(datalayout),
                shape=shape,
            ):
                mlir_dtype = self.lower_type(dtype)
                with self.loc:
                    memref_ty = ir.MemRefType.get(shape, mlir_dtype)
                return memref_ty
        return super().lower_type(ty)

    def run_passes(self, module):
        module.dump()
        pass_man = passmanager.PassManager(context=module.context)

        if _DEBUG:
            module.context.enable_multithreading(False)
        if _DEBUG:
            pass_man.enable_ir_printing()

        pass_man.add("convert-linalg-to-affine-loops")
        pass_man.add("affine-loop-fusion")
        pass_man.add("inline")
        pass_man.add("func.func(affine-parallelize)")
        pass_man.add(
            "builtin.module(func.func(gpu-map-parallel-loops,convert-parallel-loops-to-gpu))"
        )
        pass_man.add("lower-affine")
        pass_man.add("scf-parallel-loop-fusion")
        pass_man.add(
            "func.func(gpu-map-parallel-loops,convert-parallel-loops-to-gpu)"
        )
        pass_man.add("gpu-kernel-outlining")
        pass_man.add('gpu-lower-to-nvvm-pipeline{cubin-format="fatbin"}')
        pass_man.add("convert-scf-to-cf")
        pass_man.add("finalize-memref-to-llvm")
        pass_man.add("convert-math-to-libm")
        pass_man.add("convert-func-to-llvm")
        pass_man.add("convert-index-to-llvm")
        pass_man.add("convert-bufferization-to-memref")
        pass_man.add("reconcile-unrealized-casts")
        pass_man.add("func.func(llvm-request-c-wrappers)")
        pass_man.enable_verifier(True)
        pass_man.run(module.operation)
        # Output LLVM-dialect MLIR
        module.dump()
        return module

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
            val = (
                np.zeros(mlir_ty.shape, dtype=np_dtype) if val is None else val
            )
            val = cls.np_arr_to_np_duck_device_arr(val)
            ptr = ctypes.pointer(
                ctypes.pointer(runtime.get_ranked_memref_descriptor(val))
            )

        return ptr, val

    @classmethod
    def get_out_val(cls, res_ptr, res_val):
        if isinstance(res_val, cuda.cudadrv.devicearray.DeviceNDArray):
            return res_val.copy_to_host()
        else:
            return super().get_out_val(res_ptr, res_val)

    @classmethod
    def np_arr_to_np_duck_device_arr(cls, arr):
        da = cuda.to_device(arr)
        ctlie = namedtuple("ctypes_lie", "data data_as shape")
        da.ctypes = ctlie(
            da.__cuda_array_interface__["data"][0],
            lambda x: ctypes.cast(da.ctypes.data, x),
            da.__cuda_array_interface__["shape"],
        )
        da.itemsize = arr.itemsize
        return da

    @classmethod
    def jit_compile_(
        cls,
        llmod,
        input_types,
        output_types,
        function_name="func",
        exec_engine=None,
        **execution_engine_params,
    ):
        cuda_libs = (
            "mlir_cuda_runtime",
            "mlir_c_runner_utils",
            "mlir_runner_utils",
        )
        cuda_shared_libs = [find_library(x) for x in cuda_libs]
        return super().jit_compile_(
            llmod,
            input_types,
            output_types,
            function_name,
            exec_engine=execution_engine.ExecutionEngine(
                llmod, opt_level=3, shared_libs=cuda_shared_libs
            ),
        )


if __name__ == "__main__":
    gpu_compiler = Compiler(
        ConditionalExtendGraphtoRVSDG, GPUBackend(), MyCostModel(), True
    )

    @ufunc_vectorize(
        input_type=Float64, shape=(10, 10), ufunc_compiler=gpu_compiler
    )
    def foo(a, b, c):
        x = a + 1.0
        y = b - 2.0
        z = c + 3.0
        return x + y + z

    # Create NumPy arrays
    ary = np.arange(100, dtype=np.float64).reshape(10, 10)
    ary_2 = np.arange(100, dtype=np.float64).reshape(10, 10)
    ary_3 = np.arange(100, dtype=np.float64).reshape(10, 10)

    got = foo(ary, ary_2, ary_3)
    print("Got", got)
