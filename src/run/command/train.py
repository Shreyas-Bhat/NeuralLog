"""
Command Line Interface command to train the model.
"""

import argparse
import logging
import os
import time
from functools import reduce
from typing import Dict

import numpy as np
import tensorflow as tf
from tensorflow.keras.callbacks import ModelCheckpoint
from tensorflow.python.keras.callbacks import TensorBoard

from src.knowledge.program import NeuralLogProgram, BiDict, \
    get_predicate_from_string, print_neural_log_program, DEFAULT_PARAMETERS
from src.language.language import Predicate
from src.language.parser.ply.neural_log_parser import NeuralLogParser, \
    NeuralLogLexer
from src.network.callbacks import EpochLogger, get_neural_log_callback, \
    AbstractNeuralLogCallback, get_formatted_name
from src.network.dataset import print_neural_log_predictions, get_dataset_class
from src.network.network import NeuralLogNetwork, LossMaskWrapper
from src.network.network_functions import get_loss_function, CRFLogLikelihood
from src.run.command import Command, command, print_args, create_log_file, \
    TRAIN_SET_NAME, VALIDATION_SET_NAME, TEST_SET_NAME

METRIC_FILE_PREFIX = "metric_"
LOGIC_PROGRAM_EXTENSION = ".pl"

DEFAULT_LOSS = "mean_squared_error"
DEFAULT_OPTIMIZER = "sgd"
DEFAULT_REGULARIZER = None
DEFAULT_BATCH_SIZE = 1
DEFAULT_NUMBER_OF_EPOCHS = 10
DEFAULT_VALID_PERIOD = 1
DEFAULT_INVERTED_RELATIONS = True
DEFAULT_MASK_PREDICTIONS = False
DEFAULT_CLIP_LABELS = False

TAB_SIZE = 4

COMMAND_NAME = "train"

logger = logging.getLogger(__name__)


def get_clauses(filepath):
    """
    Gets the clauses from the file in `filepath`.

    :param filepath: the filepath
    :type filepath: str
    :return: the clauses
    :rtype: List[Clause]
    """
    # PLY
    start_func = time.perf_counter()
    lexer = NeuralLogLexer()
    parser = NeuralLogParser(lexer)
    parser.parse(filepath)
    clauses = parser.get_clauses()
    end_func = time.perf_counter()

    logger.info("File:\t%s", filepath)
    logger.info("\t- Total reading time:\t%0.3fs",
                end_func - start_func)

    return clauses


def format_arguments(message, arguments):
    """
    Formats the arguments for the help message.

    :param message: the initial message
    :type message: str
    :param arguments: the arguments to be formatted
    :type arguments: list[list[str]] or list[tuple[str]]
    :return: the formatted message
    :rtype: str
    """
    formatted = message
    formatted += "\n\n"
    formatted += "The following parameters can be set in the logic file " \
                 "by using the special\npredicate set_parameter or " \
                 "set_predicate_parameter*.\n" \
                 "Syntax:\n\n" \
                 "set_parameter(<name>, <value>).\nor\n" \
                 "set_parameter(<name>, class_name, " \
                 "<class_name>).\n" \
                 "set_parameter(<name>, config, <config_1>, " \
                 "<value_1>).\n...\n" \
                 "set_parameter(<name>, config, <config_n>, " \
                 "<value_n>).\n\nor\n\n" \
                 "set_predicate_parameter(<predicate>, <name>, " \
                 "<value>).\nor\n" \
                 "set_predicate_parameter(<predicate>, <name>, class_name, " \
                 "<class_name>).\n" \
                 "set_predicate_parameter(<predicate>, <name>, config, " \
                 "<config_1>, " \
                 "<value_1>).\n...\n" \
                 "set_predicate_parameter(<predicate>, <name>, config, " \
                 "<config_n>, " \
                 "<value_n>).\n\n" \
                 "One can use $<predicate>[<index>] to access the size of " \
                 "the predicate term\nwhen setting parameters."
    formatted += "\n\n"
    max_key_size = max(map(lambda x: len(x[0]), arguments))
    stride = max_key_size + TAB_SIZE
    for argument in arguments:
        key, value = argument[0], argument[1]
        if len(argument) > 2 and argument[2]:
            key += "*"
        formatted += key + " " * (stride - len(key) - 1)
        length = 0
        for word in value.split(" "):
            length += len(word) + 1
            if length > 79 - stride:
                length = len(word) + 1
                formatted += "\n"
                formatted += " " * stride
            else:
                formatted += " "
            formatted += word
        formatted += "\n\n"
    formatted += "* this feature may be set individually for each " \
                 "predicate.\n" \
                 "If it is not defined for a specific predicate,\n" \
                 "the default globally defined value will be used."
    formatted += "\n\n"
    return formatted


def find_best_model(checkpoint, history):
    """
    Finds the best model saved by the checkpoint.

    :param checkpoint: the checkpoint
    :type checkpoint: ModelCheckpoint
    :param history: a dictionary with the metrics and their values for
    each epoch.
    :type history: dict[str, np.ndarray]
    :return: the path of the best model
    :rtype: str or None
    """
    if checkpoint.save_best_only:
        return checkpoint.filepath

    period = checkpoint.period
    monitor = checkpoint.monitor

    best = checkpoint.best
    monitor_op = checkpoint.monitor_op

    values = history.get(monitor, None)
    if values is None:
        return None
    best_epoch = 0
    for i in range(len(values)):
        if monitor_op(values[i], best):
            best = values[i]
            best_epoch = i

    return checkpoint.filepath.format(epoch=(best_epoch + 1) * period)


def unique(elements):
    """
    Returns a list of unique elements from the `elements`.

    :param elements: the input elements.
    :type elements: list
    :return: the list of unique elements
    :rtype: list
    """
    element_set = set()
    unique_list = []
    for element in elements:
        if isinstance(element, dict):
            key = frozenset(element)
        else:
            key = element
        if key in element_set:
            continue
        unique_list.append(element)
        element_set.add(key)
    return unique_list


def deserialize_loss(loss_function):
    """
    Deserializes the loss functions.

    :param loss_function: the loss functions
    :type loss_function: str or dict
    :return: the deserialized loss functions
    :rtype: function or dict[function]
    """
    if isinstance(loss_function, dict):
        result = dict()
        for key, value in loss_function.items():
            result[key] = get_loss_function(value)
    else:
        result = get_loss_function(loss_function)
    return result


@command(COMMAND_NAME)
class Train(Command):
    """
    Trains the neural network.
    """

    def __init__(self, program, args, direct=False):
        super().__init__(program, args, direct)
        self.neural_program = NeuralLogProgram()
        self.parameters = None
        self.train_set = None
        self.validation_set = None
        self.test_set = None
        self.output_map = BiDict()  # type: BiDict[Predicate, str]
        self.validation_period = DEFAULT_VALID_PERIOD
        self.epochs = 1
        self.callbacks = []
        self.best_models = dict()  # type: Dict[str, ModelCheckpoint]

    # noinspection PyMissingOrEmptyDocstring,DuplicatedCode
    def build_parser(self) -> argparse.ArgumentParser:
        program = self.program
        if not self.direct:
            program += " {}".format(COMMAND_NAME)
        # noinspection PyTypeChecker
        parser = argparse.ArgumentParser(
            prog=program,
            description=self.get_command_description(),
            formatter_class=argparse.RawDescriptionHelpFormatter)

        # Input
        parser.add_argument('--program', '-p', metavar='program',
                            type=str, required=True, nargs="+",
                            help="The program file(s)")
        parser.add_argument('--train', '-t', metavar='train',
                            type=str, required=False, nargs="+", default=[],
                            help="The train file(s)")
        parser.add_argument('--validation', '-valid', metavar='validation',
                            type=str, required=False, nargs="+", default=[],
                            help="The validation file(s)")
        parser.add_argument('--test', '-test', metavar='test',
                            type=str, required=False, nargs="+", default=[],
                            help="The test file(s)")
        parser.add_argument('--loadModel', '-l', metavar='loadModel',
                            type=str, default=None, required=False,
                            help="If set, loads the model from the path and "
                                 "continues from the loaded model")

        # Output
        parser.add_argument("--outputPath", "-o", metavar='outputPath',
                            type=str, default=None, required=False,
                            help="The path to save the outputs")
        parser.add_argument("--lastModel", "-lm", metavar='lastModel',
                            type=str, default=None, required=False,
                            help="The path to save the last learned model. "
                                 "If `outputPath` is given, "
                                 "this path will be relative to it")
        parser.add_argument("--lastProgram", "-lp", metavar='lastProgram',
                            type=str, default=None, required=False,
                            help="The name of the file to save the last "
                                 "learned program. If `outputPath` is given, "
                                 "this path will be relative to it")
        parser.add_argument("--lastInference", "-li", metavar='lastInference',
                            type=str, default=None, required=False,
                            help="The prefix of the file to save the "
                                 "inferences of the last learned program. "
                                 "The name of the dataset and the `.pl` "
                                 "extension will be appended to it. "
                                 "If `outputPath` is given, this path will "
                                 "be relative to it")

        # Log
        parser.add_argument("--logFile", "-log", metavar='file',
                            type=str, default=None,
                            help="The file path to save the log into")
        parser.add_argument("--tensorBoard", "-tb", metavar='file',
                            type=str, default=None,
                            help="Creates a log event for the TensorBoard "
                                 "on the given path")
        parser.add_argument("--verbose", "-v", dest="verbose",
                            action="store_true",
                            help="Activated a verbose log")
        parser.set_defaults(verbose=False)
        return parser

    # noinspection PyMissingOrEmptyDocstring
    def get_command_description(self):
        message = super().get_command_description()
        arguments = list(
            map(lambda x: (x[0], x[2], x[3] if len(x) > 3 else True),
                DEFAULT_PARAMETERS))
        arguments += [
            ("inverse_relations", "if `True`, creates also the inverted "
                                  "relation for each output predicate. The "
                                  "default value is: "
                                  "{}".format(DEFAULT_INVERTED_RELATIONS)),
            ("loss_function", "the loss function of the neural network and, "
                              "possibly, its options. The default value is: "
                              "{}. It can be individually specified for each "
                              "predicate, just put another term with the name "
                              "of the predicate"
                              "".format(DEFAULT_LOSS.replace("_", " "))),
            ("metrics", "the metric functions to eval the neural network and, "
                        "possibly, its options. The default value is the loss"
                        "function, which is always appended to the metrics. "
                        "It can be individually specified for each "
                        "predicate, just put another term with the name of "
                        "the predicate"),
            ("optimizer", "the optimizer for the training and, "
                          "possibly, its options. The default value is: "
                          "{}".format(DEFAULT_OPTIMIZER)),
            ("regularizer", "specifies the regularizer, it can be `l1`, `l2`"
                            "or `l1_l2`. The default value is: "
                            "{}".format(DEFAULT_REGULARIZER)),
            ("batch_size", "the batch size. The default value is: "
                           "{}".format(DEFAULT_BATCH_SIZE)),
            ("epochs", "the number of epochs. The default value is: "
                       "{}".format(DEFAULT_NUMBER_OF_EPOCHS)),
            ("shuffle", "if set, shuffles the examples of the "
                        "dataset for each iteration. This option is "
                        "computationally expensive"),
            ("validation_period", "the interval (number of epochs) between the "
                                  "validation. The default value is:"
                                  "{}".format(DEFAULT_VALID_PERIOD)),
            ("callback", "a dictionary of callbacks to be used on training. "
                         "The default value is `None`"),
            ("best_model", "a dictionary with keys matching pointing to "
                           "`ModelCheckpoints` in the callback dictionary."
                           "For each entry, it will save the program and "
                           "inference files (with the value of the entry as "
                           "prefix) based on the best model saved by the "
                           "checkpoint defined by the key. "
                           "The default value is `None`"),
            ("mask_predictions", "if `True`, it masks the output of the "
                                 "network, during the training phase. Before "
                                 "the loss function, it sets the predictions "
                                 "of unknown examples to `0` by multiplying "
                                 "the output of the network by the square of "
                                 "the labels. In order to this method work, "
                                 "the labels must be: `1`, for positive "
                                 "examples; `-1`, for negative examples; "
                                 "and `0`, for unknown examples"),
            ("clip_labels", "if `True`, clips the values of the labels "
                            "to [0, 1]. This is useful when one wants to keep "
                            "the output of the network in [0, 1], and also use "
                            "the mask_predictions features."),
        ]
        return format_arguments(message, arguments)

    def _read_parameters(self, output_map):
        """
        Reads the default parameters found in the program
        """
        self.parameters = dict(self.neural_program.parameters)
        self.parameters.setdefault("mask_predictions", DEFAULT_MASK_PREDICTIONS)
        self.parameters["loss_function"] = self._get_loss_function(output_map)
        self.parameters.setdefault("clip_labels", DEFAULT_CLIP_LABELS)
        self._wrap_mask_loss_functions()
        self.parameters["metrics"] = self._get_metrics(output_map)
        self.parameters.setdefault("optimizer", DEFAULT_OPTIMIZER)
        self.parameters.setdefault("regularizer", DEFAULT_REGULARIZER)
        self.parameters.setdefault("batch_size", DEFAULT_BATCH_SIZE)
        self.parameters.setdefault("epochs", DEFAULT_NUMBER_OF_EPOCHS)
        self.parameters.setdefault("validation_period", DEFAULT_VALID_PERIOD)

        print_args(self.parameters, logger)

    def _get_loss_function(self, output_map):
        """
        Gets the loss function.

        :param output_map: the map of the outputs of the neural network by the
        predicate
        :type output_map: BiDict[tuple(Predicate, bool), str]
        :return: the loss function for each output
        :rtype: str or dict[str, str]
        """
        loss_function = self.parameters.get("loss_function", DEFAULT_LOSS)
        if isinstance(loss_function, dict) and \
                "class_name" not in loss_function and \
                "config" not in loss_function:
            default_loss = DEFAULT_LOSS
            results = dict()
            for key, value in loss_function.items():
                key = get_predicate_from_string(key)
                has_not_match = True
                for predicate, output in output_map.items():
                    if key.equivalent(predicate[0]):
                        results.setdefault(output, value)
                        has_not_match = False
                if has_not_match:
                    default_loss = value
            for key in output_map.values():
                results.setdefault(key, default_loss)
            for key, value in results.items():
                results[key] = get_loss_function(value)
        else:
            results = get_loss_function(loss_function)
        return results

    def _wrap_mask_loss_functions(self):
        """
        Wraps the loss functions to mask the values of unknown examples.

        It multiplies the output of the network by the square of the labels. In
        order to this method work, the labels must be: `1`, for positive
        examples; `-1`, for negative examples; and `0`, for unknown examples.

        In this way, the square of the labels will be `1` for the positive and
        negative examples; and `0`, for the unknown examples. When multiplied by
        the prediction, the predictions of the unknown examples will be zero,
        thus, having no error and no gradient for those examples. While the
        predictions of the known examples will remain the same.
        """
        if not self.parameters["mask_predictions"]:
            return
        loss_function = self.parameters["loss_function"]
        label_function = None
        if self.parameters["clip_labels"]:
            label_function = lambda x: tf.clip_by_value(x, clip_value_min=0.0,
                                                        clip_value_max=1.0)
        if isinstance(loss_function, dict):
            functions = dict()
            for key, value in loss_function.items():
                functions[key] = LossMaskWrapper(value, label_function)
        else:
            functions = LossMaskWrapper(loss_function, label_function)
        self.parameters["loss_function"] = functions

    def _get_metrics(self, output_map):
        """
        Gets the metrics.

        :param output_map: the map of the outputs of the neural network by the
        predicate
        :type output_map: BiDict[tuple(Predicate, bool), str]
        :return: the loss function for each output
        :rtype: str or dict[str, str]
        """
        metrics = self.parameters.get("metrics", None)
        loss = self.parameters["loss_function"]
        if isinstance(metrics, dict):
            results = dict()
            all_metrics = []
            for key, values in metrics.items():
                if isinstance(values, dict):
                    values = \
                        sorted(values.items(), key=lambda x: x[0])
                    values = list(map(lambda x: x[1], values))
                else:
                    values = [values]
                key = get_predicate_from_string(key)
                has_not_match = True
                for predicate, output in output_map.items():
                    if key.equivalent(predicate[0]):
                        metric = results.get(output, [])
                        results[output] = metric + values
                        has_not_match = False
                if has_not_match:
                    all_metrics.append((key, values))
            all_metrics = sorted(all_metrics, key=lambda x: x[0])
            all_metrics = list(map(lambda x: x[1], all_metrics))
            if len(all_metrics) > 0:
                all_metrics = reduce(list.__add__, all_metrics)
            for key in output_map.values():
                values = results.get(key, [])
                default_loss = loss.get(key) if isinstance(loss, dict) else loss
                results[key] = unique([default_loss] + all_metrics + values)
            return results
        elif metrics is None:
            if isinstance(loss, dict):
                return loss
            else:
                return [loss]
        else:
            if isinstance(loss, dict):
                results = dict()
                for key in loss.keys():
                    results[key] = [loss[key], self.parameters["metrics"]]
                return results
            else:
                return [loss, self.parameters["metrics"]]

    # noinspection PyMissingOrEmptyDocstring,PyAttributeOutsideInit
    def parse_args(self):
        # Log
        args = self.parser.parse_args(self.args)
        log_file = args.logFile
        create_log_file(log_file)
        print_args(args, logger)
        self.tensor_board = args.tensorBoard

        # Input
        self.program_files = args.program
        self.train_files = args.train
        self.validation_files = args.validation
        self.test_files = args.test
        self.load_model = args.loadModel
        self.train = len(self.train_files) > 0
        self.valid = len(self.validation_files) > 0
        self.test = len(self.test_files) > 0

        # Output
        self.output_path = args.outputPath
        self.last_model = args.lastModel
        self.last_program = args.lastProgram
        self.last_inference = args.lastInference
        self.verbose = args.verbose
        if self.verbose:
            logger.setLevel(logging.DEBUG)
            # src.run.H1.setLevel(logging.DEBUG)

    def build(self):
        """
        Builds the neural network and prepares for training.
        """
        self._read_clauses_from_file()
        self._build_model()

    def _read_clauses_from_file(self):
        """
        Read the clauses from the files.
        """
        logger.info("Reading input files...")
        start_func = time.perf_counter()
        self._read_input_file(self.program_files, "program")
        end_program = time.perf_counter()
        end_train = end_program
        if self.train:
            self._read_input_file(self.train_files, TRAIN_SET_NAME)
            end_train = time.perf_counter()
        end_validation = end_train
        end_test = end_train
        end_reading = end_train
        if self.valid > 0:
            self._read_input_file(self.validation_files, VALIDATION_SET_NAME)
            end_validation = time.perf_counter()
            end_reading = end_validation
        if self.test > 0:
            self._read_input_file(self.test_files, TEST_SET_NAME)
            end_test = time.perf_counter()
            end_reading = end_test
        self.neural_program.build_program()
        end_func = time.perf_counter()
        # logger.info("Total number of predictable constants:\t%d",
        #             len(self.neural_program.iterable_constants))
        logger.info("Program reading time:   \t%0.3fs",
                    end_program - start_func)
        if self.train:
            logger.info("Train reading time:     \t%0.3fs",
                        end_train - end_program)
        if self.valid:
            logger.info("Validation reading time:\t%0.3fs",
                        end_validation - end_train)
        if self.test:
            logger.info("Test reading time:      \t%0.3fs",
                        end_test - end_validation)
        logger.info("Building program time:  \t%0.3fs",
                    end_func - end_reading)
        logger.info("Total reading time:     \t%0.3fs",
                    end_reading - start_func)

    def _read_input_file(self, program_files, name):
        logger.info("Reading %s...", name)
        for file in program_files:
            file_clauses = get_clauses(file)
            self.neural_program.add_clauses(file_clauses, example_set=name)

    def _build_model(self):
        """
        Builds and compiles the model.
        """
        start_func = time.perf_counter()
        logger.info("Building model...")
        self._create_dataset()
        regularizer = self.neural_program.parameters.get(
            "regularizer", DEFAULT_REGULARIZER)
        self.model = NeuralLogNetwork(
            self.neural_dataset, train=True,
            regularizer=regularizer
        )
        self.model.build_layers()
        self.output_map = self._get_output_map()
        self._read_parameters(self.output_map)
        self._log_parameters(
            ["clip_labels", "loss_function", "optimizer", "regularizer"
                                                          "metrics",
             "inverse_relations"],
            self.output_map.inverse
        )
        self.model.compile(
            loss=self.parameters["loss_function"],
            optimizer=self.parameters["optimizer"],
            metrics=self.parameters["metrics"]
        )

        if self.load_model is not None:
            self.model.load_weights(self.load_model)

        end_func = time.perf_counter()

        logger.info("\nModel building time:\t%0.3fs", end_func - start_func)

    def _create_dataset(self):
        inverse_relations = self.neural_program.parameters.get(
            "inverse_relations", DEFAULT_INVERTED_RELATIONS)
        dataset_class = self.neural_program.parameters["dataset_class"]
        config = dict()
        if isinstance(dataset_class, dict):
            class_name = dataset_class["class_name"]
            config.update(dataset_class["config"])
        else:
            class_name = dataset_class
        config["program"] = self.neural_program
        config["inverse_relations"] = inverse_relations
        self.neural_dataset = get_dataset_class(class_name)(**config)

    def _get_output_map(self):
        output_map = BiDict()  # type: BiDict[Predicate, str]
        count = 1
        for predicate in self.model.predicates:
            output_map[predicate] = "output_{}".format(count)
            count += 1
        return output_map

    def _log_parameters(self, parameter_keys, map_dict=None):
        if logger.isEnabledFor(logging.INFO):
            parameters = dict(filter(lambda x: x[0] in parameter_keys,
                                     self.parameters.items()))
            if len(parameters) == 0:
                return
            if map_dict is not None:
                for key, value in parameters.items():
                    if isinstance(value, dict):
                        new_value = dict()
                        for k, v in value.items():
                            k = map_dict.get(k, k)
                            if isinstance(k, tuple):
                                k = k[0].__str__() + (" (inv)" if k[1] else "")
                            new_value[k] = v
                        parameters[key] = new_value
            print_args(parameters, logger)

    def fit(self):
        """
        Trains the neural network.
        """
        start_func = time.perf_counter()
        logger.info("Training the model...")
        self.epochs = self.parameters["epochs"]
        self.validation_period = self.parameters["validation_period"]
        self._log_parameters(["epochs", "validation_period"])
        self.callbacks = self._get_callbacks()
        self._log_parameters(["callback"])
        history = self.model.fit(
            self.train_set,
            epochs=self.epochs,
            validation_data=self.validation_set,
            validation_freq=self.validation_period,
            callbacks=self.callbacks
        )
        end_func = time.perf_counter()
        logger.info("Total training time:\t%0.3fs", end_func - start_func)

        return history

    def _get_callbacks(self):
        callbacks = []
        if self.tensor_board is not None:
            callbacks.append(TensorBoard(self.tensor_board))

        self._build_parameter_callbacks(callbacks)

        callbacks.append(EpochLogger(self.epochs, self.output_map.inverse))
        return callbacks

    def _build_parameter_callbacks(self, callbacks):
        callbacks_parameters = self.parameters.get("callback", None)
        if callbacks_parameters is None:
            return

        best_model_parameters = self.parameters.get("best_model", dict())
        for name, identifier in callbacks_parameters.items():
            if isinstance(identifier, dict):
                class_name = identifier["class_name"]
                config = identifier.get("config", dict())
            else:
                class_name = identifier
                config = dict()
            callback_class = get_neural_log_callback(class_name)
            if callback_class is None:
                continue
            config = self._adjust_config_for_callback(config, callback_class)
            callback = callback_class(**config)
            if isinstance(callback, ModelCheckpoint):
                best_model_name = best_model_parameters.get(name, None)
                if best_model_name is not None:
                    self.best_models[best_model_name] = callback
            callbacks.append(callback)

    def _adjust_config_for_callback(self, config, callback_class):
        config.setdefault("period", self.validation_period)
        if issubclass(callback_class, AbstractNeuralLogCallback):
            config["train_command"] = self
        elif issubclass(callback_class, ModelCheckpoint):
            config.setdefault("save_best_only", True)
            config.setdefault("save_weights_only", True)
            has_no_filepath = "filepath" not in config
            config.setdefault("filepath", config["monitor"])
            config["filepath"] = self._get_output_path(config["filepath"])
            if not config["save_best_only"]:
                config["filepath"] = config["filepath"] + "_{epoch}"
            elif has_no_filepath:
                config["filepath"] = config["filepath"] + "_best"
            if "mode" not in config:
                if config["monitor"].startswith("mean_rank"):
                    config["mode"] = "min"
                else:
                    config["mode"] = "max"
        return config

    def _build_examples_set(self):
        start_func = time.perf_counter()
        logger.info("Creating training dataset...")
        shuffle = self.neural_program.parameters.get("shuffle", False)
        batch_size = self.parameters["batch_size"]
        self._log_parameters(["dataset_class", "batch_size", "shuffle"])
        end_func = time.perf_counter()
        train_set_time = 0
        validation_set_time = 0
        test_set_time = 0

        if self.train:
            self.train_set = self.neural_dataset.get_dataset(
                example_set=TRAIN_SET_NAME,
                batch_size=batch_size,
                shuffle=shuffle)
            end_train = time.perf_counter()
            train_set_time = end_train - start_func
            end_func = end_train
        if self.valid:
            self.validation_set = self.neural_dataset.get_dataset(
                example_set=VALIDATION_SET_NAME, batch_size=batch_size)
            end_valid = time.perf_counter()
            validation_set_time = end_valid - end_func
            end_func = end_valid
        if self.test:
            self.test_set = self.neural_dataset.get_dataset(
                example_set=TEST_SET_NAME, batch_size=batch_size)
            end_test = time.perf_counter()
            test_set_time = end_test - end_func
            end_func = end_test

        if self.train:
            logger.info("Train dataset creating time:      \t%0.3fs",
                        train_set_time)
        if self.valid:
            logger.info("Validation dataset creation time: \t%0.3fs",
                        validation_set_time)
        if self.test:
            logger.info("Test dataset creation time:       \t%0.3fs",
                        test_set_time)

        logger.info("Total dataset creation time:      \t%0.3fs",
                    end_func - start_func)

    # noinspection PyMissingOrEmptyDocstring
    def run(self):
        self.build()
        history = None
        self._build_examples_set()
        self._save_transitions("transition_before.txt")
        if self.train:
            history = self.fit()
            if logger.isEnabledFor(logging.INFO):
                hist = history.history
                hist = dict(map(
                    lambda x: (get_formatted_name(
                        x[0], self.output_map.inverse), x[1]), hist.items()))
                logger.info("\nHistory:")
                for key, value in hist.items():
                    logger.info("%s: %s", key, value)
                logger.info("")

        logger.info("Saving data...")
        start_save = time.perf_counter()
        if history is not None and self.last_model is not None:
            filepath = self._get_output_path(self.last_model)
            self.model.save_weights(filepath)
            logger.info("\tLast model saved at:\t{}".format(filepath))
            for metric in history.history:
                array = np.array(history.history[metric])
                # noinspection PyTypeChecker
                metric = get_formatted_name(metric, self.output_map.inverse)
                metric = METRIC_FILE_PREFIX + metric
                metric_path = os.path.join(self.output_path,
                                           "{}.txt".format(metric))
                # noinspection PyTypeChecker
                np.savetxt(metric_path, array, fmt="%0.8f")

        if not self.train and self.load_model is None:
            return

        self.save_program(self.last_program)
        self.save_inferences(self.last_inference)

        if history is not None:
            logger.info("")
            for key, value in self.best_models.items():
                path = find_best_model(value, history.history)
                if path is None:
                    continue
                self.model.load_weights(path)
                self.save_program(key + "program.pl")
                if self.last_inference is not None:
                    self.save_inferences(key)
                logger.info("\tBest model for {} saved at:\t{}".format(
                    key, path))
        end_save = time.perf_counter()
        self._save_transitions("transition_after.txt")

        logger.info("Total data saving time:\t%0.3fs", end_save - start_save)

    def _save_transitions(self, filename):
        """
        Saves the transitions to file.
        """
        # TODO: Create a way to save this information into a predicate
        #  defined in the logic program
        loss_function = self.parameters["loss_function"]
        if isinstance(loss_function, LossMaskWrapper):
            loss_function = loss_function.function
        if isinstance(loss_function, CRFLogLikelihood):
            filepath = self._get_output_path(filename)
            transition = loss_function.transition_params.numpy()
            np.savetxt(filepath, transition)
            logger.info("transitions:\n%s", transition)

    def save_program(self, program_path):
        """
        Saves the program of the current model.

        :param program_path: the path to save the program
        :type program_path: str
        """
        if program_path is not None:
            self.model.update_program()
            output_program = self._get_output_path(program_path)
            output_file = open(output_program, "w")
            print_neural_log_program(self.neural_program, output_file)
            output_file.close()
            logger.info("\tProgram saved at:\t{}".format(output_program))

    def save_inferences(self, file_prefix):
        """
        Saves the inferences of the current model of the different datasets.

        :param file_prefix: the prefix of the path to be appended with
        the dataset's name
        :type file_prefix: str
        """
        if file_prefix is not None:
            if self.train or self.valid or self.test:
                logger.info("\tInferences saved at:")

            if self.train:
                self._save_inference_for_dataset(file_prefix, TRAIN_SET_NAME)

            if self.valid:
                self._save_inference_for_dataset(
                    file_prefix, VALIDATION_SET_NAME)

            if self.test:
                self._save_inference_for_dataset(file_prefix, TEST_SET_NAME)

    def _save_inference_for_dataset(self, file_prefix, dataset_name):
        tab = "\t\t"
        if dataset_name == VALIDATION_SET_NAME:
            tab = "\t"
        output = self._get_inference_filename(file_prefix, dataset_name)
        logger.info("\t\t{}:{}{}".format(dataset_name, tab, output))
        self.write_neural_log_predictions(output, dataset_name)

    def _get_inference_filename(self, prefix, dataset):
        return self._get_output_path(prefix + dataset + LOGIC_PROGRAM_EXTENSION)

    def _get_output_path(self, suffix):
        if self.output_path is not None:
            return os.path.join(self.output_path, suffix)
        return suffix

    def get_dataset(self, name):
        """
        Gets the dataset based on the name.

        :param name: the name of the dataset
        :type name: str
        :return: the dataset
        :rtype: tf.data.Dataset or None
        """
        if name == TRAIN_SET_NAME:
            return self.train_set
        if name == VALIDATION_SET_NAME:
            return self.validation_set
        if name == TEST_SET_NAME:
            return self.test_set
        return None

    def write_neural_log_predictions(self, filepath, dataset_name):
        """
        Writes the predictions of the model, for the dataset to the `filepath`.

        :param filepath: the file path
        :type filepath: str
        :param dataset_name: the name of the dataset
        :type dataset_name: str
        """
        dataset = self.get_dataset(dataset_name)
        writer = open(filepath, "w")
        print_neural_log_predictions(self.model, self.neural_program,
                                     dataset, writer, dataset_name)
        writer.close()
