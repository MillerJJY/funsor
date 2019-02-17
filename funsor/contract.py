from __future__ import absolute_import, division, print_function

import opt_einsum

import funsor.ops as ops
from funsor.registry import KeyedRegistry
from funsor.terms import Funsor
from funsor.torch import Tensor

contract = KeyedRegistry()


class Contract(Funsor):
    def __init__(self, sum_op, prod_op, lhs, rhs, dim):
        assert callable(sum_op)
        assert callable(prod_op)
        assert isinstance(lhs, Funsor)
        assert isinstance(rhs, Funsor)
        assert dim in lhs.dims and dim in rhs.dims
        schema = lhs.schema.copy()
        schema.update(rhs.schema)
        del schema[dim]
        dims = tuple(schema)
        shape = tuple(schema.values())
        super(Contract, self).__init__(dims, shape)
        self.sum_op = sum_op
        self.prod_op = prod_op
        self.lhs = lhs
        self.rhs = rhs
        self.dim = dim


################################################################################
# Contract Implementations
################################################################################

@contract.register((ops.add, ops.mul), Funsor, Funsor, frozenset)
def _sumproduct(lhs, rhs, reduce_dims):
    return Contract(ops.add, ops.mul, lhs, rhs, reduce_dims)


@contract.register((ops.add, ops.mul), Tensor, Tensor, frozenset)
def _sumproduct_tensor_tensor(lhs, rhs, reduce_dims):
    schema = lhs.schema.copy()
    schema.update(rhs.schema)
    for dim in reduce_dims:
        del schema[dim]
    dims = tuple(schema)
    data = opt_einsum.contract(lhs.data, lhs.dims, rhs.data, rhs.dims, dims,
                               backend='torch')
    return Tensor(dims, data)


@contract.register((ops.logaddexp, ops.add), Funsor, Funsor, frozenset)
def _logsumproductexp(lhs, rhs, reduce_dims):
    return Contract(ops.logaddexp, ops.add, lhs, rhs, reduce_dims)


@contract.register((ops.logaddexp, ops.add), Tensor, Tensor, frozenset)
def _logsumproductexp_tensor_tensor(lhs, rhs, reduce_dims):
    schema = lhs.schema.copy()
    schema.update(rhs.schema)
    for dim in reduce_dims:
        del schema[dim]
    dims = tuple(schema)
    data = opt_einsum.contract(lhs.data, lhs.dims, rhs.data, rhs.dims, dims,
                               backend='pyro.ops.einsum.torch_log')
    return Tensor(dims, data)


__all__ = [
    'Contract',
    'contract',
]
