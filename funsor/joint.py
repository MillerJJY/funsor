import math
from collections import OrderedDict
from functools import reduce

from multipledispatch.variadic import Variadic

import funsor.ops as ops
from funsor.cnf import Contraction
from funsor.delta import MultiDelta
from funsor.gaussian import Gaussian, sym_inverse
from funsor.integrate import Integrate
from funsor.ops import AssociativeOp, SubOp
from funsor.terms import Binary, Funsor, Number, Reduce, Unary, eager, moment_matching, normalize
from funsor.torch import Tensor


@eager.register(Binary, SubOp, MultiDelta, Gaussian)
def eager_add_delta_funsor(op, lhs, rhs):
    if lhs.fresh.intersection(rhs.inputs):
        rhs = rhs(**{name: point for name, point in lhs.terms if name in rhs.inputs})
        return op(lhs, rhs)

    return None  # defer to default implementation


#################################
# patterns for joint integration
#################################

@moment_matching.register(Contraction, AssociativeOp, ops.AddOp, frozenset, (Number, Tensor), Gaussian)
def moment_matching_contract_joint(red_op, bin_op, reduced_vars, discrete, gaussian):

    if red_op is not ops.logaddexp:
        return None

    approx_vars = frozenset(k for k in reduced_vars if gaussian.inputs.get(k, 'real') != 'real')
    exact_vars = reduced_vars - approx_vars

    if exact_vars and approx_vars:
        return Contraction(red_op, bin_op, exact_vars, discrete, gaussian).reduce(red_op, approx_vars)

    if approx_vars and not exact_vars:
        new_discrete = discrete.reduce(ops.logaddexp, approx_vars.intersection(discrete.inputs))
        num_elements = reduce(ops.mul, [
            gaussian.inputs[k].num_elements for k in approx_vars.difference(discrete.inputs)], 1)
        if num_elements != 1:
            new_discrete -= math.log(num_elements)

        int_inputs = OrderedDict((k, d) for k, d in gaussian.inputs.items() if d.dtype != 'real')
        probs = (discrete - new_discrete).exp()
        old_loc = Tensor(gaussian.loc, int_inputs)
        new_loc = (probs * old_loc).reduce(ops.add, approx_vars)
        old_cov = Tensor(sym_inverse(gaussian.precision), int_inputs)
        diff = old_loc - new_loc
        outers = Tensor(diff.data.unsqueeze(-1) * diff.data.unsqueeze(-2), diff.inputs)
        new_cov = ((probs * old_cov).reduce(ops.add, approx_vars) +
                   (probs * outers).reduce(ops.add, approx_vars))
        new_precision = Tensor(sym_inverse(new_cov.data), new_cov.inputs)
        new_inputs = new_loc.inputs.copy()
        new_inputs.update((k, d) for k, d in gaussian.inputs.items() if d.dtype == 'real')
        new_gaussian = Gaussian(new_loc.data, new_precision.data, new_inputs)
        return new_discrete + new_gaussian

    return None


@moment_matching.register(Contraction, AssociativeOp, AssociativeOp, frozenset, Variadic[object])
def moment_matching_contract_default(*args):
    return None


@normalize.register(Integrate, Funsor, Funsor, frozenset)
def normalize_integrate(log_measure, integrand, reduced_vars):
    return Contraction(ops.add, ops.mul, reduced_vars, log_measure.exp(), integrand)


# @normalize.register(Integrate, Contraction, Funsor, frozenset)
# def normalize_integrate_contraction(log_measure, integrand, reduced_vars):
#     delta_terms = [t for t in log_measure.terms if isinstance(t, MultiDelta)
#                    and t.fresh.intersection(reduced_vars, integrand.inputs)]
#     if log_measure.bin_op is ops.add and log_measure.red_op in (ops.logaddexp, anyop) and delta_terms:
#         for delta in delta_terms:
#             integrand = integrand(**{name: point for name, point in delta.terms
#                                      if name in reduced_vars.intersection(integrand.inputs)})
#     return normalize_integrate(log_measure, integrand, reduced_vars)


@eager.register(Contraction, ops.AddOp, ops.MulOp, frozenset, Unary, Funsor)
def eager_contraction_binary(red_op, bin_op, reduced_vars, lhs, rhs):
    if lhs.op is ops.exp and \
            isinstance(lhs.arg, (MultiDelta, Gaussian, Number, Tensor)) and \
            reduced_vars <= lhs.arg.fresh.intersection(rhs.inputs):
        return eager.dispatch(Integrate, lhs.arg, rhs, reduced_vars)
    return eager(Contraction, red_op, bin_op, reduced_vars, (lhs, rhs))


@eager.register(Reduce, ops.AddOp, Unary, frozenset)
def eager_reduce_exp(op, arg, reduced_vars):
    if arg.op is ops.exp and isinstance(arg.arg, (Gaussian, Tensor, MultiDelta)):
        # x.exp().reduce(ops.add) == x.reduce(ops.logaddexp).exp()
        log_result = arg.arg.reduce(ops.logaddexp, reduced_vars)
        if log_result is not normalize(Reduce, ops.logaddexp, arg.arg, reduced_vars):
            return log_result.exp()
    return None
