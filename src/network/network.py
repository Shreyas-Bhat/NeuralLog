"""
Compiles the language into a neural network.
"""
import logging
import sys
from collections import OrderedDict
from typing import Dict, List, Tuple, Any

import numpy as np
import tensorflow as tf
from tensorflow.python import keras
from tensorflow.python.training.tracking import data_structures

from src.knowledge.program import NeuralLogProgram, NO_EXAMPLE_SET, \
    ANY_PREDICATE_NAME, RuleGraph
from src.language.language import Atom, Term, HornClause, Literal, \
    get_renamed_literal, get_substitution, get_variable_indices, Predicate, \
    get_renamed_atom, AtomClause
from src.network.layer_factory import LayerFactory, \
    get_standardised_name
from src.network.network_functions import get_literal_function, \
    get_combining_function, FactLayer, \
    InvertedFactLayer, SpecificFactLayer, LiteralLayer, FunctionLayer, \
    AnyLiteralLayer, RuleLayer, ExtractUnaryLiteralLayer, DiagonalRuleLayer, \
    EmptyLayer, get_literal_layer

# WARNING: Do not support literals with same variable in the head of rules.
# WARNING: Do not support constants in the head of rules.
# WARNING: Do not support literals with constant numbers in the rules.

# WARNING: For now, we only use a generic rule to predict a specific
#  fact. For instance, h(X, Y) :- ... to predict h(X, a).
#  We should also use the other way around, use a rule
#  h(X, a) :- ... to predict facts h(X, Y). which will return
#  the values for h(X, a); and zero for every Y != a.

logger = logging.getLogger()


class LossMaskWrapper:
    """
    A mask wrapper for the loss function to mask the values of unknown labels.

    It multiplies the output of the network by the square of the labels. In
    order to this method work, the labels must be: `1`, for positive examples;
    `-1`, for negative examples; and `0`, for unknown examples.

    In this way, the square of the labels will be `1` for the positive and
    negative examples; and `0`, for the unknown examples. When multiplied by
    the prediction, the predictions of the unknown examples will be zero,
    thus, having no error and no gradient for those examples. While the
    predictions of the known examples will remain the same.
    """

    def __init__(self, loss_function, label_function=None):
        """
        Creates a loss mask wrapper.

        :param loss_function: the loss function to wrap.
        :type loss_function: function
        """
        self.loss_function = loss_function
        self.function = keras.losses.get(loss_function)
        self.label_function = label_function
        self.__name__ = self.function.__name__

    def call(self, y_true, y_pred):
        """
        The wrapped function.

        :param y_true: the true labels
        :type y_true: tf.Tensor, list[tf.Tensor], np.ndarray, list[np.ndarray]
        :param y_pred: the predictions
        :type y_pred: tf.Tensor, list[tf.Tensor], np.ndarray, list[np.ndarray]
        :return: the wrapped function
        :rtype: function
        """
        if isinstance(y_pred, list):
            new_y_pred = []
            for i in range(len(y_pred)):
                mask = tf.square(y_true[i])
                new_y_pred.append(y_pred[i] * mask)
        else:
            mask = tf.square(y_true)
            new_y_pred = y_pred * mask
        if self.label_function is not None:
            y_true = self.label_function(y_true)
        return self.function(y_true, new_y_pred)

    __call__ = call

    def __str__(self):
        return "{}({})".format(self.__class__.__name__,
                               self.loss_function.__str__())

    def __repr__(self):
        return "{}({})".format(self.__class__.__name__,
                               self.loss_function.__repr__())


# noinspection PyTypeChecker
def print_neural_log_predictions(model, neural_program, dataset,
                                 writer=sys.stdout, dataset_name=None):
    """
    Prints the predictions of `model` to `writer`.

    :param model: the model
    :type model: NeuralLogNetwork
    :param neural_program: the neural program
    :type neural_program: NeuralLogProgram
    :param dataset: the dataset
    :type dataset: tf.data.Dataset
    :param writer: the writer. Default is to print to the standard output
    :type writer: Any
    :param dataset_name: the name of the dataset
    :type dataset_name: str
    """
    for features, _ in dataset:
        y_scores = model.predict(features)
        if len(model.predicates) == 1:
            y_scores = [y_scores]
            features = [features]
        for i in range(len(model.predicates)):
            predicate, inverted = model.predicates[i]
            if inverted:
                continue
            for feature, y_score in zip(features[i], y_scores[i]):
                x = feature.numpy()
                if x.max() == 0.0:
                    continue
                subject_index = np.argmax(x)
                subject = neural_program.get_constant_by_index(
                    predicate, 0, subject_index)
                if predicate.arity == 1:
                    clause = AtomClause(Atom(predicate, subject,
                                             weight=float(y_score)))
                    print(clause, file=writer)
                else:
                    clauses = []
                    for index in range(len(y_score)):
                        object_term = neural_program.get_constant_by_index(
                            predicate, 1, index)
                        prediction = Atom(predicate, subject, object_term,
                                          weight=float(y_score[index]))
                        if dataset_name is not None and \
                                prediction.simple_key() not in \
                                neural_program.examples[
                                    dataset_name][predicate]:
                            continue
                        clauses.append(AtomClause(prediction))

                    if len(clauses) > 0:
                        clause = AtomClause(Atom(predicate, subject, "X"))
                        print("%%", clause, file=writer, sep=" ")
                        for clause in sorted(
                                clauses,
                                key=lambda c: c.atom.weight,
                                reverse=True):
                            print(clause, file=writer)
                        print(file=writer)
            # print(file=writer)


def is_cyclic(atom, previous_atoms):
    """
    Check if there is a cycle between the current atom and the previous
    atoms. If the atom's predicate appears in a atom in previous atoms,
    then, there is a cycle.

    :param atom: the current atom
    :type atom: Atom
    :param previous_atoms: the previous atoms
    :type previous_atoms: list[Atom] or set[Atom]
    :return: True if there is a cycle; False, otherwise
    :rtype: bool
    """
    if previous_atoms is None or len(previous_atoms) == 0:
        return False

    for previous_atom in previous_atoms:
        if atom.predicate == previous_atom.predicate:
            if get_substitution(previous_atom, atom) is not None:
                return True

    return False


class CyclicProgramException(Exception):
    """
    Represents a cyclic program exception.
    """

    def __init__(self, atom) -> None:
        """
        Creates an term malformed exception.

        :param atom: the atom
        :type atom: Atom
        """
        super().__init__("Cyclic program, cannot create the Predicate Node for "
                         "{}".format(atom))


def is_clause_fact(clause):
    """
    Returns true if the clause degenerates to a fact, this happens when the
    head of the clause is equals to the the body, for instance:
    `h(X, Y) :- h(X, Y).`. This kind of clauses can be ignored since it is
    already represented by the facts of `h(X, Y)`.

    :param clause: the clause
    :type clause: HornClause
    :return: `True` if the the clause degenerates to a fact; `False` otherwise.
    :rtype: bool
    """
    if len(clause.body) != 1:
        return False
    body = clause.body[0]
    if body.negated:
        return False

    return clause.head.predicate == body.predicate and \
           clause.head.terms == body.terms


def log_equivalent_clause(current_clause, older_clause):
    """
    Logs the redundant clause.

    :param current_clause: the current clause
    :type current_clause: HornClause
    :param older_clause: the older clause
    :type older_clause: HornClause
    """
    if logger.isEnabledFor(logging.WARNING):
        if older_clause is None:
            return
        start_line = current_clause.provenance.start_line
        clause_filename = current_clause.provenance.filename

        old_start_line = older_clause.provenance.start_line
        old_clause_filename = older_clause.provenance.filename
        logger.warning(
            "Warning: clause `%s`, defined in file: %s "
            "at %d ignored. The clause has already been "
            "defined in in file: %s at %d.",
            current_clause, clause_filename,
            start_line,
            old_clause_filename,
            old_start_line
        )


class NeuralLogNetwork(keras.Model):
    """
    The NeuralLog
    Network.
    """

    _literal_layers: Dict[Tuple[Literal, bool], LiteralLayer] = dict()
    "The literal layer by literal"

    _fact_layers: Dict[Tuple[Atom, bool], FactLayer] = dict()
    "The fact layer by literal"

    _rule_layers: Dict[Tuple[HornClause, bool], RuleLayer] = dict()
    "The rule layer by clause"

    _function_by_predicate: Dict[Predicate, Any] = dict()
    "The function by predicate"

    program: NeuralLogProgram
    "The NeuralLog program"

    predicates: List[Tuple[Predicate, bool]]

    def __init__(self, program, train=True, inverse_relations=True):
        """
        Creates a NeuralLogNetwork.

        :param program: the neural language
        :type program: NeuralLogProgram
        :param train: if `False`, all the literals will be considered as not
        trainable/learnable, this is useful to build neural networks for
        inference only. In this way, the unknown facts will be treated as
        zeros, instead of being randomly initialized
        :param inverse_relations: if `True`, also creates the layers for the
        inverse relations.
        :type inverse_relations: bool
        :type train: bool
        """
        super(NeuralLogNetwork, self).__init__(name="NeuralLogNetwork")
        self.program = program
        self.layer_factory = LayerFactory(self.program, train=train)
        # noinspection PyTypeChecker
        self.predicates = data_structures.NoDependency(list())
        self.predicate_layers = list()
        self.neutral_element = self._get_edge_neutral_element()
        self.neutral_element = tf.reshape(self.neutral_element, [1, 1])
        self.inverse_relations = inverse_relations
        self.empty_layer = EmptyLayer("empty")

    def get_recursion_depth(self, predicate=None):
        """
        Gets the maximum recursion depth for the predicate.

        :param predicate: the predicate
        :type predicate: Predicate
        :return: the maximum recursion depth
        :rtype: int
        """
        value = self.program.get_parameter_value("recursion_depth", predicate)
        sys.setrecursionlimit(max(sys.getrecursionlimit(), 15 * value))
        return value

    def get_literal_negation_function(self, predicate):
        """
        Gets the literal negation function for the atom. This function is the
        function to be applied when the atom is negated.

        The default function 1 - `a`, where `a` is the tensor representation
        of the atom.

        :param predicate: the predicate
        :type predicate: Predicate
        :return: the negation function
        :rtype: function
        """
        name = self.program.get_parameter_value("literal_negation_function",
                                                predicate)
        return get_literal_function(name)

    def _get_path_combining_function(self, predicate=None):
        """
        Gets the path combining function. This is the function to combine
        different path from a RuleLayer.

        The default is to multiply all the paths, element-wise, by applying the
        `tf.math.multiply` function.

        :param predicate: the predicate
        :type predicate: Predicate
        :return: the combining function
        :rtype: function
        """
        combining_function = self.program.get_parameter_value(
            "path_combining_function", predicate)
        return get_combining_function(combining_function)

    def _get_edge_neutral_element(self):
        """
        Gets the neutral element of the edge combining function. This element is
        used to extract the tensor value of grounded literal in a rule.

        The default edge combining function is the element-wise
        multiplication. Thus, the neutral element is `1.0`, represented by
        `tf.constant(1.0)`.

        :return: the combining function
        :rtype: tf.Tensor
        """
        combining_function = self.program.get_parameter_value(
            "edge_neutral_element")
        return get_combining_function(combining_function)

    def _get_any_literal(self):
        """
        Gets the any literal layer.

        :return: the any literal layer
        :rtype: EndAnyLiteralLayer
        """
        combining_function = self.program.get_parameter_value(
            "any_aggregation_function")
        function = get_combining_function(combining_function)
        return AnyLiteralLayer("literal_layer_any-X0-X1-", function)

    def build_layers(self):
        """
        Builds the layers of the network.
        """
        for example_set in self.program.examples.values():
            for predicate in example_set:
                logger.debug("Building output layer for predicate: %s",
                             predicate)
                literal = Literal(Atom(predicate,
                                       *list(map(lambda x: "X{}".format(x),
                                                 range(predicate.arity)))))
                if (predicate, False) not in self.predicates:
                    predicate_layer = self._build_literal(literal, dict())
                    if predicate.arity == 1:
                        combining_func = \
                            self.get_unary_literal_extraction_function(
                                predicate)
                        predicate_layer = ExtractUnaryLiteralLayer(
                            predicate_layer, combining_func)
                    self.predicates.append((predicate, False))
                    self.predicate_layers.append(predicate_layer)
                key = (predicate, True)
                if self.inverse_relations and predicate.arity == 2 and \
                        key not in self.predicates:
                    predicate_layer = self._build_literal(literal, dict(),
                                                          inverted=True)
                    self.predicates.append(key)
                    self.predicate_layers.append(predicate_layer)
        if len(self.predicates) == 1:
            self.call = self.call_single_input
        else:
            # noinspection PyAttributeOutsideInit
            self.call = self.call_multiples_inputs

    def get_unary_literal_extraction_function(self, predicate):
        """
        Gets the unary literal extraction function. This is the function to
        extract the value of unary prediction.

        The default is the dot multiplication, implemented by the
        `tf.matmul`, applied to the transpose of the literal prediction.

        :param predicate: the predicate
        :type predicate: Predicate
        :return: the combining function
        :rtype: function
        """
        combining_function = self.program.get_parameter_value(
            "unary_literal_extraction_function", predicate)
        return get_combining_function(combining_function)

    # noinspection PyMissingOrEmptyDocstring,PyUnusedLocal
    def call_single_input(self, inputs, training=None, mask=None):
        results = []
        for i in range(len(self.predicate_layers)):
            results.append(self.predicate_layers[i](inputs))
        return tuple(results)

    # noinspection PyMissingOrEmptyDocstring,PyUnusedLocal
    def call_multiples_inputs(self, inputs, training=None, mask=None):
        results = []
        for i in range(len(self.predicate_layers)):
            results.append(self.predicate_layers[i](inputs[i]))
        return tuple(results)

    # noinspection PyMissingOrEmptyDocstring
    def compute_output_shape(self, input_shape):
        shape = tf.TensorShape(input_shape).as_list()
        return tf.TensorShape(shape)

    # noinspection PyMissingOrEmptyDocstring
    def _build_literal(self, atom, predicates_depths, inverted=False):
        """
        Builds the layer for the literal.

        :param atom: the atom
        :type atom: Atom
        :param predicates_depths: the depths of the predicates
        :type predicates_depths: dict[Predicate, int]
        :param inverted: if `True`, creates the inverted literal; this is,
            a literal in the format (output, input). If `False`, creates the
            standard (input, output) literal format.
        :type inverted: bool
        :return: the predicate layer
        :rtype: LiteralLayer or src.network.network_functions.FunctionLayer
        """
        renamed_literal = get_renamed_literal(atom)
        key = (renamed_literal, inverted)
        literal_layer = self._literal_layers.get(key, None)
        if literal_layer is None:
            if atom.predicate in self.program.logic_predicates:
                logger.debug("Building layer for literal: %s", renamed_literal)
                predicates_depths.setdefault(atom.predicate, -1)
                predicates_depths[atom.predicate] += 1
                literal_layer = self._build_logic_literal_layer(
                    renamed_literal, predicates_depths, inverted)
                predicates_depths[atom.predicate] -= 1
            else:
                logger.debug("Building layer for function: %s", renamed_literal)
                literal_layer = self._build_function_layer(renamed_literal)
            self._literal_layers[key] = literal_layer
        return literal_layer

    def _build_logic_literal_layer(self, renamed_literal, predicates_depths,
                                   inverted):
        """
        Builds the logic literal layer.

        :param renamed_literal: the renamed literal
        :type renamed_literal: Atom
        :param predicates_depths: the depths of the predicates
        :type predicates_depths: dict[Predicate, int]
        :param inverted: if `True`, creates the inverted literal; this is,
            a literal in the format (output, input). If `False`, creates the
            standard (input, output) literal format.
        :type inverted: bool
        :return: the literal layer
        :rtype: LiteralLayer
        """
        predicate = renamed_literal.predicate
        depth = predicates_depths[predicate]
        if depth < self.get_recursion_depth(predicate) + 1:
            inputs = []
            if (predicate in self.program.facts_by_predicate or
                    predicate in self.program.trainable_predicates):
                inputs = [self._build_fact(renamed_literal, inverted=inverted)]
            input_clauses = dict()  # type: Dict[RuleLayer, HornClause]
            for clause in self.program.clauses_by_predicate.get(
                    predicate, []):
                if is_clause_fact(clause):
                    continue
                substitution = get_substitution(clause.head, renamed_literal)
                if substitution is None:
                    continue
                rule = self._build_rule(clause, predicates_depths, inverted)
                if rule is None:
                    continue
                rule = self._build_specific_rule(
                    renamed_literal, inverted, rule, substitution)
                if rule in input_clauses:
                    log_equivalent_clause(clause, input_clauses[rule])
                    continue
                input_clauses[rule] = clause
                inputs.append(rule)
        else:
            inputs = [self.empty_layer]

        combining_func = self.get_literal_combining_function(renamed_literal)
        negation_function = None
        if isinstance(renamed_literal, Literal) and renamed_literal.negated:
            negation_function = self.get_literal_negation_function(
                predicate)
        return LiteralLayer(
            "literal_layer_{}".format(
                get_standardised_name(renamed_literal.__str__())), inputs,
            combining_func, negation_function=negation_function)

    def _build_specific_rule(self, literal, inverted, rule, substitution):
        """
        Builds a specific rule from a more generic one.

        :param literal: the literal
        :type literal: Atom
        :param inverted: if `True`, creates the inverted literal; this is,
            a literal in the format (output, input). If `False`, creates the
            standard (input, output) literal format.
        :type inverted: bool
        :param rule: the general rule
        :type rule: RuleLayer
        :param substitution: the dictionary with the substitution from the
        generic term to the specific one
        :type substitution: dict[Term, Term]
        :return: the specific rule
        :rtype: SpecificFactLayer
        """
        predicate = literal.predicate
        substitution_terms = dict()
        last_term = None
        equal_terms = False
        for generic, specific in substitution.items():
            equal_terms = last_term == specific or last_term is None
            last_term = specific
            if not generic.is_constant() and specific.is_constant():
                substitution_terms[specific] = generic

        if len(substitution_terms) > 0:
            source = literal.terms[-1 if inverted else 0]
            destination = literal.terms[0 if inverted else -1]
            literal_string = literal.__str__()
            if inverted:
                literal_string = "inv_" + literal_string
            layer_name = get_standardised_name(
                "{}_specific_{}".format(rule.name, literal_string))
            input_constant = None
            input_combining_function = None
            output_constant = None
            output_extract_func = None

            if source.is_constant() and source in substitution_terms:
                term_index = 1 if inverted else 0
                input_constant = self.layer_factory.get_one_hot_tensor(
                    literal, term_index)
                input_combining_function = \
                    self.layer_factory.get_and_combining_function(predicate)
            if destination.is_constant() and destination in substitution_terms:
                term_index = 0 if inverted else 1
                output_constant = \
                    self.layer_factory.get_constant_lookup(literal, term_index)
                output_extract_func = \
                    self.layer_factory.get_output_extract_function(predicate)
            rule = SpecificFactLayer(
                layer_name, rule,
                input_constant=input_constant,
                input_combining_function=input_combining_function,
                output_constant=output_constant,
                output_extract_function=output_extract_func
            )
        elif equal_terms and predicate.arity > 1:
            rule = DiagonalRuleLayer(
                rule, self.layer_factory.get_and_combining_function(predicate))
        return rule

    def get_literal_combining_function(self, literal):
        """
        Gets the combining function for the `literal`. This is the function to
        combine the different proves of a literal (FactLayers and RuleLayers).

        The default is to sum all the proves, element-wise, by applying the
        `tf.math.add_n` function.

        :param literal: the literal
        :type literal: Atom
        :return: the combining function
        :rtype: function
        """
        literal_combining_function = self.program.get_parameter_value(
            "literal_combining_function", literal.predicate)
        return get_combining_function(literal_combining_function)

    def _build_function_layer(self, renamed_literal):
        """
        Builds the logic literal layer.

        :param renamed_literal: the renamed literal
        :type renamed_literal: Atom
        :return: the function layer
        :rtype: src.network.network_functions.FunctionLayer
        """
        function_identifier = self.program.get_parameter_value(
            "function_value", renamed_literal.predicate)
        if function_identifier is None:
            function_identifier = renamed_literal.predicate.name
        function_value = self._get_predicate_function(
            renamed_literal.predicate, function_identifier)
        inputs = None
        term = renamed_literal.terms[0]
        if term.is_constant():
            inputs = self.layer_factory.get_one_hot_tensor(renamed_literal, 0)
        name = "literal_layer_{}".format(
            get_standardised_name(renamed_literal.__str__()))
        return FunctionLayer(name, function_value, inputs=inputs)

    def _get_predicate_function(self, predicate, function_identifier):
        """
        Gets the predicate function for the predicate.

        :param predicate: the predicate
        :type predicate: Predicate
        :param function_identifier: the function identifier
        :type function_identifier: str or dict
        :return: the predicate function
        :rtype: function
        """
        function_value = self._function_by_predicate.get(predicate, None)
        if function_value is None:
            try:
                function_value = get_literal_function(function_identifier)
            except (ValueError, TypeError):
                function_value = get_literal_layer(function_identifier)
            self._function_by_predicate[predicate] = function_value
        return function_value

    def _build_rule(self, clause, predicates_depths, inverted=False):
        """
        Builds the Rule Node.

        :param clause: the clause
        :type clause: HornClause
        :param predicates_depths: the depths of the predicates
        :type predicates_depths: dict[Predicate, int]
        :param inverted: if `True`, creates the layer for the inverted rule;
            this is, the rule in the format (output, input). If `False`,
            creates the layer for standard (input, output) rule format.
        :type inverted: bool
        :return: the rule layer
        :rtype: RuleLayer
        """
        key = (clause, inverted)
        rule_layer = self._rule_layers.get(key, None)
        if rule_layer is None:
            logger.debug("Building layer for rule: %s", clause)
            rule_graph = RuleGraph(clause)
            paths, grounds = rule_graph.find_clause_paths(inverted)

            layer_paths = []
            for path in paths:
                layer_path = []
                for i in range(len(path)):
                    if path[i].predicate.name == ANY_PREDICATE_NAME:
                        literal_layer = self._get_any_literal()
                    else:
                        literal_layer = self._build_literal(
                            path[i], predicates_depths, path.inverted[i])
                    layer_path.append(literal_layer)
                layer_paths.append(layer_path)

            grounded_layers = []
            for grounded in grounds:
                literal_layer = self._build_literal(grounded, predicates_depths)
                grounded_layers.append(literal_layer)
            layer_name = "rule_layer_{}".format(
                get_standardised_name(clause.__str__()))
            rule_layer = \
                RuleLayer(
                    layer_name, layer_paths, grounded_layers,
                    self._get_path_combining_function(clause.head.predicate),
                    self.neutral_element)
            self._rule_layers[key] = rule_layer

        return rule_layer

    def _build_fact(self, atom, inverted=False):
        """
        Builds the fact layer for the atom.

        :param atom: the atom
        :type atom: Atom
        :param inverted: if `True`, creates the inverted fact; this is,
        a fact in the format (output, input). If `False`, creates the
        standard (input, output) fact format.
        :type inverted: bool
        :return: the fact layer
        :rtype: FactLayer
        """
        renamed_atom = get_renamed_atom(atom)
        key = (renamed_atom, inverted)
        fact_layer = self._fact_layers.get(key, None)
        if fact_layer is None:
            logger.debug("Building layer for fact: %s", renamed_atom)
            fact_layer = self.layer_factory.build_atom(renamed_atom)
            if inverted:
                fact_layer = InvertedFactLayer(
                    fact_layer, self.layer_factory, atom.predicate)
            self._fact_layers[key] = fact_layer

        return fact_layer

    def get_invert_fact_function(self, literal):
        """
        Gets the fact inversion function. This is the function to extract
        the inverse of a facts.

        The default is the transpose function implemented by `tf.transpose`.

        :param literal: the literal
        :type literal: Atom
        :return: the combining function
        :rtype: function
        """
        combining_function = self.program.get_parameter_value(
            "invert_fact_function", literal.predicate)
        return get_combining_function(combining_function)

    # noinspection PyTypeChecker
    def update_program(self):
        """
        Updates the program based on the learned parameters.
        """
        for atom, tensor in self.layer_factory.variable_cache.items():
            variable_indices = get_variable_indices(atom)
            rank = len(variable_indices)
            values = tensor.numpy()
            size_0 = self.program.get_constant_size(atom.predicate, 0)
            if rank == 0:
                fact = Atom(atom.predicate, *atom.terms, weight=values)
                self.program.add_fact(fact)
            elif rank == 1:
                for i in range(size_0):
                    fact = Atom(atom.predicate, *atom.terms, weight=values[i])
                    fact.terms[variable_indices[0]] = \
                        self.program.get_constant_by_index(
                            atom.predicate, 0, i)
                    self.program.add_fact(fact)
            elif rank == 2:
                size_1 = self.program.get_constant_size(atom.predicate, 1)
                for i in range(size_0):
                    for j in range(size_1):
                        fact = Atom(atom.predicate, *atom.terms,
                                    weight=values[i, j])
                        fact.terms[variable_indices[0]] = \
                            self.program.get_constant_by_index(
                                atom.predicate, 0, i)
                        fact.terms[variable_indices[1]] = \
                            self.program.get_constant_by_index(
                                atom.predicate, 1, j)
                        self.program.add_fact(fact)


def get_predicate_indices(predicate, inverted):
    """
    Gets the indices of the predicate's input and output.

    :param predicate: the predicate
    :type predicate: Predicate
    :param inverted: if the predicate is inverted
    :type inverted: bool
    :return: the input and output indices
    :rtype: (int, int)
    """
    if predicate.arity == 1:
        input_index = 0
        output_index = 0
    else:
        if inverted:
            input_index = 1
            output_index = 0
        else:
            input_index = 0
            output_index = 1
    return input_index, output_index


class NeuralLogDataset:
    """
    Represents a NeuralLog dataset to train a NeuralLog network.
    """

    network: NeuralLogNetwork
    "The NeuralLog program"

    examples: Dict[Term, Dict[Predicate, Dict[Term, float] or float]]

    def __init__(self, network):
        """
        Creates a NeuralLogNetwork.

        :param network: the NeuralLog network
        :type network: NeuralLogNetwork
        """
        self.network = network
        self.program = network.program

    # noinspection PyUnusedLocal
    def call(self, features, labels, *args, **kwargs):
        """
        Used to transform the features and examples from the sparse
        representation to dense in order to train the network.

        :param features: A dense index tensor of the features
        :type features: tuple[tf.SparseTensor]
        :param labels: A tuple sparse tensor of labels
        :type labels: tuple[tf.SparseTensor]
        :param args: additional arguments
        :type args: list
        :param kwargs: additional arguments
        :type kwargs: dict
        :return: the features and label tensors
        :rtype: (tf.Tensor or tuple[tf.Tensor], tuple[tf.Tensor])
        """
        dense_features = []
        for i in range(len(self.network.predicates)):
            predicate, inverted = self.network.predicates[i]
            index, _ = get_predicate_indices(predicate, inverted)
            feature = tf.one_hot(
                features[i],
                self.program.get_constant_size(predicate, index))
            dense_features.append(feature)

        labels = tuple(map(lambda x: tf.sparse.to_dense(x), labels))

        if len(dense_features) > 1:
            dense_features = tuple(dense_features)
        else:
            dense_features = dense_features[0]

        return dense_features, labels

    __call__ = call

    def get_dataset(self, example_set=NO_EXAMPLE_SET,
                    batch_size=1, shuffle=False):
        """
        Gets the data set for the example set.

        :param example_set: the name of the example set
        :type example_set: str
        :param batch_size: the batch size
        :type batch_size: int
        :param shuffle: if `True`, shuffles the dataset.
        :type shuffle: bool
        :return: the dataset
        :rtype: tf.data.Dataset
        """
        features, labels = self.build(example_set=example_set)
        # noinspection PyTypeChecker
        dataset_size = len(features[0])
        dataset = tf.data.Dataset.from_tensor_slices((features, labels))
        if shuffle:
            dataset = dataset.shuffle(dataset_size)
        dataset = dataset.batch(batch_size)
        dataset = dataset.map(self)
        logger.info("Dataset %s created with %d example(s)", example_set,
                    dataset_size)
        return dataset

    def build(self, example_set=NO_EXAMPLE_SET):
        """
        Builds the features and label to train the neural network based on
        the `example_set`.

        The labels are always a sparse tensor.

        :param example_set: the name of the set of examples
        :type example_set: str
        sparse tensor. If `False`, the features are generated as a dense
        tensor of indices, for each index a one hot vector creation is
        necessary.
        :return: the features and labels
        :rtype: (tuple[tf.SparseTensor], tuple[tf.SparseTensor])
        """
        output_by_term = OrderedDict()
        input_terms = []
        examples = self.program.examples.get(example_set, OrderedDict())
        for predicate, inverted in self.network.predicates:
            facts = examples.get(predicate, dict()).values()
            for fact in facts:
                input_term = fact.terms[-1 if inverted else 0]
                if input_term not in output_by_term:
                    output = dict()
                    output_by_term[input_term] = output
                    input_terms.append(input_term)
                else:
                    output = output_by_term[input_term]
                if predicate.arity == 1:
                    output[(predicate, inverted)] = fact.weight
                else:
                    output_term = fact.terms[0 if inverted else -1]
                    # noinspection PyTypeChecker
                    output.setdefault((predicate, inverted), []).append(
                        (output_term, fact.weight))

        all_features = []
        all_labels = []
        for predicate, inverted in self.network.predicates:
            features = []
            label_values = []
            label_indices = []
            input_index, output_index = get_predicate_indices(predicate,
                                                              inverted)
            for i in range(len(input_terms)):
                index = self.program.get_index_of_constant(
                    predicate, input_index, input_terms[i])
                if index is None:
                    index = -1
                features.append(index)
                outputs = output_by_term[input_terms[i]].get(
                    (predicate, inverted), None)
                if outputs is not None:
                    if predicate.arity == 1:
                        label_indices.append([i, 0])
                        label_values.append(outputs)
                    else:
                        for output_term, output_value in outputs:
                            output_term_index = \
                                self.program.get_index_of_constant(
                                    predicate, output_index, output_term)
                            label_indices.append([i, output_term_index])
                            label_values.append(output_value)

            all_features.append(features)
            if predicate.arity == 1:
                dense_shape = [len(input_terms), 1]
                empty_index = [[0, 0]]
            else:
                dense_shape = [
                    len(input_terms),
                    self.program.get_constant_size(predicate, output_index)]
                empty_index = [[0, 0]]
            if len(label_values) == 0:
                sparse_tensor = tf.SparseTensor(indices=empty_index,
                                                values=[0.0],
                                                dense_shape=dense_shape)
            else:
                sparse_tensor = tf.SparseTensor(indices=label_indices,
                                                values=label_values,
                                                dense_shape=dense_shape)
            sparse_tensor = tf.sparse.reorder(sparse_tensor)
            all_labels.append(sparse_tensor)

        return tuple(all_features), tuple(all_labels)
