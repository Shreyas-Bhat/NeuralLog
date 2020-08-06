"""
Handle the revision operators based on Meta Logic Programs.
"""
import copy
import logging
import re
from collections import deque
from typing import Set, Tuple, TypeVar, Generic, Optional, Iterable, List, \
    Generator, Union, Deque, Sequence, Dict, Iterator

import src.knowledge.theory.manager.revision.operator.revision_operator as ro
from src.knowledge.examples import Examples, ExampleIterator
from src.knowledge.graph import RulePathFinder, RuleGraph
from src.knowledge.manager.tree_manager import TreeTheory, Node, \
    FALSE_LITERAL, add_clause_to_tree
from src.knowledge.program import NeuralLogProgram, get_predicate_from_string
from src.knowledge.theory import TheoryRevisionException
from src.language.language import Atom, HornClause, Predicate, Variable, \
    get_term_from_string, Literal, get_variable_atom, KnowledgeException, \
    AtomClause, Quote, Clause
from src.language.parser.ply.neural_log_parser import NeuralLogLexer, \
    NeuralLogParser
from src.util import OrderedSet, InitializationException
from src.util.language import get_unify_map
from src.util.variable_generator import VariableGenerator, PredicateGenerator, \
    PermutationGenerator

VARIABLE_PREDICATE_NAME = "A"

META_PREDICATE_TYPE_MATCH = re.compile("\\$([A-Z][a-zA-Z0-9_-]*)"
                                       "(/([0-9]|[1-9][0-9]+))?"
                                       "(\\[([0-9]+)\\](\\[(.+)\\])?)?")

logger = logging.getLogger(__name__)


def check_if_predicates_unify(predicate, goal, unify_constant_predicate=True):
    """
    Tries to unify `predicate` with `goal`, if possible. If it is possible,
    returns `True`; otherwise, returns `False`.

    :param predicate: the predicate
    :type predicate: Predicate
    :param goal: the goal
    :type goal: Predicate
    :param unify_constant_predicate: if `True`, unifies the clauses to constant
    predicate goals; otherwise, only unifies the clauses to variable predicate
    goals.
    :type unify_constant_predicate: bool
    :return: `True`, if it is possible to unify the atom with the goal;
    otherwise, `False`
    :rtype: bool
    """
    if predicate == goal:
        return True
    if predicate.arity != goal.arity:
        # IMPROVE: Maybe it would be beneficial to generalize in order to
        #  unify predicates of different arities.
        return False
    if isinstance(get_term_from_string(predicate.name), Variable):
        return unify_constant_predicate or \
               isinstance(get_term_from_string(goal.name), Variable)


E = TypeVar('E')


class SLDNode(Generic[E]):
    """
    Represents a node of a SLD-Resolution tree.
    """

    def __init__(self, values, applied_clause=None, parent=None):
        """
        Creates a SLD node.

        :param values: the values of the node
        :type values: Iterable[Atom]
        :param applied_clause: the clause applied to the node
        :type applied_clause: HornClause
        :param parent: the parent node
        :type parent: SLDNode
        """
        self.values: Tuple[Atom] = tuple(values)
        self.applied_clause: Optional[HornClause] = applied_clause
        self.parent: Optional[SLDNode] = parent
        self.children: Set[SLDNode] = OrderedSet()
        if parent is not None:
            parent.children.add(self)
        self._avoiding_terms = self._get_used_variables()

    def _get_used_variables(self):
        """
        Gets the variables used by the values of the node.

        :return: the set of variables used by the values
        :rtype: Set[Term]
        """
        used_variables = set()
        for atom in self.values:
            predicate_name = get_term_from_string(atom.predicate.name)
            if isinstance(predicate_name, Variable):
                used_variables.add(predicate_name)
            for term in atom.terms:
                if isinstance(term, Variable):
                    used_variables.add(term)

        return used_variables

    @property
    def avoiding_terms(self):
        """
        The terms to avoid when renaming variable terms.

        :return:
        :rtype:
        """
        avoid_terms = set(self._avoiding_terms)
        if self.parent is not None:
            avoid_terms.update(self.parent.avoiding_terms)
        return avoid_terms

    def __hash__(self):
        return hash((self.parent, self.values))

    def __eq__(self, other):
        return id(self) == id(other)

    def __repr__(self):
        if self.parent is None:
            return f"{self.values} -> {self.children}"
        else:
            return f"{self.parent.values} -> {self.values} -> {self.children}"


def extract_meta_program(node: SLDNode) -> Deque[HornClause]:
    """
    Extracts a meta program from a node of the SLD-Resolution tree.

    :param node: the node of the SLD-Resolution tree
    :type node: SLDNode
    :return: the meta program
    :rtype: Deque[HornClause]
    """
    meta_program: deque[HornClause] = deque()
    while node is not None and node.applied_clause is not None:
        meta_program.appendleft(node.applied_clause)
        node = node.parent
    return meta_program


def replace_variable_terms(terms, variables, arities):
    """
    Replace the variable terms that unifies to predicates.

    :param terms: the terms
    :type terms: Iterable[Term]
    :param variables: the variable substitution map
    :type variables: Dict[Term, str]
    :param arities: the arity per variable substitution
    :type arities: Dict[Term, str]
    :return: the substituted terms
    :rtype: List[Term]
    """
    new_terms = []
    for term in terms:
        if isinstance(term, Quote):
            # The term is quoted, can be a template to extract a
            # attribute of the logic program
            match = META_PREDICATE_TYPE_MATCH.fullmatch(term.value)
            if match is not None:
                fields = match.groups()
                variable = Variable(fields[0])
                arity = fields[2]
                argument = fields[4]
                constant = fields[6]
                new_term = term.quote
                new_term += "$"
                new_term += variables[variable]
                new_term += "/"
                if arity is None:
                    new_term += str(arities[variable])
                else:
                    new_term += str(arity)
                if argument is not None:
                    new_term += "["
                    new_term += argument
                    new_term += "]"
                    if constant is not None:
                        new_term += "["
                        new_term += constant
                        new_term += "]"
                new_term += term.quote
                term = Quote(new_term)
        elif isinstance(term, Variable):
            # The term is a variable, that might be a predicate
            predicate = variables.get(term, None)
            if predicate is not None:
                arity = arities.get(term, -1)
                term = Quote(f"\"{predicate}/{arity}\"")
        new_terms.append(term)
    return new_terms


class MetaProgram:
    """
    Represents a meta program.
    """

    def __init__(self, meta_clauses, trainable_predicates=()):
        """
        Creates the meta program.

        :param meta_clauses: a set of meta clauses
        :type meta_clauses: Iterable[HornClause]
        :param trainable_predicates: a set of trainable predicates
        :type trainable_predicates: Iterable[Predicate or str]
        """
        self.head_predicates: Set[Predicate] = set()
        """
        Holds the variable predicates found in the head. These are the 
        predicates the MUST be invented. 
        """

        self.body_predicates: Set[Predicate] = set()
        """
        Holds the variable predicates found in the body. These are the 
        predicates that can either be invented, if they are trainable; or 
        must already exist in the knowledge base.
        """

        self.trainable_predicate: Set[Union[str, Predicate]] = \
            set(trainable_predicates)
        """
        Holds the trainable predicates, for fixed arity predicates; or the
        predicates' names, for any arity predicate.
        """

        self.builtin_facts: Set[AtomClause] = OrderedSet()
        """
        Represents the builtin facts from the literals found in the body of the
        clauses.
        """

        self.clauses: Set[HornClause] = OrderedSet()
        "The clauses of the meta program, without the builtin literals."

        self._create_meta_program(meta_clauses)

    def _create_meta_program(self, meta_clauses):
        """
        Creates the meta program.

        :param meta_clauses: a set of meta clauses
        :type meta_clauses: Iterable[HornClause]
        """
        for clause in meta_clauses:
            body_variable_predicates = set()
            body = []
            for literal in clause.body:
                predicate = literal.predicate
                if predicate.name in NeuralLogProgram.BUILTIN_PREDICATES:
                    self.builtin_facts.add(AtomClause(literal))
                    if predicate.name == \
                            NeuralLogProgram.LEARN_BUILTIN_PREDICATE:
                        predicate = get_predicate_from_string(
                            literal.terms[0].get_name())
                        if predicate.arity < 0:
                            self.trainable_predicate.add(predicate.name)
                        else:
                            self.trainable_predicate.add(predicate)
                else:
                    body.append(literal)
                    if isinstance(
                            get_term_from_string(predicate.name), Variable):
                        body_variable_predicates.add(predicate)

            predicate = clause.head.predicate
            predicate_term = get_term_from_string(predicate.name)
            if isinstance(predicate_term, Variable) and \
                    predicate not in body_variable_predicates:
                self.head_predicates.add(predicate)
            self.body_predicates.update(body_variable_predicates)
            self.clauses.add(
                HornClause(clause.head, *body, provenance=clause.provenance))

        self.body_predicates.difference_update(self.head_predicates)

    def apply_substitution(self, substitution):
        """
        Applies the substitution to the meta program and returns a new
        program. If the substitution bind all the higher-order terms to
        first-order terms, the resulting program would be a first-order program.

        :param substitution: the substitutions
        :type substitution: Dict[Predicate, str]
        :return: the resulting program
        :rtype: Sequence[Clause]
        """
        variables = map(
            lambda x: (get_term_from_string(x[0].name), x[0].arity, x[1]),
            substitution.items())
        variables = \
            list(filter(lambda x: isinstance(x[0], Variable), variables))
        arities = dict(map(lambda x: (x[0], x[1]), variables))
        variables = dict(map(lambda x: (x[0], x[2]), variables))

        result_program = OrderedSet()
        for clause in self.clauses:
            predicate = clause.head.predicate
            new_predicate = substitution.get(predicate, predicate)
            new_terms = filter(lambda x: x not in variables, clause.head.terms)
            new_head = Atom(new_predicate, *new_terms)

            new_body = []
            for literal in clause.body:
                predicate = literal.predicate
                new_predicate = substitution.get(predicate, predicate)
                terms = filter(lambda x: x not in variables, literal.terms)
                new_terms = replace_variable_terms(terms, variables, arities)
                atom = Atom(new_predicate, *new_terms)
                # Filters True Atoms, since it does not make difference in
                # the body of the rule
                if atom == NeuralLogProgram.TRUE_ATOM:
                    continue
                new_body.append(Literal(atom, negated=literal.negated))
            result_program.add(
                HornClause(new_head, *new_body, provenance=clause.provenance))

        for clause in self.builtin_facts:
            predicate = clause.atom.predicate
            new_predicate = substitution.get(predicate, predicate)
            terms = clause.atom.terms
            new_terms = replace_variable_terms(terms, variables, arities)
            result_program.add(
                AtomClause(Atom(new_predicate, *new_terms,
                                provenance=clause.provenance)))

        return result_program

    # noinspection DuplicatedCode
    def first_order_programs(self, logic_predicates, predicate_generator):
        """
        Yields all possible first-order program generated by the meta program.

        :param logic_predicates: the possible logic predicates
        :type logic_predicates: Iterable[Predicate]
        :param predicate_generator: a predicate name generator
        :type predicate_generator: Iterator[str]
        :return: All possible programs generated by the meta program.
        :rtype: Generator[Sequence[Clause]]
        """
        variable_terms = []
        permutation_terms = []
        fixed_terms = []
        for predicate in self.head_predicates:
            variable_terms.append(predicate)
            fixed_term = next(predicate_generator)
            fixed_terms.append(fixed_term)
            permutation_terms.append([fixed_term])

        unique_for_body = OrderedSet()
        for predicate in self.body_predicates:
            variable_terms.append(predicate)
            permutations = OrderedSet()
            if predicate in self.trainable_predicate or \
                    predicate.name in self.trainable_predicate:
                element = next(predicate_generator)
                permutations.add(element)
                unique_for_body.add(element)
            for logic_predicate in logic_predicates:
                if predicate.arity == logic_predicate.arity:
                    permutations.add(logic_predicate.name)
            permutations.update(fixed_terms)
            permutation_terms.append(permutations)

        pg = PermutationGenerator(variable_terms, permutation_terms)
        for substitution in pg:
            yield self.apply_substitution(substitution)

    def __repr__(self):
        message = "Higher-Order Program:\n"
        for clause in self.clauses:
            message += str(clause)
            message += "\n"
        message += "\n"
        if self.builtin_facts:
            message += "Builtin Predicates:\n"
            for builtin in self.builtin_facts:
                message += str(builtin)
                message += "\n"
            message += "\n"
        message += f"Head predicates:\t{self.head_predicates}\n"
        message += f"Body predicates:\t{self.body_predicates}"

        return message


def apply_clause_to_node(node, clause, unify_constant_predicate=True):
    """
    Applies the meta `clause` to the `node` of the SLD-Resolution tree.

    :param node: the node of the SLD-Resolution tree
    :type node: SLDNode
    :param clause: the meta clause
    :type clause: HornClause
    :param unify_constant_predicate: if `True`, unifies the clauses to
    constant predicate goals; otherwise, only unifies the clauses to
    variable predicate goals.
    :type unify_constant_predicate: bool
    :return: the list of resulting nodes of the application of the clause
    to the node
    :rtype: Generator[SLDNode]
    """
    avoiding_terms = node.avoiding_terms

    for i in range(len(node.values)):
        goal = node.values[i]
        variable_generator = VariableGenerator(avoiding_terms)
        if not check_if_predicates_unify(
                clause.head.predicate, goal.predicate,
                unify_constant_predicate):
            continue
        head = Atom(goal.predicate, *clause.head.terms)
        substitution = get_unify_map(head, goal)
        if substitution is None:
            continue
        substitution[clause.head.predicate] = goal.predicate
        substitution[get_term_from_string(clause.head.predicate.name)] = \
            get_term_from_string(goal.predicate.name)

        renamed_body = []
        for literal in clause.body:
            predicate = literal.predicate
            renamed_predicate = substitution.get(predicate)
            if renamed_predicate is None:
                predicate_name = get_term_from_string(predicate.name)
                if isinstance(predicate_name, Variable) and \
                        predicate_name in avoiding_terms:
                    new_term = next(variable_generator)
                    renamed_predicate = Predicate(new_term, predicate.arity)
                    # avoiding_terms.add(new_term)
                else:
                    renamed_predicate = predicate
                substitution[predicate] = renamed_predicate
                substitution[get_term_from_string(predicate.name)] = \
                    get_term_from_string(renamed_predicate.name)

            renamed_terms = []
            for term in literal.terms:
                renamed_term = substitution.get(term)
                if renamed_term is None:
                    if isinstance(term,
                                  Variable) and term in avoiding_terms:
                        renamed_term = next(variable_generator)
                    else:
                        renamed_term = term
                    substitution[term] = renamed_term
                renamed_terms.append(renamed_term)
            renamed_literal = Literal(
                Atom(renamed_predicate, *renamed_terms),
                negated=literal.negated)
            renamed_body.append(renamed_literal)

        renamed_terms = []
        for term in head.terms:
            renamed_terms.append(substitution[term])
        renamed_head = Atom(goal.predicate, *renamed_terms)

        renamed_clause = HornClause(renamed_head, *renamed_body)
        new_values = \
            node.values[:i] + tuple(renamed_body) + node.values[i + 1:]
        new_node = SLDNode(new_values, renamed_clause, node)
        yield new_node


class MetaRevisionOperator(ro.RevisionOperator):
    """
    Revision operator that proposes revision based on a (meta) Higher-Order
    Logic Program.
    """

    OPTIONAL_FIELDS = dict(ro.RevisionOperator.OPTIONAL_FIELDS)
    OPTIONAL_FIELDS.update({
        "maximum_depth": 0,
        "tree_theory": None
    })

    def __init__(self, learning_system=None, theory_metric=None,
                 clause_modifiers=None, meta_program=None, maximum_depth=None,
                 tree_theory=None):
        super().__init__(learning_system, theory_metric, clause_modifiers)

        self.meta_program: str = meta_program
        "The higher-order logic program (as string)."

        self.meta_clauses: List[HornClause] = self._read_program()

        self.maximum_depth = maximum_depth
        "The maximum depth in order to apply the meta program."

        if self.maximum_depth is None:
            self.maximum_depth = self.OPTIONAL_FIELDS["maximum_depth"]

        self.tree_theory: Optional[TreeTheory] = tree_theory
        "The tree theory, if any."

        self.revised_clause: Optional[HornClause] = None
        self.removed_item: Union[HornClause, Literal, None] = None

    def _read_program(self):
        """
        Reads the meta program.

        :return: the list of meta clauses
        :rtype: List[HornClause]
        """
        lexer = NeuralLogLexer()
        parser = NeuralLogParser(lexer)
        parser.parser.parse(input=self.meta_program, lexer=lexer)
        parser.expand_placeholders()
        # noinspection PyTypeChecker
        return parser.get_clauses()

    # noinspection PyMissingOrEmptyDocstring
    def required_fields(self):
        return super().required_fields() + ["meta_program"]

    # noinspection PyMissingOrEmptyDocstring,PyAttributeOutsideInit
    def initialize(self):
        super().initialize()
        self.meta_clauses: List[HornClause] = self._read_program()
        if self.tree_theory is None:
            self.perform_operation = self.perform_operation_on_examples
        else:
            self.perform_operation = self.perform_operation_on_tree
        self.revised_clause = None
        self.removed_item = None

    def _update_logic_predicates(self, theory):
        """
        Updates the set of possible existing predicates on the learning system.

        :param theory: the current theory
        :type theory: NeuralLogProgram
        """
        self._logic_predicates = set()
        knowledge_base = self.learning_system.knowledge_base
        self._logic_predicates.update(
            knowledge_base.logic_predicates,
            theory.logic_predicates)

        self._generic_trainable_predicates = set()
        # The code below may cause the number of parameters to grow quickly,
        # by inventing trainable predicates with high arity
        # predicates = \
        #     filter(lambda x: x.arity < 0,
        #            self.learning_system.knowledge_base.trainable_predicates)
        # self._generic_trainable_predicates.update(
        #     map(lambda x: x.name, predicates))

        self._avoid_predicates = set()
        self._append_predicates_to_avoid(knowledge_base, self._avoid_predicates)
        self._append_predicates_to_avoid(theory, self._avoid_predicates)

    @staticmethod
    def _append_predicates_to_avoid(program, avoid_predicates):
        """
        Appends the predicate, from program, that must be avoided.

        :param program: the program
        :type program: NeuralLogProgram
        :param avoid_predicates: the set into which to append the predicates
        :type avoid_predicates: Set[str]
        """
        for predicate in program.logic_predicates:
            avoid_predicates.add(predicate.name)
        for predicate in program.functional_predicates:
            avoid_predicates.add(predicate.name)
        for predicate in program.trainable_predicates:
            avoid_predicates.add(predicate.name)

    # noinspection PyMissingOrEmptyDocstring
    def perform_operation(self, targets, minimum_threshold=None):
        # Placeholder for the `perform_operation` function.
        # The real implementation will be either the `perform_operation_on_tree`
        # or the `perform_operation_on_example`.
        pass

    @staticmethod
    def _build_target_atom(revision_leaf):
        """
        Builds the target atom to apply the meta program, based on the revision
        leaf.

        If the last literal of the revision leaf is propositional or is
        connected to the output variable of the head of the clause, returns
        it as the target atom. Otherwise, returns a variable target atom
        connecting the output term of the last predicate to the output variable
        of the head of the clause.

        :param revision_leaf: the revision leaf
        :type revision_leaf: Node[HornClause]
        :return: the target atom
        :rtype: Atom
        """
        if revision_leaf.is_default_child:
            horn_clause = revision_leaf.parent.element
        else:
            horn_clause = revision_leaf.element
        last_literal = horn_clause.body[-1]
        output_term = horn_clause.head.terms[-1]
        arity = last_literal.arity()
        if arity == 0 or output_term in last_literal.terms:
            # The target atom is propositional or it connects to the
            # output of the rule, it will be the target
            target_atom = last_literal
        elif arity != 2:
            # The target atom does not connect to the output of the rule,
            # the target will be a generic atom connecting the output of
            # the last atom to the output of the rule
            target_atom = Atom(
                VARIABLE_PREDICATE_NAME, last_literal.terms[-1], output_term)
        else:
            # The target atom does not connect to the output of the rule,
            # the target should be a generic atom connecting the output of
            # the last atom to the output of the rule. However, we need to
            # analise the rule graph in order to determine the output of the
            # last literal, which will be the input of the target
            rule_path_finder = RulePathFinder(horn_clause)
            rule_graph: RuleGraph = rule_path_finder.find_clause_paths(-1)
            # We should always be able to find the input of the target,
            # however, we will assume that the last term is the input,
            # if the loop below does not found it
            input_term = last_literal.terms[-1]
            for edge in rule_graph.edges:
                if edge.literal.arity() != 2 or \
                        output_term != edge.get_output_term():
                    continue
                possible_input = edge.get_input_terms()[0]
                if possible_input in last_literal.terms:
                    input_term = possible_input
                    break
            target_atom = Atom(VARIABLE_PREDICATE_NAME, input_term, output_term)
        return target_atom

    def revise_node(self, target_atom, targets, minimum_threshold,
                    extract_theory_function):
        """
        Revises a literal node of the TreeTheory. It generates new rules,
        based on the literal and appends the body of the generated rule to the
        theory.

        :param target_atom: the target atom to base the revision.
        :type target_atom: Atom
        :param targets: the target examples
        :type targets: Examples
        :param minimum_threshold: a minimum threshold to consider by the
        operator. If set, the first program which improves the current theory
        above the threshold is returned. If not set, the best found program is
        returned
        :type minimum_threshold: Optional[float]
        :param extract_theory_function: a function to extract the theory, based
        on the first clause of the found program
        :type extract_theory_function: function
        :return: the revised theory
        :rtype: NeuralLogProgram or None
        """
        revision_leaf = self.tree_theory.get_revision_leaf()
        logger.debug("Revising\t%s with target\t%s", revision_leaf, target_atom)
        inferred_examples = self.learning_system.infer_examples(targets)
        current_evaluation = self.theory_metric.compute_metric(
            targets, inferred_examples)

        clean_predicate_generator = \
            PredicateGenerator(avoid_terms=self._avoid_predicates)
        best_theory = None
        best_evaluation = current_evaluation
        for node in self.apply_higher_order_program(target_atom):
            predicate_generator = clean_predicate_generator.clean_copy()
            meta_clauses = extract_meta_program(node)
            meta_program = \
                MetaProgram(meta_clauses, self._generic_trainable_predicates)
            logger.debug("Using meta-program:\n%s", meta_program)
            first_order_programs = meta_program.first_order_programs(
                self._logic_predicates, predicate_generator)
            for program in first_order_programs:
                # IMPROVE: Use a clause evaluation class to get performance
                #  metrics and to allow timeout.
                if not program or not isinstance(program[0], HornClause):
                    continue
                # noinspection PyTypeChecker

                current_theory = extract_theory_function(
                    program, revision_leaf, targets)
                if current_theory is None:
                    continue
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Evaluating theory modified by program:\n%s",
                        "\n".join(map(lambda x: str(x), program)))
                evaluation = \
                    self.theory_evaluator.evaluate_theory(
                        targets, self.theory_metric, current_theory)
                improvement = \
                    self.theory_metric.compare(evaluation, best_evaluation)
                if minimum_threshold is None:
                    if improvement > 0.0:
                        best_evaluation = evaluation
                        best_theory = current_theory
                else:
                    if improvement > minimum_threshold:
                        return current_theory

        return best_theory

    # noinspection PyUnusedLocal
    def _build_root_node_theory(self, program, revision_leaf, targets):
        """
        Builds a theory, from the found program, in order to revise the current
        theory from a false leaf node.

        :param program: the found logic program
        :type program: Sequence[Clause]
        :param revision_leaf: the revision leaf
        :type revision_leaf: Node[HornClause]
        :param targets: the target examples
        :type targets: Examples
        :return: the revised theory
        :rtype: NeuralLogProgram or None
        """
        # noinspection PyTypeChecker
        first_clause: HornClause = program[0]
        # filter programs whose added clause is false
        if FALSE_LITERAL in first_clause.body:
            return None

        new_clause = self.apply_clause_modifiers(first_clause, targets)
        self.revised_clause = new_clause

        current_theory = self.learning_system.theory.copy()
        current_theory.add_clauses([new_clause])
        current_theory.add_clauses(program[1:])
        return current_theory

    def _build_false_leaf_theory(self, program, revision_leaf, targets):
        """
        Builds a theory, from the found program, in order to revise the current
        theory from a false leaf node.

        :param program: the found logic program
        :type program: Sequence[Clause]
        :param revision_leaf: the revision leaf
        :type revision_leaf: Node[HornClause]
        :param targets: the target examples
        :type targets: Examples
        :return: the revised theory
        :rtype: NeuralLogProgram or None
        """
        # noinspection PyTypeChecker
        first_clause: HornClause = program[0]
        # filter programs whose added clause is false
        if FALSE_LITERAL in first_clause.body:
            return None

        if VARIABLE_PREDICATE_NAME in \
                map(lambda x: x.predicate.name, first_clause.body):
            # This program attempted to use a placeholder predicate, skip it
            return None

        new_clause = copy.deepcopy(revision_leaf.parent.element)
        new_clause.body.extend(first_clause.body)
        new_clause = self.apply_clause_modifiers(new_clause, targets)
        self.revised_clause = new_clause

        current_theory = self.learning_system.theory.copy()
        current_theory.add_clauses([new_clause])
        current_theory.add_clauses(program[1:])
        return current_theory

    def _build_literal_leaf_theory(self, program, revision_leaf, targets):
        """
        Builds a theory, from the found program, in order to revise the current
        theory from a literal leaf node.

        :param program: the found logic program
        :type program: Sequence[Clause]
        :param revision_leaf: the revision leaf
        :type revision_leaf: Node[HornClause]
        :param targets: the target examples
        :type targets: Examples
        :return: the revised theory
        :rtype: NeuralLogProgram or None
        """
        # noinspection PyTypeChecker
        first_clause: HornClause = program[0]
        if VARIABLE_PREDICATE_NAME in \
                map(lambda x: x.predicate.name, first_clause.body):
            # This program attempted to use a placeholder predicate, skip it
            return None

        original_clause: HornClause = revision_leaf.parent.element
        current_theory = self.learning_system.theory.copy()
        current_theory.clauses_by_predicate[
            original_clause.head.predicate].remove(original_clause)
        if FALSE_LITERAL in first_clause.body:
            # the program added a false literal to the clause, just remove
            # the clause
            self.removed_item = original_clause
            self.revised_clause = original_clause
        else:
            # the program appends predicates to the clause
            new_clause = copy.deepcopy(original_clause)
            if first_clause.head.predicate.name != VARIABLE_PREDICATE_NAME:
                # the program replaces a literal from the clause by a
                # non-false, possibly empty, set of literals
                new_clause.body.remove()
                removed_literal = Literal(first_clause.head)
                new_clause.body.remove(removed_literal)
                new_clause.body.remove()
                self.removed_item = removed_literal
            new_clause.body.extend(first_clause.body)
            new_clause = self.apply_clause_modifiers(new_clause, targets)
            self.revised_clause = new_clause
            current_theory.add_clauses([new_clause])
            current_theory.add_clauses(program[1:])

        return current_theory

    def perform_operation_on_tree(self, targets, minimum_threshold=None):
        """
        Performs the operation based on the tree theory.

        :param targets: the target examples
        :type targets: Examples
        :param minimum_threshold: a minimum threshold to consider by the
        operator. Implementations of this class could use this threshold in
        order to improve performance by skipping evaluating candidates
        :type minimum_threshold: Optional[float]
        :return: the revised theory
        :rtype: NeuralLogProgram or None
        """
        revision_leaf = self.tree_theory.get_revision_leaf()
        self.revised_clause = None
        self.removed_item = None
        logger.debug("Trying to revise rule:\t%s", revision_leaf)
        if revision_leaf.is_root:
            # This is the root node
            target_atom = Atom(self.tree_theory.get_target_predicate(),
                               *revision_leaf.element.head.terms)
            return self.revise_node(target_atom, targets, minimum_threshold,
                                    self._build_root_node_theory)
        else:
            target_atom = self._build_target_atom(revision_leaf)
            if revision_leaf.is_default_child:
                # This node represents a false leaf, it is a rule creation in
                # the parent node
                return self.revise_node(target_atom, targets, minimum_threshold,
                                        self._build_false_leaf_theory)
            else:
                # This node represents a rule, it is a literal addition
                # operation
                return self.revise_node(target_atom, targets, minimum_threshold,
                                        self._build_literal_leaf_theory)

    def perform_operation_on_examples(self, targets, minimum_threshold=None):
        """
        Performs the operation based on the examples.

        :param targets: the target examples
        :type targets: Examples
        :param minimum_threshold: a minimum threshold to consider by the
        operator. Implementations of this class could use this threshold in
        order to improve performance by skipping evaluating candidates
        :type minimum_threshold: Optional[float]
        :return: the revised theory
        :rtype: NeuralLogProgram or None
        """
        try:
            logger.info("Performing operation on\t%d examples.", targets.size())
            theory = self.learning_system.theory.copy()
            inferred_examples = None
            current_evaluation = None
            self._update_logic_predicates(theory)
            for example in ExampleIterator(targets):
                if inferred_examples is None:
                    inferred_examples = self.learning_system.infer_examples(
                        targets, theory)
                    current_evaluation = self.theory_metric.compute_metric(
                        targets, inferred_examples)
                if not ro.is_positive(example) or \
                        inferred_examples.contains_example(example):
                    # The inferred examples contains only the examples whose
                    # weight is greater than the null weight
                    if ro.is_positive(example):
                        logger.debug("Skipping covered example:\t%s", example)
                    # It skips negatives or covered positive examples
                    continue
                target = get_variable_atom(example.predicate)
                updated = self.perform_operation_for_example(
                    target, theory, targets,
                    inferred_examples, current_evaluation, minimum_threshold)
                if updated:
                    inferred_examples = None
                    current_evaluation = None
                    self._update_logic_predicates(theory)
            return theory
        except KnowledgeException as e:
            raise TheoryRevisionException("Error when revising the theory.", e)

    def perform_operation_for_example(
            self, example, theory, targets,
            inferred_examples, current_evaluation, minimum_threshold):
        """
        Performs the operation for a single examples.

        :param example: the example
        :type example: Atom
        :param theory: the theory
        :type theory: NeuralLogProgram
        :param targets: the other examples
        :type targets: Examples
        :param inferred_examples: the inferred value for the examples
        :type inferred_examples: ExamplesInferences
        :param current_evaluation: the current evaluation of the theory on
        the targets
        :type current_evaluation: float
        :param minimum_threshold: a minimum threshold to consider by the
        operator. Implementations of this class could use this threshold in
        order to improve performance by skipping evaluating candidates
        :type minimum_threshold: Optional[float]
        :return: `True`, if the theory has changed.
        :rtype: bool
        """
        try:
            positive = ro.is_positive(example)
            if not positive or inferred_examples.contains_example(example):
                # The inferred examples contains only the examples whose
                # weight is greater than the null weight
                if positive:
                    logger.debug("Skipping covered example:\t%s", example)
                # It skips negatives or covered positive examples
                return

            logger.debug("Building clause from the example:\t%s", example)
            program = self.build_program_from_target(
                example, targets, current_evaluation, minimum_threshold)
            # horn_clause = self.apply_clause_modifiers(horn_clause, targets)
            if program:
                for horn_clause in program:
                    horn_clause.provenance = ro.LearnedClause(str(self))
                theory.add_clauses(program)
                theory.build_program()
                logger.info("Program appended to the theory:\n%s",
                            "\n".join(map(lambda x: str(x), program)))
                return True
        except (KnowledgeException, InitializationException):
            logger.exception("Error when revising the example, reason:")
        return False

    def build_program_from_target(self, target_atom, examples,
                                  current_evaluation, minimum_threshold=None):
        """
        Builds the a program from the example.

        :param target_atom: the target atom
        :type target_atom: Atom
        :param examples: the examples to evaluate the program
        :type examples: Examples
        :param current_evaluation: the current evaluation of the theory on
        the targets
        :type current_evaluation: float
        :param minimum_threshold: the minimum threshold to early stop the
        evaluation of candidate programs
        :type minimum_threshold: Optional[float]
        :return: if `minimum_threshold` is None, returns the best evaluated
        program found, from the example; otherwise, returns the first program
        whose the improvement over the current theory is bigger than the
        minimum threshold
        :rtype: Optional[Sequence[Clause]]
        """
        clean_predicate_generator = \
            PredicateGenerator(avoid_terms=self._avoid_predicates)
        best_program = None
        best_evaluation = current_evaluation
        for node in self.apply_higher_order_program(target_atom):
            predicate_generator = clean_predicate_generator.clean_copy()
            meta_clauses = extract_meta_program(node)
            meta_program = \
                MetaProgram(meta_clauses, self._generic_trainable_predicates)
            logger.debug("Using meta-program:\n%s", meta_program)
            first_order_programs = meta_program.first_order_programs(
                self._logic_predicates, predicate_generator)
            for program in first_order_programs:
                # IMPROVE: Use a clause evaluation class to get performance
                #  metrics and to allow timeout.
                if not program or not isinstance(program[0], HornClause):
                    continue
                current_program = OrderedSet()
                # noinspection PyTypeChecker
                current_program.add(
                    self.apply_clause_modifiers(program[0], examples))
                current_program.update(program[1:])
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Evaluating appending the program:\n%s",
                        "\n".join(map(lambda x: str(x), current_program)))
                evaluation = \
                    self.theory_evaluator.evaluate_theory_appending_clause(
                        examples, self.theory_metric, current_program)
                improvement = \
                    self.theory_metric.compare(evaluation, best_evaluation)
                if minimum_threshold is None:
                    if improvement > 0.0:
                        best_evaluation = evaluation
                        best_program = current_program
                else:
                    if improvement > minimum_threshold:
                        return current_program

        return best_program

    def apply_higher_order_program(self, target_atom):
        """
        Applies the higher-order clauses to the target atom and returns a set
        of programs.

        :param target_atom: the target atom
        :type target_atom: Atom
        :return: a set of SLD nodes leaves of a SLD tree
        :rtype: Generator[SLDNode]
        """
        queue: deque[SLDNode] = deque()
        "Holds all the nodes at the deepest level"

        root = SLDNode([copy.deepcopy(target_atom)])  # The root (level 0)
        for clause in self.meta_clauses:
            for node in apply_clause_to_node(root, clause, True):
                queue.append(node)
                yield node
        for _ in range(self.maximum_depth - 1):
            size = len(queue)
            for __ in range(size):
                current_node = queue.popleft()
                for clause in self.meta_clauses:
                    for node in apply_clause_to_node(
                            current_node, clause, False):
                        queue.append(node)
                        yield node

    # noinspection PyMissingOrEmptyDocstring,DuplicatedCode
    def theory_revision_accepted(self, revised_theory, examples):
        if self.revised_clause is None:
            return

        # Removes the used examples
        revision_leaf = self.tree_theory.get_revision_leaf()
        for predicate in examples:
            self.tree_theory.remove_example_from_leaf(predicate, revision_leaf)

        # Removes the removed parts of the theory, if any
        if isinstance(self.removed_item, Literal):
            # Remove Literal case
            self.tree_theory.remove_literal_from_tree(revision_leaf)
        if isinstance(self.removed_item, HornClause):
            # Remove Rule case
            self.tree_theory.remove_rule_from_tree(revision_leaf)

        # Adds the added parts to the theory, if any
        add_clause_to_tree(self.revised_clause, revision_leaf)

#
# def revise_root_node(self, targets, minimum_threshold):
#     """
#     Revises the root node of the TreeTheory. It generates new rules to be
#     appended to the theory.
#
#     :param targets: the target examples
#     :type targets: Examples
#     :param minimum_threshold: a minimum threshold to consider by the
#     operator. If set, the first program which improves the current theory
#     above the threshold is returned. If not set, the best found program is
#     returned
#     :type minimum_threshold: Optional[float]
#     :return: the revised theory
#     :rtype: NeuralLogProgram or None
#     """
#     revision_leaf = self.tree_theory.get_revision_leaf()
#     target_atom = Atom(self.tree_theory.get_target_predicate(),
#                        *revision_leaf.element.head.terms)
#     inferred_examples = self.learning_system.infer_examples(targets)
#     current_evaluation = self.theory_metric.compute_metric(
#         targets, inferred_examples)
#     clean_predicate_generator = \
#         PredicateGenerator(avoid_terms=self._avoid_predicates)
#     best_theory = None
#     best_evaluation = current_evaluation
#     for node in self.apply_higher_order_program(target_atom):
#         predicate_generator = clean_predicate_generator.clean_copy()
#         meta_clauses = extract_meta_program(node)
#         meta_program = \
#             MetaProgram(meta_clauses, self._generic_trainable_predicates)
#         first_order_programs = meta_program.first_order_programs(
#             self._logic_predicates, predicate_generator)
#         for program in first_order_programs:
#             if not program or not isinstance(program[0], HornClause):
#                 continue
#             # noinspection PyTypeChecker
#             first_clause: HornClause = program[0]
#             # filter programs whose added clause is false
#             if FALSE_LITERAL in first_clause.body:
#                 continue
#
#             current_theory = self._build_root_node_theory(
#                 first_clause, revision_leaf, targets)
#             current_theory.add_clauses(program[1:])
#
#             evaluation = \
#                 self.theory_evaluator.evaluate_theory(
#                     targets, self.theory_metric, current_theory)
#             improvement = \
#                 self.theory_metric.compare(evaluation, best_evaluation)
#             if minimum_threshold is None:
#                 if improvement > 0.0:
#                     best_evaluation = evaluation
#                     best_theory = current_theory
#             else:
#                 if improvement > minimum_threshold:
#                     return current_theory
#
#     return best_theory
#
# def revise_false_leaf(self, targets, minimum_threshold):
#     """
#     Revises a false node of the TreeTheory. It generates new rules,
#     from existing ones, to be appended to the theory.
#
#     :param targets: the target examples
#     :type targets: Examples
#     :param minimum_threshold: a minimum threshold to consider by the
#     operator. If set, the first program which improves the current theory
#     above the threshold is returned. If not set, the best found program is
#     returned
#     :type minimum_threshold: Optional[float]
#     :return: the revised theory
#     :rtype: NeuralLogProgram or None
#     """
#     revision_leaf = self.tree_theory.get_revision_leaf()
#     target_atom = self._build_target_atom(revision_leaf)
#     inferred_examples = self.learning_system.infer_examples(targets)
#     current_evaluation = self.theory_metric.compute_metric(
#         targets, inferred_examples)
#
#     clean_predicate_generator = \
#         PredicateGenerator(avoid_terms=self._avoid_predicates)
#     best_theory = None
#     best_evaluation = current_evaluation
#     for node in self.apply_higher_order_program(target_atom):
#         predicate_generator = clean_predicate_generator.clean_copy()
#         meta_clauses = extract_meta_program(node)
#         meta_program = \
#             MetaProgram(meta_clauses, self._generic_trainable_predicates)
#         first_order_programs = meta_program.first_order_programs(
#             self._logic_predicates, predicate_generator)
#         for program in first_order_programs:
#             if not program or not isinstance(program[0], HornClause):
#                 continue
#             # noinspection PyTypeChecker
#             first_clause: HornClause = program[0]
#             # filter programs whose added clause is false
#             if FALSE_LITERAL in first_clause.body:
#                 continue
#
#             if VARIABLE_PREDICATE_NAME in \
#                     map(lambda x: x.predicate.name, first_clause.body):
#                 # This program attempted to use a placeholder predicate,
#                 # skip it
#                 continue
#
#             current_theory = self._build_false_leaf_theory(
#                 first_clause, revision_leaf, targets)
#             current_theory.add_clauses(program[1:])
#
#             evaluation = \
#                 self.theory_evaluator.evaluate_theory(
#                     targets, self.theory_metric, current_theory)
#             improvement = \
#                 self.theory_metric.compare(evaluation, best_evaluation)
#             if minimum_threshold is None:
#                 if improvement > 0.0:
#                     best_evaluation = evaluation
#                     best_theory = current_theory
#             else:
#                 if improvement > minimum_threshold:
#                     return current_theory
#
#     return best_theory
