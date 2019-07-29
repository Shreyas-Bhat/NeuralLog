"""
Parses the Abstract Syntax Tree.
"""
import logging
from collections import deque

from antlr4 import ParserRuleContext
from antlr4.tree.Tree import TerminalNodeImpl

from language.language import *
# from language.parser.autogenerated.NeuralLogListener import NeuralLogListener
from language.parser.autogenerated.NeuralLogParser import NeuralLogParser

# QUOTE_SPLITTER = re.compile("{~[{}]+}")

logger = logging.getLogger()


class TooManyArguments(Exception):
    """
    Represents an exception raised by an atom with too many arguments.
    """

    MAX_NUMBER_OF_ARGUMENTS = 2

    def __init__(self, atom, found) -> None:
        """
        Creates a too many arguments exception.

        :param atom: the atom
        :type atom: NeuralLogParser.AtomContext
        :param found: the number of arguments found
        :type found: int
        """
        super().__init__("Too many arguments found for {} at line {}:{}."
                         " Found {} arguments, the maximum number of "
                         "arguments allows is {}."
                         .format(atom.getText(),
                                 atom.start.line, atom.start.column,
                                 found, self.MAX_NUMBER_OF_ARGUMENTS))


class UnsupportedUngroundedFact(Exception):
    """
    Represents an exception raised by an unsupported ungrounded fact.
    """

    def __init__(self, atom) -> None:
        """
        Creates an unsupported ungrounded fact exception.

        :param atom: the atom
        :type atom: NeuralLogParser.ClauseContext
        """
        super().__init__("Unsupported ungrounded fact found {} at line {}:{}."
                         " Facts must be grounded "
                         "(must contain only constants)."
                         .format(atom.getText(),
                                 atom.start.line, atom.start.column))


class UnsupportedTemplateFact(Exception):
    """
    Represents an exception raised by an unsupported template fact.
    """

    def __init__(self, atom) -> None:
        """
        Creates an unsupported template fact exception.

        :param atom: the atom
        :type atom: NeuralLogParser.ClauseContext
        """
        super().__init__("Unsupported template fact found {} at line {}:{}."
                         " Facts can not be templates."
                         .format(atom.getText(),
                                 atom.start.line, atom.start.column))


class BadTermException(Exception):
    """
    Represents an exception raised by a mal formed term.
    """

    def __init__(self, term, key, substitution) -> None:
        """
        Creates a bad term exception.

        :param term: the term
        :type term: str or Term
        :param key: the key
        :type key: str
        :param substitution: the substitution
        :type substitution: str
        """
        super().__init__("Bad term formed when replacing {key} by {sub} on "
                         "{term}.".format(term=term, key="{" + key + "}",
                                          sub=substitution))


class BadClauseException(Exception):
    """
    Represents an exception raised by a mal formed clause.
    """

    def __init__(self, clause) -> None:
        """
        Creates a bad clause exception.

        :param clause: the clause
        """
        super().__init__("Template only supported in Horn clauses. "
                         "Found {}".format(clause.__str__()))


class KeyDict(dict):
    """
    A dictionary to replace fields when formatting a string.
    If it is asked for a key that it does not have, it returns the keys
    surrounded by curly braces, as such, the place on the string remains the
    same.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def __getitem__(self, k):
        return self.get(k, "{" + k + "}")


def extract_value(string):
    """
    Tries to extract a number value from a string.

    :param string: the string
    :type string: str
    :return: the extracted value, if it success; otherwise, returns the string
    :rtype: str or float
    """
    try:
        value = float(string)
        return Number(value)
    except ValueError:
        return string


def solve_parts(parts, key, substitution):
    """
    Solve the place holders from the parts.

    :param parts: the parts
    :type parts: list[str]
    :param key: the name of the place holder
    :type key: str
    :param substitution: the value of the place holder
    :type substitution: str
    :return: the solved parts
    :rtype: list[str]
    """
    solved_terms = []
    join_terms = ""
    for part in parts:
        if part.startswith("{"):
            if key == part[1:-1]:
                join_terms += substitution
            else:
                if join_terms != "":
                    solved_terms.append(str(join_terms))
                    join_terms = ""
                solved_terms.append(part)
        else:
            join_terms += part
    if join_terms != "":
        solved_terms.append(str(join_terms))

    return solved_terms


def solve_place_holder_term(term, key, substitution):
    """
    Solve the place holder for the term.

    :param term: the term
    :type term: Term
    :param key: the name of the place holder
    :type key: str
    :param substitution: the value of the place holder
    :type substitution: str
    :return: the solved predicate
    :rtype: Term
    """
    if not term.is_template():
        return term
    if isinstance(term, Quote):
        return Quote(term.quote +
                     term.value.format_map(KeyDict({key: substitution})) +
                     term.quote)
    if isinstance(term, TemplateTerm):
        parts = solve_parts(term.parts, key, substitution)
        if len(parts) == 1:
            value = extract_value(parts[0])
            if isinstance(value, str):
                return get_term_from_string(value)
            return value
        else:
            return TemplateTerm(parts)
    raise BadTermException(term, key, substitution)


def solve_place_holder_predicate(predicate, key, substitution):
    """
    Solve the place holder for the predicate.

    :param predicate: the predicate
    :type predicate: Predicate or TemplatePredicate
    :param key: the name of the place holder
    :type key: str
    :param substitution: the value of the place holder
    :type substitution: str
    :return: the solved predicate
    :rtype: Predicate or TemplatePredicate
    """
    if not predicate.is_template():
        return predicate
    parts = solve_parts(predicate.parts, key, substitution)
    if len(parts) == 1:
        return Predicate(parts[0], predicate.arity)
    else:
        return TemplatePredicate(parts, predicate.arity)


def solve_literal(literal, key, substitution):
    """
    Solves the literal.

    :param literal: the literal
    :type literal: Literal
    :param key: the name of the place holder
    :type key: str
    :param substitution: the value of the place holder
    :type substitution: str
    :return: the solved literal
    :rtype: Literal
    """
    if not literal.is_template():
        return literal
    literal_pred = solve_place_holder_predicate(literal.predicate,
                                                key, substitution)
    literal_terms = []
    for term in literal.terms:
        literal_terms.append(solve_place_holder_term(term, key, substitution))

    return Literal(Atom(literal_pred, *literal_terms),
                   literal.negated, literal.trainable)


def solve_place_holder(clause, key, substitution):
    """
    Solve the place_holder specified by `key` in the clause, by replacing
    it by `substitution`.

    :param clause: the clause
    :type clause: HornClause
    :param key: the name of the place_holder
    :type key: str
    :param substitution: the substitution for the place_holder
    :type substitution: str
    :return: The new clause
    :rtype: HornClause
    """
    if clause.head.is_template():
        head_pred = solve_place_holder_predicate(clause.head.predicate,
                                                 key, substitution)
        head_terms = []
        for term in clause.head.terms:
            head_terms.append(solve_place_holder_term(term,
                                                      key, substitution))
        solved_head = Atom(head_pred, *head_terms)
    else:
        solved_head = clause.head
    solved_body = []
    for literal in clause.body:
        solved_body.append(solve_literal(literal, key, substitution))

    return HornClause(solved_head, *solved_body)


def solve_place_holders(clause, place_holders):
    """
    Generates a set of clause by replacing the template terms by the
    possible place holders in place_holders.
    :param clause: the clause
    :type clause: HornClause
    :param place_holders: the place holders
    :type place_holders: dict[str, set[str]]
    :return: the set of horn clauses
    :rtype: set[HornClause]
    """
    queue = deque([clause])
    for key, values in place_holders.items():
        size = len(queue)
        for _ in range(size):
            current = queue.popleft()
            for sub in values:
                solved_clause = solve_place_holder(current, key, sub)
                if solved_clause is not None:
                    queue.append(solved_clause)

    return set(queue)


class NeuralLogTransverse:
    """
    Transverse a NeuralLog Abstract Syntax Tree.
    """

    # Knowledge part
    # TODO: mark all position of predicates that contain a variable term
    # TODO: collect all the constants that appears on a variable position of
    #  a predicate, sort than and create a dictionary

    # Network part
    # TODO: collect all the predicates of the body of rules that does not
    #  appear in the head of any atom, they should be functions.

    # TODO: identify the predicates that has an real-valued term

    # TODO: create the matrix representation of the grounded data

    # TODO: create the neural network representation

    # TODO: update the knowledge part with the weights learned by the neural net

    def __init__(self):
        self.scope = KeyDict()
        self.depth = 0

        self.clauses = []
        self.predicates = set()
        self.constants = set()

    def __call__(self, node, *args, **kwargs):
        self.process_program(node)
        self.expand_placeholders()

    def process_program(self, node):
        """
        Process the program node from the Abstract Syntax Tree.

        :param node: the node
        :type node: ParserRuleContext
        """
        if isinstance(node, NeuralLogParser.ProgramContext):
            self.log("Program Node")
            for child in node.getChildren():
                self.process_program(child)
        elif isinstance(node, NeuralLogParser.ClauseContext):
            self.process_clause(node)
        elif isinstance(node, NeuralLogParser.For_loopContext):
            self.process_for_loop(node)

    def process_for_loop(self, node):
        """
        Process the for loop node.

        :param node: the for loop node
        :type node: NeuralLogParser.For_loopContext
        """
        self.log("ForLoop Node")
        # node.getChildren(0): "for"
        # node.getChildren(1): for_variable
        # node.getChildren(2): "in"
        # node.getChildren(3): for_terms|for_range
        # node.getChildren(4): "do"
        # node.getChildren(5): program
        # node.getChildren(6): "done"

        variable = node.getChild(1).getText()
        for_terms = self.get_for_range(node.getChild(3))
        self.log("for %s in %s do", variable, " ".join(for_terms))
        self.depth += 1
        for value in for_terms:
            self.scope[variable] = value
            self.process_program(node.getChild(5))
        self.scope.pop(variable, None)
        self.depth -= 1
        self.log("done")

    def process_clause(self, node):
        """
        Process the clause node.

        :param node: the clause node
        :type node: NeuralLogParser.ClauseContext
        :return:
        :rtype:
        """
        child = node.getChild(0)
        self.log("Clause Node")
        if isinstance(child, NeuralLogParser.Horn_clauseContext):
            clause = self.process_horn_clause(child)
            self.clauses.append(clause)
            for literal in clause.body:
                if not literal.predicate.is_template():
                    self.predicates.add(literal.predicate)
            predicate = clause.head.predicate
        else:
            if isinstance(child, NeuralLogParser.AtomContext):
                clause = AtomClause(self.process_atom(child))
            elif isinstance(child, NeuralLogParser.Weighted_atomContext):
                clause = AtomClause(self.process_weighted_atom(child))
            else:
                raise ClauseMalformedException()
            if not clause.is_grounded():
                raise UnsupportedUngroundedFact(node)
            if clause.is_template():
                raise UnsupportedTemplateFact(node)
            self.clauses.append(clause)
            predicate = clause.atom.predicate
        if not predicate.is_template() and not clause.is_template():
            self.predicates.add(predicate)
        clause.provenance = node

    def process_atom(self, atom):
        """
        Process the atom node.

        :param atom: the atom node
        :type atom: NeuralLogParser.AtomContext
        :return: the atom
        :rtype: Atom
        """
        self.log("Atom Node")
        predicate = self.process_predicate(atom.getChild(0))
        arguments = []
        if atom.getChildCount() > 1:
            arguments = self.process_list_of_arguments(atom.getChild(1))
        predicate.arity = len(arguments)
        if predicate.arity > 2:
            raise TooManyArguments(atom, predicate.arity)
        return Atom(predicate, *arguments)

    def process_weighted_atom(self, weighted_atom):
        """
        Process the weighted atom node.

        :param weighted_atom: the weighted atom node
        :type weighted_atom: NeuralLogParser.Weighted_atomContext
        :return: the weighted atom
        :rtype: Atom
        """
        self.log("Weighted Atom Node")
        weight = float(weighted_atom.getChild(0).getText())
        # weighted_atom.getChild(1).getText(): "::"
        atom = self.process_atom(weighted_atom.getChild(2))
        atom.weight = weight
        return atom

    def process_horn_clause(self, horn_clause):
        """
        Process the Horn clause node.

        :param horn_clause: the Horn clause node
        :type horn_clause: NeuralLogParser.Horn_clauseContext
        :return: the Horn clause
        :rtype: HornClause
        """

        self.log("Horn Clause Node")
        head = self.process_atom(horn_clause.getChild(0))
        body = []
        if horn_clause.getChildCount() > 2:
            body = self.process_body(horn_clause.getChild(2))
        return HornClause(head, *body)

    def process_predicate(self, predicate):
        """
        Process the predicate node.

        :param predicate: the Horn clause node
        :type predicate: NeuralLogParser.PredicateContext
        :return: the predicate
        :rtype: Predicate
        """
        self.log("Predicate Node")
        if predicate.getChildCount() == 1 and \
                predicate.getChild(0).getSymbol().type == NeuralLogParser.TERM:
            return Predicate(predicate.getChild(0).getText())
        else:
            solved_terms = self.solve_for_placeholders(predicate.getChildren())
            if len(solved_terms) == 1 and solved_terms[0][0] != "{":
                return Predicate(solved_terms[0])
            return TemplatePredicate(solved_terms)

    def process_list_of_arguments(self, list_of_arguments):
        """
        Process the list of arguments node.

        :param list_of_arguments: the list of arguments node
        :type list_of_arguments: NeuralLogParser.List_of_argumentsContext
        :return: the list of arguments
        :rtype: list[Term or float or int]
        """
        self.log("List of Arguments Node")
        arguments = []
        for argument in list_of_arguments.getChildren():
            if isinstance(argument, NeuralLogParser.ArgumentContext):
                arguments.append(self.process_argument(argument))

        return arguments

    def process_body(self, body):
        """
        Process the body node.

        :param body: the Horn clause node
        :type body: NeuralLogParser.BodyContext
        :return: the body of the clause
        :rtype: list[Literal]
        """
        self.log("Body Node")
        literals = []
        for literal in body.getChildren():
            if isinstance(literal, NeuralLogParser.LiteralContext):
                literals.append(self.process_literal(literal))

        return literals

    def process_argument(self, argument):
        """
        Process the argument node.

        :param argument: the argument node
        :type argument: NeuralLogParser.ArgumentContext
        :return: the argument
        :rtype: Term or float or int
        """
        self.log("Argument Node")
        child = argument.getChild(0)
        if isinstance(child, NeuralLogParser.NumberContext):
            return self.process_number(child)
        elif isinstance(child, NeuralLogParser.TermContext):
            return self.process_term(child)
        else:
            raise BadArgumentException(child.getText())

    def process_literal(self, literal):
        """
        Process the literal node.

        :param literal: the Horn clause node
        :type literal: NeuralLogParser.LiteralContext
        :return: the literal of the body of the clause
        :rtype: Literal
        """
        self.log("Literal Node")
        trainable = False
        negated = False
        child_count = literal.getChildCount()
        for i in range(0, child_count - 1):
            if literal.getChild(i).getSymbol().type == NeuralLogParser.NEGATION:
                negated = True
            elif literal.getChild(i).getSymbol().type == \
                    NeuralLogParser.TRAINABLE_IDENTIFIER:
                trainable = True
        atom = self.process_atom(literal.getChild(child_count - 1))
        return Literal(atom, negated=negated, trainable=trainable)

    def process_number(self, number):
        """
        Process the number node.

        :param number: the number node
        :type number: NeuralLogParser.NumberContext
        :return: the number
        :rtype: float or int
        """
        self.log("Number Node")
        if number.getChild(0).getSymbol().type == NeuralLogParser.INTEGER:
            return int(number.getText())
        else:
            return float(number.getText())

    def process_term(self, term):
        """
        Process the term node.

        :param term: the term node
        :type term: NeuralLogParser.TermContext
        :return: the term
        :rtype: Term
        """
        self.log("Term Node")
        if term.getChildCount() == 1:
            child = term.getChild(0)
            text = child.getText()  # type: str
            if child.getSymbol().type == NeuralLogParser.TERM:
                converted_term = get_term_from_string(text)
                self.add_constant(converted_term)
                return converted_term
            elif child.getSymbol().type == NeuralLogParser.QUOTED:
                quote = Quote(text.format_map(self.scope))
                self.add_constant(quote)
                return quote
        solved_terms = self.solve_for_placeholders(term.getChildren())
        if len(solved_terms) == 1 and solved_terms[0][0] != "{":
            solved_term = extract_value(solved_terms[0])
            if isinstance(solved_term, str):
                converted_term = get_term_from_string(solved_term)
                self.add_constant(converted_term)
                return converted_term
            return solved_term
        return TemplateTerm(solved_terms)

    def get_for_range(self, node):
        """
        Gets the terms or range of the for loop.

        :param node: the node of the AST
        :type node: NeuralLogParser.For_termsContext or
        NeuralLogParser.For_rangeContext
        :return: the list of elements
        :rtype: list[str]
        """
        if isinstance(node, NeuralLogParser.For_rangeContext):
            return self.process_for_range(node)
        elif isinstance(node, NeuralLogParser.For_termsContext):
            return self.process_for_terms(node)

    def process_for_range(self, node):
        """
        Process the for range node.

        :param node: the for range node
        :type node: NeuralLogParser.For_rangeContext
        :return: a list of the items in the range
        :rtype: list[str]
        """
        self.log("ForRange Node")
        start = int(node.getChild(1).getText())
        end = int(node.getChild(3).getText())
        return list(map(lambda x: str(x), range(start, end + 1)))

    def process_for_terms(self, node):
        """
        Process the for terms node.

        :param node: the for terms node
        :type node: NeuralLogParser.For_termsContext
        :return: a list of the terms
        :rtype: list[str]
        """

        self.log("ForTerms Node")
        items = []
        for term in node.getChildren():
            items.append(term.getText())
        return items

    def solve_for_placeholders(self, terms):
        """
        Solves the placeholders from the for.

        :param terms: the nodes of the terms to be solved.
        :type terms: list[TerminalNodeImpl]
        :return: the terms
        :rtype: list[str]
        """
        solved_terms = []
        joint_term = ""
        for term in terms:
            term_text = term.getText()
            if term.getSymbol().type == NeuralLogParser.PLACE_HOLDER:
                key = term_text[1:-1]
                if key in self.scope.keys():
                    joint_term += self.scope[key]
                else:
                    if joint_term != "":
                        solved_terms.append(str(joint_term))
                        joint_term = ""
                    solved_terms.append(term_text)
            else:
                joint_term += term_text
        if joint_term != "":
            solved_terms.append(str(joint_term))

        return solved_terms

    def add_constants(self, clause):
        """
        Adds the constants of the clause in the constant set.
        :param clause: the clause
        :type clause: HornClause
        """
        for term in clause.head:
            self.add_constant(term)
        for literal in clause.body:
            for term in literal.terms:
                self.add_constant(term)

    def add_constant(self, term):
        """
        Add the term to the set of constants, if the term is a constant
        :param term: the term to be added
        :type term: Term
        """
        if isinstance(term, Constant) or \
                (isinstance(term, Quote) and term.is_constant()):
            self.constants.add(term)

    def expand_placeholders(self):
        """
        Expands the placeholders from the Horn clauses.
        """
        expanded_clauses = []
        predicates_names = set()
        constants_names = set()
        predicates_names.update(map(lambda x: x.get_name(), self.predicates))
        constants_names.update(map(lambda x: x.get_name(), self.constants))

        for clause in self.clauses:
            if not clause.is_template():
                expanded_clauses.append(clause)
                continue
            if not isinstance(clause, HornClause):
                raise BadClauseException(clause)
            place_holders = dict()
            for literal in clause.body:
                if literal.predicate.is_template():
                    ground_placeholders(literal.predicate.parts, place_holders,
                                        *predicates_names)
                for term in literal.terms:
                    if not term.is_template():
                        continue
                    if isinstance(term, Quote):
                        parts = PLACE_HOLDER.split(term.get_name())
                    else:
                        parts = term.parts
                    ground_placeholders(parts,
                                        place_holders,
                                        *predicates_names, *constants_names)
            solved = sorted(solve_place_holders(clause, place_holders),
                            key=lambda x: x.__str__())
            for new_clause in solved:
                if self.is_valid(new_clause):
                    expanded_clauses.append(new_clause)
                    self.predicates.add(clause.head.predicate)
                    self.add_constants(new_clause)
        self.clauses = expanded_clauses

    def log(self, message, *args, level=logging.DEBUG):
        """
        Logs the current state on the AST.

        :param message: the message to log
        :type message: str
        :param args: the arguments to the message
        :type args: list[str] or str
        :param level: the level of the log
        :type level: int
        """
        logger.log(level, ("\t" * self.depth) + message, *args)

    def is_valid(self, clause):
        """
        Checks if the solved clause is valid.

        It is valid if all predicates in its body already exist in the program.

        :param clause: the clause
        :type clause: HornClause
        :return: true if it is valid, false otherwise.
        :rtype: bool
        """
        if clause.is_template():
            return False
        for literal in clause.body:
            if literal.predicate not in self.predicates:
                return False

        return True


def ground_placeholders(parts, place_holders_map, *possible_constants):
    """
    Finds the possible substitution for the place holders found in parts.

    The substitutions are place on the place_holders map.

    :param parts: the parts of the template term
    :type parts: list[str]
    :param place_holders_map: the map containing the place_holders
    :type place_holders_map: dict[str, set[str]]
    :param possible_constants: the possible constants to replace the
    place_holders
    :type possible_constants: str
    """
    name = ""
    place_holders = []
    for part in parts:
        if part.startswith("{"):
            place_holders.append(part[1:-1])
            name += "(.+)"
        else:
            name += part
    length = len(place_holders)
    name_regex = re.compile(name)
    possible_subs = dict()
    for cons in possible_constants:
        for match in name_regex.finditer(cons):
            for i in range(length):
                possible_subs.setdefault(place_holders[i],
                                         set()).add(match.group(i + 1))

    for k, v in possible_subs.items():
        if k in place_holders_map.keys():
            place_holders_map[k] = place_holders_map[k].intersection(v)
        else:
            place_holders_map[k] = v
