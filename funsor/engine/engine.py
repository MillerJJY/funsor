from __future__ import absolute_import, division, print_function

from six.moves import reduce
from multipledispatch import dispatch

from funsor.handlers import effectful, Handler, Message, OpRegistry
from funsor.terms import Arange, Binary, Finitary, Funsor, Number, Reduction, Substitution, Tensor, Unary, Variable
from funsor.distributions import Normal


class EagerEval(OpRegistry):
    _terms_processed = {}
    _terms_postprocessed = {}


@EagerEval.register(Tensor)
def eager_tensor(dims, data):
    return Tensor(dims, data).materialize()  # .data


@EagerEval.register(Number)
def eager_number(data, dtype):
    return Number(data, dtype)


# TODO add general Normal
@EagerEval.register(Normal)
def eager_distribution(loc, scale):
    return Normal(loc, scale).materialize()


@EagerEval.register(Variable)
def eager_variable(name, size):
    if isinstance(size, int):
        return Arange(name, size)
    else:
        return Variable(name, size)


@EagerEval.register(Unary)
def eager_unary(op, v):
    return op(v)


@EagerEval.register(Substitution)
def eager_substitution(arg, subs):  # this is the key...
    return Substitution(arg, subs).materialize()


@EagerEval.register(Binary)
def eager_binary(op, lhs, rhs):
    return op(lhs, rhs)


@EagerEval.register(Finitary)
def eager_finitary(op, operands):
    if len(operands) == 1:
        return eager_unary(op, operands[0])  # XXX is this necessary?
    return reduce(op, operands[1:], operands[0])


@EagerEval.register(Reduction)
def eager_reduce(op, arg, reduce_dims):
    assert isinstance(arg, Tensor)  # XXX is this actually true?
    return arg.reduce(op, reduce_dims)


class TailCall(Message):
    pass


class trampoline(Handler):
    """Trampoline to handle tail recursion automatically"""
    def __enter__(self):
        self._schedule = []
        self._args_queue = []
        self._kwargs_queue = []
        self._returnvalue = None
        return super(trampoline, self).__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            while self._schedule:
                fn, nargs, nkwargs = self._schedule.pop(0)
                args = tuple(self._args_queue.pop(0) for i in range(nargs))
                kwargs = dict(self._kwargs_queue.pop(0) for i in range(nkwargs))
                self._args_queue.append(fn(*args, **kwargs))
            self._returnvalue = self._args_queue.pop(0)
            assert not self._args_queue and not self._kwargs_queue
        else:
            self._schedule, self._args_queue, self._kwargs_queue = [], [], []
            self._returnvalue = None
        return super(trampoline, self).__exit__(exc_type, exc_value, traceback)

    @dispatch(object)
    def process(self, msg):
        return super(trampoline, self).process(msg)

    @dispatch(TailCall)
    def process(self, msg):
        msg["stop"] = True  # defer until exit
        msg["value"] = True
        self._schedule.append((msg["fn"], len(msg["args"]), len(msg["kwargs"])))
        self._args_queue.extend(msg["args"])
        self._kwargs_queue.extend(list(msg["kwargs"].items()))
        return msg

    def __call__(self, *args, **kwargs):
        with self:
            self.fn(*args, **kwargs)
        return self._returnvalue


def _tail_call(fn, *args, **kwargs):
    """tail call annotation for trampoline interception"""
    return effectful(TailCall, fn)(*args, **kwargs)


@trampoline
def eval(x):
    r"""
    Overloaded partial evaluation of deferred expression.
    Default semantics: do nothing (reflect)

    This handles a limited class of expressions, raising
    ``NotImplementedError`` in unhandled cases.

    :param Funsor x: An input funsor, typically deferred.
    :return: An evaluated funsor.
    :rtype: Funsor
    :raises: NotImplementedError
    """
    assert isinstance(x, Funsor)

    if isinstance(x, Tensor):
        return _tail_call(effectful(Tensor, Tensor), x.dims, x.data)

    if isinstance(x, Normal):
        return _tail_call(effectful(Normal, Normal),
                          eval(x.params["loc"]), eval(x.params["scale"]))

    if isinstance(x, Number):
        return _tail_call(effectful(Number, Number), x.data, type(x.data))

    if isinstance(x, Variable):
        return _tail_call(effectful(Variable, Variable), x.name, x.shape[0])

    if isinstance(x, Substitution):
        return _tail_call(
            effectful(Substitution, Substitution),
            eval(x.arg),
            tuple((dim, eval(value)) for (dim, value) in x.subs)
        )

    # Arithmetic operations
    if isinstance(x, Unary):
        return _tail_call(effectful(Unary, Unary), x.op, eval(x.v))

    if isinstance(x, Binary):
        return _tail_call(effectful(Binary, Binary), x.op, eval(x.lhs), eval(x.rhs))

    if isinstance(x, Finitary):
        return _tail_call(effectful(Finitary, Finitary), x.op, tuple(eval(tx) for tx in x.operands))

    # Reductions
    if isinstance(x, Reduction):
        return _tail_call(effectful(Reduction, Reduction), x.op, eval(x.arg), x.reduce_dims)

    # TODO Can we simply return x here?
    raise NotImplementedError


__all__ = [
    'eval',
    'EagerEval',
]