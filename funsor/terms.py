from __future__ import absolute_import, division, print_function

import functools
import inspect
import numbers
from collections import OrderedDict
from weakref import WeakValueDictionary

import torch
from six import add_metaclass

import funsor.ops as ops

DOMAINS = ('real', 'density')


def align_tensors(*args):
    r"""
    Permute multiple tensors before applying a broadcasted op.

    This is mainly useful for implementing eager funsor operations.

    :param Tensor \*args: Multiple :class:`Tensor`s.
    :return: a pair ``(dims, tensors)`` where tensors are all
        :class:`torch.Tensor`s that can be broadcast together to a single data
        with ``dims``.
    """
    sizes = OrderedDict()
    for x in args:
        sizes.update(x.schema)
    dims = tuple(sizes)
    tensors = []
    for i, x in enumerate(args):
        x_dims, x = x.dims, x.data
        if x_dims != dims:
            x = x.data.permute(tuple(x_dims.index(d) for d in dims if d in x_dims))
            x = x.reshape(tuple(sizes[d] if d in x_dims else 1 for d in dims))
        assert x.dim() == len(dims)
        tensors.append(x)
    return dims, tensors


class ConsHashedMeta(type):
    _cache = WeakValueDictionary()
    _init_args = {}

    def __call__(cls, *args, **kwargs):
        if kwargs:
            # Convert kwargs to args.
            if cls not in cls._init_args:
                cls._init_args[cls] = inspect.getargspec(cls.__init__)[0][1:]
            args = list(args)
            for name in cls._init_args[cls][len(args):]:
                args.append(kwargs[name])
            args = tuple(args)

        # Memoize creation.
        key = (cls, args)
        if key in cls._cache:
            return cls._cache[key]
        result = super(ConsHashedMeta, cls).__call__(*args)
        cls._cache[key] = result
        return result


@add_metaclass(ConsHashedMeta)
class Funsor(object):
    """
    Abstract base class for immutable functional tensors.

    Derived classes must implement ``__init__()`` methods taking hashable
    ``*args`` and no ``**kwargs`` so as to support cons hashing.

    .. note:: Probabilistic methods like :meth:`sample` and :meth:`marginal`
        follow the convention that funsors represent log density functions.
        Thus for example the partition function is given by :meth:`logsumexp`.

    :param tuple dims: A tuple of strings of dimension names.
    :param tuple shape: A tuple of sizes. Each size is either a nonnegative
        integer or a string denoting a continuous domain.
    """
    def __init__(self, dims, shape):
        assert isinstance(dims, tuple)
        assert all(isinstance(d, str) for d in dims)
        assert len(set(dims)) == len(dims)
        assert isinstance(shape, tuple)
        assert all(isinstance(s, int) or s in DOMAINS for s in shape)
        assert len(dims) == len(shape)
        super(Funsor, self).__init__()
        self.dims = dims
        self.shape = shape
        self.schema = OrderedDict(zip(dims, shape))

    def __hash__(self):
        return id(self)

    def __call__(self, *args, **kwargs):
        """
        Partially evaluates this funsor by substituting dimensions.
        """
        subs = OrderedDict(zip(self.dims, args))
        for d in self.dims:
            if d in kwargs:
                subs[d] = kwargs[d]
        return Substitution(self, tuple(subs.items()))

    def __getitem__(self, args):
        if not isinstance(args, tuple):
            args = (args,)

        # Handle Ellipsis notation like x[..., 0].
        kwargs = {}
        for pos, arg in enumerate(args):
            if arg is Ellipsis:
                kwargs.update(zip(reversed(self.dims),
                                  reversed(args[1 + pos:])))
                break
            kwargs[self.dims[pos]] = arg

        # Handle complete slices like x[:].
        kwargs = {dim: value
                  for dim, value in kwargs.items()
                  if not (isinstance(value, slice) and value == slice(None))}

        return self(**kwargs)

    # Avoid __setitem__ due to immutability.

    def align(self, dims, shape=None):
        """
        Align this funsor to match given ``dims`` and ``shape``.
        This can both permute and add constant dims.
        """
        if shape is None:
            assert set(dims) == set(self.dims)
            shape = tuple(self.schema[d] for d in dims)
        if dims == self.dims:
            assert shape == self.shape
            return self
        return Align(self, dims, shape)

    def __bool__(self):
        if self.shape:
            raise ValueError(
                "bool value of Funsor with more than one value is ambiguous")
        raise NotImplementedError

    def __nonzero__(self):
        return self.__bool__()

    def item(self):
        if self.shape:
            raise ValueError(
                "only one element Funsors can be converted to Python scalars")
        raise NotImplementedError

    def materialize(self):
        """
        Materializes all discrete variables.
        """
        kwargs = {dim: Arange(dim, size)
                  for dim, size in self.schema.items()
                  if isinstance(size, int)}
        return self(**kwargs)

    def unary(self, op):
        """
        Pointwise unary operation.
        """
        return Unary(op, self)

    def binary(self, op, other):
        """
        Broadcasted pointwise binary operation.
        """
        return Binary(op, self, to_funsor(other))

    def reduce(self, op, dims=None):
        """
        Reduce along all or a subset of dimensions.

        :param callable op: A reduction operation.
        :param set dims: An optional dim or set of dims to reduce.
            If unspecified, all dims will be reduced.
        """
        if dims is None:
            dims = frozenset(self.dims)
        else:
            if isinstance(dims, str):
                dims = (dims,)
            dims = frozenset(dims).intersection(self.dims)
        if not dims:
            return self
        return Reduction(op, self, dims)

    def argreduce(self, op, dims):
        """
        Reduce along a subset of dimensions,
        keeping track of the values of those dimensions.

        :param callable op: A reduction operation.
        :param set dims: An optional dim or set of dims to reduce.
        :return: A tuple ``(args, remaining)`` where ``args`` is a
            dict mapping a subset of input dims to funsors possibly depending
            on remaining dims, and ``remaining`` is a funsor depending on
            remaing dims.
        :rtype: tuple
        """
        if isinstance(dims, str):
            dims = (dims,)
        dims = frozenset(dims).intersection(self.dims)
        if not dims:
            return {}, self
        raise NotImplementedError

    # ------------------------------------------------------------------------
    # Subclasses should not override these methods; instead override
    # the generic handlers and fall back to super(...).handler.

    def __invert__(self):
        return self.unary(ops.invert)

    def __neg__(self):
        return self.unary(ops.neg)

    def abs(self):
        return self.unary(ops.abs)

    def sqrt(self):
        return self.unary(ops.sqrt)

    def exp(self):
        return self.unary(ops.exp)

    def log(self):
        return self.unary(ops.log)

    def log1p(self):
        return self.unary(ops.log1p)

    def __add__(self, other):
        return self.binary(ops.add, other)

    def __radd__(self, other):
        return self.binary(ops.add, other)

    def __sub__(self, other):
        return self.binary(ops.sub, other)

    def __rsub__(self, other):
        return self.binary(ops.rsub, other)

    def __mul__(self, other):
        return self.binary(ops.mul, other)

    def __rmul__(self, other):
        return self.binary(ops.mul, other)

    def __truediv__(self, other):
        return self.binary(ops.truediv, other)

    def __rtruediv__(self, other):
        return self.binary(ops.rtruediv, other)

    def __pow__(self, other):
        return self.binary(ops.pow, other)

    def __rpow__(self, other):
        return self.binary(ops.rpow, other)

    def __and__(self, other):
        return self.binary(ops.and_, other)

    def __rand__(self, other):
        return self.binary(ops.and_, other)

    def __or__(self, other):
        return self.binary(ops.or_, other)

    def __ror__(self, other):
        return self.binary(ops.or_, other)

    def __xor__(self, other):
        return self.binary(ops.xor, other)

    def __eq__(self, other):
        return self.binary(ops.eq, other)

    def __ne__(self, other):
        return self.binary(ops.ne, other)

    def __lt__(self, other):
        return self.binary(ops.lt, other)

    def __le__(self, other):
        return self.binary(ops.le, other)

    def __gt__(self, other):
        return self.binary(ops.gt, other)

    def __ge__(self, other):
        return self.binary(ops.ge, other)

    def __min__(self, other):
        return self.binary(ops.min, other)

    def __max__(self, other):
        return self.binary(ops.max, other)

    def sum(self, dims=None):
        return self.reduce(ops.add, dims)

    def prod(self, dims=None):
        return self.reduce(ops.mul, dims)

    def logsumexp(self, dims=None):
        return self.reduce(ops.logaddexp, dims)

    def all(self, dims=None):
        return self.reduce(ops.and_, dims)

    def any(self, dims=None):
        return self.reduce(ops.or_, dims)

    def min(self, dims=None):
        return self.reduce(ops.min, dims)

    def max(self, dims=None):
        return self.reduce(ops.max, dims)

    def argmin(self, dims):
        return self.argreduce(ops.min, dims)

    def argmax(self, dims):
        return self.argreduce(ops.max, dims)

    def marginal(self, dims):
        return self.argreduce(ops.marginal, dims)

    def sample(self, dims):
        """
        Randomly samples from a probability distribution whose
        log probability density is represented by this funsor.

        :param tuple dims: a set of dims to be sampled.
        :return: a tuple ``(values, remaining)`` where ``values`` is a
            dict mapping a subset of input dims to funsors of joint samples
            possibly depending on remaining dims, and ``remaining`` is a funsor
            depending on remaing dims.
        :rtype: tuple
        """
        return self.argreduce(self, ops.sample, dims)


class Variable(Funsor):
    """
    Funsor representing a single free variable.

    :param str name: A variable name.
    :param size: A size, either an int or a ``DOMAIN``.
    """
    def __init__(self, name, size):
        super(Variable, self).__init__((name,), (size,))

    def __repr__(self):
        return "Variable({}, {})".format(repr(self.name), repr(self.shape[0]))

    @property
    def name(self):
        return self.dims[0]

    def __call__(self, *args, **kwargs):
        kwargs.update(zip(self.dims, args))
        if self.name not in kwargs:
            return self
        value = kwargs[self.name]
        if isinstance(value, str):
            return Variable(value, self.shape[0])
        return to_funsor(value)


class Substitution(Funsor):
    def __init__(self, arg, subs):
        assert isinstance(arg, Funsor)
        assert isinstance(subs, tuple)
        for dim_value in subs:
            assert isinstance(dim_value, tuple)
            dim, value = dim_value
            assert isinstance(dim, str)
            assert dim in arg.dims
            assert isinstance(value, Funsor)
        schema = arg.schema.copy()
        for dim, value in subs:
            del schema[dim]
        for dim, value in subs:
            schema.update(value.schema)
        dims = tuple(schema)
        shape = tuple(schema.values())
        super(Substitution, self).__init__(dims, shape)
        self.arg = arg
        self.subs = subs

    def __repr__(self):
        return 'Substitution({}, {})'.format(self.arg, self.subs)

    def __call__(self, *args, **kwargs):
        # TODO eagerly fuse substitutions.
        kwargs.update(zip(self.dims, args))
        subs = {dim: value(**kwargs) for dim, value in self.subs}
        for dim, value in self.subs:
            kwargs.pop(dim, None)
        result = self.arg(**kwargs)(**subs)
        # FIXME for densities, add log_abs_det_jacobian
        return result

    def materialize(self):
        subs = {dim: value.materialize() for dim, value in self.subs}
        return self.arg.materialize()(**subs)

    def argreduce(self, op, dims):
        if isinstance(dims, str):
            dims = (dims,)
        dims = frozenset(dims).intersection(self.dims)
        if not dims:
            return {}, self
        # TODO apply log_abs_det_jacobian of each transform in self.subs
        return super(Substitution, self).argreduce(op, dims)


class Align(Funsor):
    def __init__(self, arg, dims, shape):
        assert isinstance(arg, Funsor)
        assert isinstance(dims, tuple)
        assert isinstance(shape, tuple)
        assert all(isinstance(d, str) for d in dims)
        assert all(isinstance(s, int) or s in DOMAINS for s in shape)
        for d, s in zip(dims, shape):
            assert arg.schema.get(d, s) == s
        super(Align, self).__init__(dims, shape)
        self.arg = arg
        self.dims = dims
        self.shape = shape

    def __call__(self, *args, **kwargs):
        kwargs.update(zip(self.dims, args))
        return self.arg(**kwargs)

    def align(self, dims, shape=None):
        if shape is None:
            assert set(dims) == set(self.dims)
            shape = tuple(self.schema[d] for d in dims)
        return self.arg.align(dims, shape)

    def unary(self, op):
        return self.arg.unary(op)

    def binary(self, op, other):
        return self.arg.binary(op, other)

    def reduce(self, op, dims=None):
        return self.arg.reduce(op, dims)

    def argreduce(self, op, dims):
        return self.arg.reduce(op, dims)


class Unary(Funsor):
    def __init__(self, op, arg):
        assert callable(op)
        assert isinstance(arg, Funsor)
        super(Unary, self).__init__(arg.dims, arg.shape)
        self.op = op
        self.arg = arg

    def __repr__(self):
        return 'Unary({}, {})'.format(self.op.__name__, self.arg)

    def __call__(self, *args, **kwargs):
        return self.arg(*args, **kwargs).unary(self.op)

    def materialize(self):
        return self.arg.materialize().unary(self.op)


class Binary(Funsor):
    def __init__(self, op, lhs, rhs):
        assert callable(op)
        assert isinstance(lhs, Funsor)
        assert isinstance(rhs, Funsor)
        schema = lhs.schema.copy()
        schema.update(rhs.schema)
        dims = tuple(schema)
        shape = tuple(schema.values())
        super(Binary, self).__init__(dims, shape)
        self.op = op
        self.lhs = lhs
        self.rhs = rhs

    def __repr__(self):
        return 'Binary({}, {}, {})'.format(self.op.__name__, self.lhs, self.rhs)

    def __call__(self, *args, **kwargs):
        kwargs.update(zip(self.dims, args))
        return self.lhs(**kwargs).binary(self.op, self.rhs(**kwargs))

    def materialize(self):
        return self.lhs.materialize().binary(self.op, self.rhs.materialize())


class Reduction(Funsor):
    def __init__(self, op, arg, reduce_dims):
        assert callable(op)
        assert isinstance(arg, Funsor)
        assert isinstance(reduce_dims, frozenset)
        dims = tuple(d for d in arg.dims if d not in reduce_dims)
        shape = tuple(arg.schema[d] for d in dims)
        super(Reduction, self).__init__(dims, shape)
        self.op = op
        self.arg = arg
        self.reduce_dims = reduce_dims

    def __repr__(self):
        return 'Reduction({}, {}, {})'.format(
            self.op.__name__, self.arg, self.reduce_dims)

    def __call__(self, *args, **kwargs):
        kwargs = {dim: value for dim, value in kwargs.items()
                  if dim in self.dims}
        kwargs.update(zip(self.dims, args))
        return self.arg(**kwargs).reduce(self.op, self.reduce_dims)

    def materialize(self):
        return self.arg.materialize().reduce(self.op, self.reduce_dims)

    def reduce(self, op, dims=None):
        if op is self.op:
            # Eagerly fuse reductions.
            if dims is None:
                dims = frozenset(self.dims)
            else:
                dims = frozenset(dims).intersection(self.dims)
            return Reduction(op, self.arg, self.reduce_dims | dims)
        return super(Reduction, self).reduce(op, dims)


class AddTypeMeta(ConsHashedMeta):
    def __call__(cls, data, dtype=None):
        if dtype is None:
            dtype = type(data)
        return super(AddTypeMeta, cls).__call__(data, dtype)


@add_metaclass(AddTypeMeta)
class Number(Funsor):
    """
    Funsor backed by a Python number.

    :param numbers.Number data: A python number.
    """
    def __init__(self, data, dtype=None):
        assert isinstance(data, numbers.Number)
        assert dtype == type(data)
        super(Number, self).__init__((), ())
        self.data = data

    def __repr__(self):
        return 'Number({})'.format(repr(self.data))

    def __call__(self, *args, **kwargs):
        return self

    def __bool__(self):
        return bool(self.data)

    def item(self):
        return self.data

    def unary(self, op):
        return Number((), op(self.data))

    def binary(self, op, other):
        if isinstance(other, numbers.Number):
            return Number(op(self.data, other))
        if isinstance(other, Number):
            return Number(op(self.data, other.data))
        if isinstance(other, torch.Tensor):
            assert other.dim() == 0
            return Tensor((), op(self.data, other.data))
        if isinstance(other, Tensor):
            return Tensor(other.dims, op(self.data, other.data))
        return super(Number, self).binary(op, other)


class Tensor(Funsor):
    """
    Funsor backed by a PyTorch Tensor.

    :param tuple dims: A tuple of strings of dimension names.
    :param torch.Tensor data: A PyTorch tensor of appropriate shape.
    """
    def __init__(self, dims, data):
        assert isinstance(data, torch.Tensor)
        super(Tensor, self).__init__(dims, data.shape)
        self.data = data

    def __repr__(self):
        return 'Tensor({}, {})'.format(self.dims, self.data)

    def __call__(self, *args, **kwargs):
        subs = OrderedDict(zip(self.dims, args))
        for d in self.dims:
            if d in kwargs:
                subs[d] = kwargs[d]
        if all(isinstance(v, (Number, numbers.Number, slice, Tensor, str)) for v in subs.values()):
            # Substitute one dim at a time.
            conflicts = list(subs.keys())
            result = self
            for i, (dim, value) in enumerate(subs.items()):
                if isinstance(value, Funsor):
                    if not set(value.dims).isdisjoint(conflicts[1 + i:]):
                        raise NotImplementedError(
                            'TODO implement simultaneous substitution')
                result = result._substitute(dim, value)
            return result
        return super(Tensor, self).__call__(*args, **kwargs)

    def _substitute(self, dim, value):
        pos = self.dims.index(dim)
        if isinstance(value, Number):
            value = value.data
            # Fall through to numbers.Number case.
        if isinstance(value, numbers.Number):
            dims = self.dims[:pos] + self.dims[1+pos:]
            data = self.data[(slice(None),) * pos + (value,)]
            return Tensor(dims, data)
        if isinstance(value, slice):
            if value == slice(None):
                return self
            start = 0 if value.start is None else value.start
            stop = self.shape[pos] if value.stop is None else value.stop
            step = 1 if value.step is None else value.step
            value = Tensor((dim,), torch.arange(start, stop, step))
            # Fall through to Tensor case.
        if isinstance(value, Tensor):
            dims = self.dims[:pos] + value.dims + self.dims[1+pos:]
            index = [slice(None)] * len(self.dims)
            index[pos] = value.data
            for d in value.dims:
                if d != dim and d in self.dims:
                    raise NotImplementedError('TODO')
            data = self.data[tuple(index)]
            return Tensor(dims, data)
        if isinstance(value, str):
            if self.dims[pos] == value:
                return self
            dims = list(self.dims)
            dims[pos] = dim
            return Tensor(tuple(dims), self.data)
        raise RuntimeError('{} should be handled by caller'.format(value))

    def __bool__(self):
        return bool(self.data)

    def item(self):
        return self.data.item()

    def materialize(self):
        return self

    def align(self, dims, shape=None):
        """
        Create an equivalent :class:`Tensor` whose ``.dims`` are
        the provided dims. Note all dims must be accounted for in the input.

        :param tuple dims: A tuple of strings representing all named dims
            but in a new order.
        :return: A permuted funsor equivalent to self.
        :rtype: Tensor
        """
        if shape is None:
            assert set(dims) == set(self.dims)
            shape = tuple(self.schema[d] for d in dims)
        if dims == self.dims:
            assert shape == self.shape
            return self
        if set(dims) == set(self.dims):
            data = self.data.permute(tuple(self.dims.index(d) for d in dims))
            return Tensor(dims, data)
        # TODO unsqueeze and expand
        return Align(self, dims, shape)

    def unary(self, op):
        return Tensor(self.dims, op(self.data))

    def binary(self, op, other):
        if isinstance(other, numbers.Number):
            return Tensor(self.dims, op(self.data, other))
        if isinstance(other, torch.Tensor):
            assert other.dim() == 0
            return Tensor(self.dims, op(self.data, other))
        if isinstance(other, Number):
            return Tensor(self.dims, op(self.data, other.data))
        if isinstance(other, Tensor):
            if self.dims == other.dims:
                return Tensor(self.dims, op(self.data, other.data))
            dims, (self_data, other_data) = align_tensors(self, other)
            return Tensor(dims, op(self_data, other_data))
        return super(Tensor, self).binary(op, other)

    def reduce(self, op, dims=None):
        if op in ops.REDUCE_OP_TO_TORCH:
            torch_op = ops.REDUCE_OP_TO_TORCH[op]
            self_dims = frozenset(self.dims)
            if dims is None:
                dims = self_dims
            else:
                dims = self_dims.intersection(dims)
            if not dims:
                return self
            if dims == self_dims:
                if op is ops.logaddexp:
                    # work around missing torch.Tensor.logsumexp()
                    return Tensor((), self.data.reshape(-1).logsumexp(0))
                return Tensor((), torch_op(self.data))
            data = self.data
            for pos in reversed(sorted(map(self.dims.index, dims))):
                if op in (ops.min, ops.max):
                    data = getattr(data, op.__name__)(pos)[0]
                else:
                    data = torch_op(data, pos)
            dims = tuple(d for d in self.dims if d not in dims)
            return Tensor(dims, data)
        return super(Tensor, self).reduce(op, dims)

    def argreduce(self, op, dims):
        dims = frozenset(dims).intersection(self.dims)
        if not dims:
            return {}, self

        if op in (ops.min, ops.max):
            if len(dims) == 1:
                dim = next(iter(dims))
                pos = self.dims.index(dim)
                value, remaining = getattr(self.data, op.__name__)
                dims = self.dims[:pos] + self.dims
                return {dim: Tensor(dims, value)}, Tensor(dims, remaining)
            raise NotImplementedError('TODO implement multiway argmin, argmax')

        if op is ops.sample:
            if len(dims) == 1:
                dim, = dims
                pos = self.dims.index(dim)
                shift = self.data.max(pos, keepdim=True)
                probs = (self.data - shift).exp()
                remaining = probs.sum(pos).log() + shift.squeeze(pos)

                probs = probs.transpose(pos, -1)
                value = torch.multinomial(probs.reshape(-1, probs.size(-1)), 1)
                value = value.reshape(probs.shape[:-1] + (0,))
                value = value.transpose(pos, -1).squeeze(pos)

                dims = self.dims[:pos] + self.dims[1 + pos:]
                return {dim: Tensor(dims, value)}, Tensor(dims, remaining)
            raise NotImplementedError('TODO implement multiway sample')

        raise NotImplementedError('TODO handle {}'.format(op))


class Arange(Tensor):
    def __init__(self, name, size):
        data = torch.arange(size)
        super(Arange, self).__init__((name,), data)


def to_funsor(x):
    """
    Convert to a :class:`Funsor`.
    Only :class:`Funsor`s and scalars are accepted.
    """
    if isinstance(x, Funsor):
        return x
    if isinstance(x, numbers.Number):
        return Number(x)
    if isinstance(x, torch.Tensor):
        assert x.dim() == 0
        return Tensor((), x)
    raise ValueError("cannot convert to Funsor: {}".format(x))


def _of_shape(fn, shape):
    args, vargs, kwargs, defaults = inspect.getargspec(fn)
    assert not vargs
    assert not kwargs
    dims = tuple(args)
    args = [Variable(dim, size) for dim, size in zip(dims, shape)]
    return to_funsor(fn(*args)).align(dims, shape)


def of_shape(*shape):
    """
    Decorator to construct a :class:`Funsor` with one free :class:`Variable`
    per function arg.
    """
    return functools.partial(_of_shape, shape=shape)


def _lift(fn, *args):
    dims, tensors = align_tensors(*args)
    data = fn(*tensors)
    return Tensor(dims, data)


def lift(fn):
    """
    Lift a PyTorch function ``f(*args)`` to a Funsor function.

    This currently only supports pointwise functions.
    """
    return functools.partial(_lift, fn)


__all__ = [
    'Align',
    'Arange',
    'Binary',
    'DOMAINS',
    'Funsor',
    'Number',
    'Reduction',
    'Substitution',
    'Tensor',
    'Unary',
    'Variable',
    'lift',
    'of_shape',
    'ops',
    'to_funsor',
]
