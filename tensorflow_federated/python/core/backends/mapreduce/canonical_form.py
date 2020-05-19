# Lint as: python3
# Copyright 2019, The TensorFlow Federated Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Standardized representation of logic deployable to MapReduce-like systems."""

from tensorflow_federated.proto.v0 import computation_pb2
from tensorflow_federated.python.common_libs import py_typecheck
from tensorflow_federated.python.core.api import computation_base
from tensorflow_federated.python.core.api import computation_types
from tensorflow_federated.python.core.impl.types import type_analysis


class CanonicalForm(object):
  """Standardized representation of logic deployable to MapReduce-like systems.

  This standardized representation can be used to describe a range of iterative
  processes representable as a single round of MapReduce-like processing, and
  deployable to MapReduce-like systems that are only capable of executing plain
  TensorFlow code.

  Non-iterative processes, or processes that do not originate at the server can
  be described as well as degenerate cases, but we standardize on the iterative
  process here as the basis for defining the canonical representation, as this
  type of processing is most common in federated learning scenarios, where
  different rounds typically involve different subsets of a potentially very
  large number of participating clients, and the iterative aspect of the
  computation allows for it to not only model processes that evolve over time,
  but also ones that might involve a very large client population in which not
  all participants (clients, data shards, etc.) may be present at the same time,
  and the iterative approach may instead be dictated by data availability or
  scalability considerations. Related to the above, the fact that in practical
  scenarios the set of clients involved in a federated computation will (not
  always, but usually) vary from round to round, the server state is necessarily
  what connects subsequent rounds into a single contiguous logical sequence.

  Instances of this class can be generated by TFF's transformation pipeline and
  consumed by a variety of backends that have the ability to orchestrate their
  execution in a MapReduce-like fashion. The latter can include systems that run
  static data pipelines such Apache Beam or Hadoop, but also platforms like that
  which has been described in the following paper:

  "Towards Federated Learning at Scale: System Design"
  https://arxiv.org/pdf/1902.01046.pdf

  It should be noted that not every computation that proceeds in synchronous
  rounds is representable as an instance of this class. In particular, this
  representation is not suitable for computations that involve multiple phases
  of processing, and does not generalize to arbitrary static data pipelines.
  Generalized representations that can take advantage of the full expressiveness
  of Apache Beam-like systems may emerge at a later time, and will be supported
  by a separate set of tools, with a more expressive canonical representation.

  We say that a `tff.templates.IterativeProcess` is *in the canonical form for a
  simple MapReduce-like platform* if its iterative component (`next`) can be
  converted into a semantically equivalent instance of the computation template
  shown below, with all the variable constituents of the template representable
  as simple sections of TensorFlow logic. A process in such form can then be
  equivalently represented by listing only the variable constituents of the
  template (the tuple of TensorFlow computations that appear as parameters of
  the federated operators). Such compact representations can be encapsulated as
  instances of this class.

  The requirement that the variable constituents of the template be in the form
  of pure TensorFlow code (not arbitrary TFF constructs) reflects the intent
  for instances of this class to be easily converted into a representation that
  can be compiled into a system that does *not* have the ability to interpret
  the full TFF language (as defined in `computation.proto`), but that does have
  the ability to run TensorFlow. Client-side logic in such systems could be
  deployed in a number of ways, e.g., as shards in a MapReduce job, to mobile or
  embedded devices, etc.

  Conceptually, `next`, as the iterator part of an iterative process, is modeled
  in the same way as any stateful computation in TFF, i.e., one that takes the
  server state as the first component of the input, and returns updated server
  state as the first component of the output. If there is no need for server
  state, the input/output state should be modeled as an empty tuple.

  In addition to updating state, `next` additionally takes client-side data as
  input, and can produce results on server-side and/or on the client-side. As
  is the case for the server state, if either of these is undesired, it should
  be modeled as an empty tuple.

  The type signature of `next`, in the concise TFF type notation (as defined in
  TFF's `computation.proto`), is as follows:

  ```python
  (<S@SERVER,{D}@CLIENTS> -> <S@SERVER,X@SERVER,{Y}@CLIENTS>)
  ```

  The above type signature involves the following abstract types:

  * `S` is the type of the state that is passed at the server between rounds of
    processing. For example, in the context of federated training, the server
    state would typically include the weights of the model being trained. The
    weights would be updated in each rounds as the model is trained on more and
    more of the clients' data, and hence the server state would evolve as well.

    Note: This is also the type of the output of the `initialize` that produces
    the server state to feed into the first round (see the constructor argument
    list below).

  * `D` represents the type of per-client units of data that serve as the input
    to the computation. Often, this would be a sequence type, i.e., a data set
    in TensorFlow's parlance, although strictly speaking, this does not have to
    always be the case.

  * `X` represents the type of server-side outputs generated by the server after
    each round.

  * `Y` represents the type of client-side outputs generated on the clients in
    each round. Most computations would not involve client-side outputs.

  One can think of the process based on this representation as being equivalent
  to the following loop:

  ```python
  client_data = ...
  server_state = initialize()
  while True:
    server_state, server_outputs, client_outputs = (
      next(server_state, client_data))
  ```

  The logic of `next` in this form is defined as follows in terms of the seven
  variable components `prepare`, `work`, `zero`, `accumulate`, `merge`,
  `report`, and `update` (in addition to `initialize` that produces the server
  state component for the initial round). The code below uses common syntactic
  shortcuts (such as federated zipping and unzipping) for brevity.

  ```python
  @tff.federated_computation
  def next(server_state, client_data):

    # The server prepares an input to be broadcast to all clients that controls
    # what will happen in this round.

    client_input = (
      tff.federated_broadcast(tff.federated_map(prepare, server_state)))

    # The clients all independently do local work and produce updates, plus the
    # optional client-side outputs.

    client_updates, client_outputs = (
      tff.federated_map(work, [client_data, client_input]))

    # The client updates are aggregated across the system into a single global
    # update at the server.

    global_update = (
      tff.federated_aggregate(client_updates, zero, accumulate, merge, report))

    # Finally, the server produces a new state as well as server-side output to
    # emit from this round.

    new_server_state, server_output = (
      tff.federated_map(update, [server_state, global_update]))

    # The updated server state, server- and client-side outputs are returned as
    # results of this round.

    return new_server_state, server_output, client_outputs
  ```

  The above characterization of `next` depends on the seven pieces of what must
  be pure TensorFlow logic, defined as follows. Please also consult the
  documentation for related federated operators for more detail (particularly
  the `tff.federated_aggregate()`, as sever of the components below correspond
  directly to the parameters of that operator).

  * `prepare` represents the preparatory steps taken by the server to generate
    inputs that will be broadcast to the clients and that, together with the
    client data, will drive the client-side work in this round. It takes the
    initial state of the server, and produces the input for use by the clients.
    Its type signature is `(S -> C)`.

  * `work` represents the totality of client-side processing, again all as a
    single section of TensorFlow code. It takes a tuple of client data and
    client input that was broadcasted by the server, and returns a tuple that
    consists of a client update to be aggregated (across all the clients) along
    with possibly local per-client output (to be consumed locally by clients,
    as it is not being aggregated). Its type signature is `(<D,C> -> <U,Y>)`.
    As noted earlier, the per-client local outputs will typically be absent.
    An example use of such outputs might involve, e.g., debugging metrics that
    might be preserved locally (but not aggregated globally).

  * `zero` is the TensorFlow computation that produces the initial state of
    accumulators that are used to combine updates collected from subsets of the
    client population. In some systems, all accumulation may happen at the
    server, but for scalability reasons, it is often desirable to structure
    aggregation in multiple tiers. Its type signature is `A`, or when
    represented as a `tff.Computation` in Python, `( -> A)`.

  * `accumulate` is the TensorFlow computation that updates the state of an
    update accumulator (initialized with `zero` above) with a single client's
    update. Its type signature is `(<A,U> -> A)`. Typically, a single acumulator
    would be used to combine the updates from multiple clients, but this does
    not have to be the case (it's up to the target deployment platform to choose
    how to use this logic in a particular deployment scenario).

  * `merge` is the TensorFlow computation that merges two accumulators holding
    the results of aggregation over two disjoint subsets of clients. Its type
    signature is `(<A,A> -> A)`.

  * `report` is the TensorFlow computation that transforms the state of the
    top-most accumulator (after accumulating updates from all clients and
    merging all the resulting accumulators into a single one at the top level
    of the system hierarchy) into the final result of aggregation. Its type
    signature is `(A -> R)`.

  * `update` is the TensorFlow computation that applies the aggregate of all
    clients' updates (the output of `report`), also referred to above as the
    global update, to the server state, to produce a new server state to feed
    into the next round, and that additionally outputs a server-side output,
    to be reported externally as one of the results of this round. In federated
    learning scenarios, the server-side outputs might include things like loss
    and accuracy metrics, and the server state to be carried over, as noted
    above, may include the model weights to be trained further in a subsequent
    round. The type signature of this computation is `(<S,R> -> <S,X>)`.

  The above TensorFlow computations' type signatures involves the following
  abstract types in addition to those defined earlier:

  * `C` is the type of the inputs for the clients, to be supplied by the server
    at the beginning of each round (or an empty tuple if not needed).

  * `U` is the type of the per-client update to be produced in each round and
    fed into the cross-client aggregation protocol.

  * `A` is the type of the accumulators used to combine updates from subsets of
    clients.

  * `R` is the type of the final result of aggregating all client updates, the
    global update to be incorporated into the server state at the end of the
    round (and to produce the server-side output).

  The individual TensorFlow computations that constitute an iterative process
  in this form are supplied as constructor arguments.
  """

  def __init__(self, initialize, prepare, work, zero, accumulate, merge, report,
               bitwidth, update):
    """Constructs a representation of a MapReduce-like iterative process.

    Note: All the computations supplied here as arguments must be TensorFlow
    computations, i.e., instances of `tff.Computation` constructed by the
    `tff.tf_computation` decorator/wrapper.

    Args:
      initialize: The computation that produces the initial server state.
      prepare: The computation that prepares the input for the clients.
      work: The client-side work computation.
      zero: The computation that produces the initial state for accumulators.
      accumulate: The computation that adds a client update to an accumulator.
      merge: The computation to use for merging pairs of accumulators.
      report: The computation that produces the final server-side aggregate for
        the top level accumulator (the global update).
      bitwidth: The computation that produces the bitwidth for secure sum.
      update: The computation that takes the global update and the server state
        and produces the new server state, as well as server-side output.

    Raises:
      TypeError: If the Python or TFF types of the arguments are invalid or not
        compatible with each other.
      AssertionError: If the manner in which the given TensorFlow computations
        are represented by TFF does not match what this code is expecting (this
        is an internal error that requires code update).
    """
    for label, comp in [
        ('initialize', initialize),
        ('prepare', prepare),
        ('work', work),
        ('zero', zero),
        ('accumulate', accumulate),
        ('merge', merge),
        ('report', report),
        ('bitwidth', bitwidth),
        ('update', update),
    ]:
      py_typecheck.check_type(comp, computation_base.Computation, label)

      # TODO(b/130633916): Remove private access once an appropriate API for it
      # becomes available.
      comp_proto = comp._computation_proto  # pylint: disable=protected-access

      if not isinstance(comp_proto, computation_pb2.Computation):
        # Explicitly raised to force it to be done in non-debug mode as well.
        raise AssertionError('Cannot find the embedded computation definition.')
      which_comp = comp_proto.WhichOneof('computation')
      if which_comp != 'tensorflow':
        raise TypeError('Expected all computations supplied as arguments to '
                        'be plain TensorFlow, found {}.'.format(which_comp))

    if prepare.type_signature.parameter != initialize.type_signature.result:
      raise TypeError(
          'The `prepare` computation expects an argument of type {}, '
          'which does not match the result type {} of `initialize`.'.format(
              prepare.type_signature.parameter,
              initialize.type_signature.result))

    if (not isinstance(work.type_signature.parameter,
                       computation_types.NamedTupleType) or
        len(work.type_signature.parameter) != 2):
      raise TypeError(
          'The `work` computation expects an argument of type {} that is not '
          'a two-tuple.'.format(work.type_signature.parameter))

    if work.type_signature.parameter[1] != prepare.type_signature.result:
      raise TypeError(
          'The `work` computation expects an argument tuple with type {} as '
          'the second element (the initial client state from the server), '
          'which does not match the result type {} of `prepare`.'.format(
              work.type_signature.parameter[1], prepare.type_signature.result))

    if (not isinstance(work.type_signature.result,
                       computation_types.NamedTupleType) or
        len(work.type_signature.result) != 2):
      raise TypeError(
          'The `work` computation returns a result  of type {} that is not a '
          'two-tuple.'.format(work.type_signature.result))

    py_typecheck.check_type(zero.type_signature, computation_types.FunctionType)

    py_typecheck.check_type(accumulate.type_signature,
                            computation_types.FunctionType)
    py_typecheck.check_len(accumulate.type_signature.parameter, 2)
    type_analysis.check_assignable_from(accumulate.type_signature.parameter[0],
                                        zero.type_signature.result)
    if (accumulate.type_signature.parameter[1] !=
        work.type_signature.result[0][0]):

      raise TypeError(
          'The `accumulate` computation expects a second argument of type {}, '
          'which does not match the expected {} as implied by the type '
          'signature of `work`.'.format(accumulate.type_signature.parameter[1],
                                        work.type_signature.result[0][0]))
    type_analysis.check_assignable_from(accumulate.type_signature.parameter[0],
                                        accumulate.type_signature.result)

    py_typecheck.check_type(merge.type_signature,
                            computation_types.FunctionType)
    py_typecheck.check_len(merge.type_signature.parameter, 2)
    type_analysis.check_assignable_from(merge.type_signature.parameter[0],
                                        accumulate.type_signature.result)
    type_analysis.check_assignable_from(merge.type_signature.parameter[1],
                                        accumulate.type_signature.result)
    type_analysis.check_assignable_from(merge.type_signature.parameter[0],
                                        merge.type_signature.result)

    py_typecheck.check_type(report.type_signature,
                            computation_types.FunctionType)
    type_analysis.check_assignable_from(report.type_signature.parameter,
                                        merge.type_signature.result)

    py_typecheck.check_type(bitwidth.type_signature,
                            computation_types.FunctionType)

    expected_update_parameter_type = computation_types.to_type([
        initialize.type_signature.result,
        [report.type_signature.result, work.type_signature.result[0][1]],
    ])
    if update.type_signature.parameter != expected_update_parameter_type:
      raise TypeError(
          'The `update` computation expects an argument of type {}, '
          'which does not match the expected {} as implied by the type '
          'signatures of `initialize`, `report`, and `work`.'.format(
              update.type_signature.parameter, expected_update_parameter_type))

    if (not isinstance(update.type_signature.result,
                       computation_types.NamedTupleType) or
        len(update.type_signature.result) != 2):
      raise TypeError(
          'The `update` computation returns a result  of type {} that is not '
          'a two-tuple.'.format(update.type_signature.result))

    if update.type_signature.result[0] != initialize.type_signature.result:
      raise TypeError(
          'The `update` computation returns a result tuple with type {} as '
          'the first element (the updated state of the server), which does '
          'not match the result type {} of `initialize`.'.format(
              update.type_signature.result[0],
              initialize.type_signature.result))

    self._initialize = initialize
    self._prepare = prepare
    self._work = work
    self._zero = zero
    self._accumulate = accumulate
    self._merge = merge
    self._report = report
    self._bitwidth = bitwidth
    self._update = update

  @property
  def initialize(self):
    return self._initialize

  @property
  def prepare(self):
    return self._prepare

  @property
  def work(self):
    return self._work

  @property
  def zero(self):
    return self._zero

  @property
  def accumulate(self):
    return self._accumulate

  @property
  def merge(self):
    return self._merge

  @property
  def report(self):
    return self._report

  @property
  def bitwidth(self):
    return self._bitwidth

  @property
  def update(self):
    return self._update

  def summary(self, print_fn=print):
    """Prints a string summary of the `CanonicalForm`.

    Arguments:
      print_fn: Print function to use. It will be called on each line of the
        summary in order to capture the string summary.
    """
    computations = [
        ('initialize', self.initialize),
        ('prepare', self.prepare),
        ('work', self.work),
        ('zero', self.zero),
        ('accumulate', self.accumulate),
        ('merge', self.merge),
        ('report', self.report),
        ('bitwidth', self.bitwidth),
        ('update', self.initialize),
    ]
    for name, comp in computations:
      # Add sufficient padding to align first column; len('initialize') == 10
      print_fn('{:<10}: {}'.format(
          name, comp.type_signature.compact_representation()))
