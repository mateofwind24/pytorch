# This module contains functions that *will be allowed* by dynamo

import functools


def is_compiling() -> bool:
    return False


def wrap_inline(fn):
    """
    Create an extra frame around fn that is not in skipfiles
    """

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        return fn(*args, **kwargs)

    return inner


def call_hook(hook, *args):
    """
    Used by compiled autograd to handle hook returning None
    """
    result = hook(*args)
    if result is None:
        return args[0]
    return result


class FakeContext:
    def __init__(self, saved_tensors):
        # this will cache the results of saved_tensors
        # and will no longer call into c++ binding
        self.saved_tensors = saved_tensors


def call_backward(backward_fn, saved_tensors, *args):
    grads = backward_fn(FakeContext(saved_tensors), *args)

    # in eager, we wrap in a tuple when there's only one grad output
    if type(grads) is not tuple:
        grads = (grads,)

    return grads
