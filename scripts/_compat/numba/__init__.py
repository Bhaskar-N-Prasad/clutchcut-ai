"""Plain-Python stand-in for numba, used only when Windows blocks numba's compiled files.

Smart App Control can refuse numba's DLL. librosa only needs numba to speed things up, so these decorators
just run the Python code as written. Slower, but the results are the same.
"""
import re

import numpy as np

__version__ = "0.0-compat"
config = type("config", (), {"DISABLE_JIT": True})()


def _decorator(*args, **kwargs):
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]
    return lambda f: f


jit = njit = generated_jit = cfunc = stencil = _decorator
prange = range


def vectorize(*args, **kwargs):
    def wrap(f):
        nin = f.__code__.co_argcount
        loop = np.vectorize(f)

        def call(*arrays, out=None):
            res = loop(*arrays[:nin])
            target = arrays[nin] if len(arrays) > nin else out  # ufuncs may take the output array positionally
            if target is None:
                return res
            target[...] = res
            return target
        return call
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return wrap(args[0])
    return wrap


def guvectorize(signatures, layout=None, **kwargs):
    """Numba gufuncs write into their last argument(s); emulate that with a per-element loop."""
    layout = layout or signatures  # newer numba also accepts the layout on its own
    ins, outs = layout.replace(" ", "").split("->")
    in_dims = re.findall(r"\(([^)]*)\)", ins)
    out_dims = re.findall(r"\(([^)]*)\)", outs)
    out_types = [None] * len(out_dims)
    if isinstance(signatures, (list, tuple)) and signatures:
        # e.g. "void(float32[:], bool_[:])": the last arguments are the outputs, and their dtypes matter
        names = [a.strip().split("[")[0] for a in re.search(r"\((.*)\)", str(signatures[0])).group(1).split(",")]
        for k, name in enumerate(names[-len(out_dims):]):
            out_types[k] = (bool if name.startswith("bool") else np.int64 if name.startswith("int")
                            else np.complex128 if name.startswith("complex") else np.float64)

    def wrap(f):
        def call(*arrays):
            given = list(arrays[len(in_dims):])  # callers may pass the output arrays to fill
            arrays = [np.asarray(a) for a in arrays[:len(in_dims)]]
            core = [len([d for d in dims.split(",") if d]) for dims in in_dims]
            batch = np.broadcast_shapes(*[a.shape[:a.ndim - c] for a, c in zip(arrays, core)])
            sizes = {}
            for a, dims, c in zip(arrays, in_dims, core):
                for name, size in zip([d for d in dims.split(",") if d], a.shape[a.ndim - c:]):
                    sizes[name] = size
            results = [given[k] if k < len(given) else
                       np.zeros(batch + tuple(sizes[d] for d in dims.split(",") if d), dtype=t or np.result_type(*arrays))
                       for k, (dims, t) in enumerate(zip(out_dims, out_types))]
            for idx in np.ndindex(batch):
                views = [np.broadcast_to(a, batch + a.shape[a.ndim - c:])[idx] for a, c in zip(arrays, core)]
                outs_here = [r[idx] for r in results]
                outs_here = [o if o.ndim else o.reshape(1) for o in outs_here]
                f(*views, *outs_here)
                for r, o in zip(results, outs_here):
                    if r[idx].ndim == 0:
                        r[idx] = o[0]
            return results[0] if len(results) == 1 else tuple(results)
        return call
    return wrap


class _Types:
    def __getattr__(self, name):
        return object()


types = _Types()
float32 = float64 = int32 = int64 = boolean = complex64 = complex128 = uint8 = object()
