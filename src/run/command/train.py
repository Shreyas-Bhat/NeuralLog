"""
Train the model command.
"""

import argparse
import logging
import os
import time

import numpy as np
import tensorflow as tf
from antlr4 import FileStream
from tensorflow.python.keras.callbacks import TensorBoard

from src.knowledge.program import NeuralLogProgram, BiDict, \
    get_predicate_from_string, print_neural_log_program
from src.language.language import Predicate
from src.language.parser.autogenerated.NeuralLogLexer import NeuralLogLexer, \
    CommonTokenStream
from src.language.parser.autogenerated.NeuralLogParser import NeuralLogParser
from src.language.parser.neural_log_listener import NeuralLogTransverse
from src.network.callbacks import EpochLogger, get_neural_log_callback, \
    AbstractNeuralLogCallback, get_formatted_name
from src.network.network import NeuralLogNetwork, NeuralLogDataset, \
    print_neural_log_predictions
from src.run.command import Command, command, print_args, create_log_file, \
    TRAIN_SET_NAME, VALIDATION_SET_NAME, TEST_SET_NAME

METRIC_FILE_PREFIX = "metric_"
TRAIN_PREDICTION_FILE = "train_predictions.pl"
VALIDATION_PREDICTION_FILE = "validation_predictions.pl"
TEST_PREDICTION_FILE = "test_predictions.pl"

DEFAULT_LOSS = "mean_squared_error"
DEFAULT_OPTIMIZER = "sgd"
DEFAULT_REGULARIZER = "l2"
DEFAULT_BATCH_SIZE = 1
DEFAULT_NUMBER_OF_EPOCHS = 10
DEFAULT_VALIDATION_PERIOD = 1

TAB_SIZE = 4

COMMAND_NAME = "train"

logger = logging.getLogger()


def get_clauses(filepath):
    """
    Gets the clauses from the file in `filepath`.

    :param filepath: the filepath
    :type filepath: str
    :return: the clauses
    :rtype: List[Clause]
    """
    # Creates the lexer from the file
    start_func = time.process_time()
    lexer = NeuralLogLexer(FileStream(filepath, "utf-8"))
    stream = CommonTokenStream(lexer)
    end_lexer = time.process_time()

    # Parses the tokens from the lexer
    parser = NeuralLogParser(stream)
    abstract_syntax_tree = parser.program()
    end_parser = time.process_time()

    # Traverses the Abstract Syntax Tree generated by the parser
    transverse = NeuralLogTransverse()
    clauses = transverse(abstract_syntax_tree)
    end_func = time.process_time()

    logger.info("File:\t%s", filepath)
    logger.info("\t- Lexer time:        \t%0.3fs",
                end_lexer - start_func)
    logger.info("\t- Parser time:       \t%0.3fs",
                end_parser - end_lexer)
    logger.info("\t- Transversing time: \t%0.3fs",
                end_func - end_parser)
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
    formatted = message + "\n\n"
    max_key_size = max(map(lambda x: len(x[0]), arguments))
    stride = max_key_size + TAB_SIZE + 1
    for key, value in arguments:
        formatted += key + ":" + " " * (stride - len(key) - 1)
        length = 0
        for word in value.split(" "):
            length += len(word) + 1
            if length > 79 - stride:
                length = len(word) + 1
                formatted += "\n"
                formatted += " " * stride
            formatted += word + " "
        formatted += "\n\n"
    return formatted


@command(COMMAND_NAME)
class Train(Command):
    """
    Trains the neural network.
    """

    def __init__(self, program, args):
        super().__init__(program, args)
        self.neural_program = NeuralLogProgram()
        self.parameters = None
        self.train_set = None
        self.validation_set = None
        self.test_set = None
        self.batch_size = 1
        self.output_map = BiDict()  # type: BiDict[Predicate, str]
        self.validation_period = DEFAULT_VALIDATION_PERIOD
        self.epochs = 1
        self.callbacks = []

    # noinspection PyMissingOrEmptyDocstring
    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog=self.program + " {}".format(COMMAND_NAME),
            description=self.get_command_description(),
            formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument('--program', '-p', metavar='program',
                            type=str, required=True, nargs="+",
                            help="The program file(s)")
        parser.add_argument('--train', '-t', metavar='train',
                            type=str, required=True, nargs="+",
                            help="The train file(s)")
        parser.add_argument('--validation', '-valid', metavar='validation',
                            type=str, required=False, nargs="+", default=[],
                            help="The validation file(s)")
        parser.add_argument('--test', '-test', metavar='test',
                            type=str, required=False, nargs="+", default=[],
                            help="The test file(s)")

        # Output
        parser.add_argument("--outputPath", "-out", metavar='model',
                            type=str, default=None, required=False,
                            help="The path to save the outputs")
        parser.add_argument("--modelName", "-m", metavar='model',
                            type=str, default="last_model", required=False,
                            help="The name of the file to save the last "
                                 "learned model")
        parser.add_argument("--outputProgram", "-o", metavar='program',
                            type=str, default=None, required=False,
                            help="The name of the file to save the learned "
                                 "program")
        parser.add_argument("--savePredictions", "-s",
                            action="store_true",
                            help="If set, saves the predictions to the output "
                                 "path.")
        parser.set_defaults(savePredictions=False)
        # Log
        parser.add_argument("--logFile", "-log", metavar='file',
                            type=str, default=None,
                            help="The file path to save the log into")
        parser.add_argument("--tensorBoard", "-tb", metavar='file',
                            type=str, default=None,
                            help="Creates a log event for the TensorBoard "
                                 "on the given path")
        # Validation
        # parser.add_argument('--filterFiles', '-filters', metavar='filter',
        #                     type=str, nargs='*', default=[],
        #                     help="The logic file, with the known positive "
        #                          "examples, other than the testing examples,"
        #                          " to be filtered from the evaluation")

        return parser

    # noinspection PyMissingOrEmptyDocstring
    def get_command_description(self):
        message = super().get_command_description()
        message += "\nThere following parameters can be set in the logic file" \
                   " by using the special predicate `set_parameter`."
        arguments = [
            ("loss_function", "the loss function of the neural network and, "
                              "possibly, its options. The default value is: "
                              "{}".format(DEFAULT_LOSS.replace("_", " "))),
            ("metrics", "the metric functions to eval the neural network and, "
                        "possibly, its options. The default value is the loss"
                        "function, which is always appended to the metrics"),
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
                                  "{}".format(DEFAULT_VALIDATION_PERIOD)),
            ("callback", "a dictionary of callbacks to be used on training. "
                         "The default value is `None`"),
        ]
        return format_arguments(message, arguments)

    def _read_parameters(self, output_map):
        """
        Reads the default parameters found in the program
        """
        self.parameters = dict(self.neural_program.parameters)
        self.parameters["loss_function"] = self._get_loss_function(output_map)
        self.parameters["metrics"] = self._get_metrics(output_map)
        self.parameters.setdefault("optimizer", DEFAULT_OPTIMIZER)
        self.parameters.setdefault("regularizer", DEFAULT_REGULARIZER)
        self.parameters.setdefault("batch_size", DEFAULT_BATCH_SIZE)
        self.parameters.setdefault("epochs", DEFAULT_NUMBER_OF_EPOCHS)
        self.parameters.setdefault(
            "validation_period", DEFAULT_VALIDATION_PERIOD)

    def _get_loss_function(self, output_map):
        """
        Gets the loss function.

        :param output_map: the map of the outputs of the neural network by the
        predicate
        :type output_map: BiDict[tuple(Predicate, bool), str]
        :return: the loss function for each output
        :rtype: str or dict[str, str]
        """
        loss_function = self.parameters.get("loss_function", None)
        default_loss = DEFAULT_LOSS
        if isinstance(loss_function, dict):
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
            return results
        elif loss_function is None:
            return default_loss
        else:
            return self.parameters["loss_function"]

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
            for key in output_map.values():
                values = results.get(key, [])
                default_loss = loss.get(key) if isinstance(loss, dict) else loss
                results[key] = [default_loss] + all_metrics + values
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
        args = self.parser.parse_args(self.args)
        log_file = args.logFile
        create_log_file(log_file)
        print_args(args)

        self.program_files = args.program
        self.train_files = args.train
        self.validation_files = args.validation
        self.test_files = args.test
        self.train = len(self.train_files) > 0
        self.valid = len(self.validation_files) > 0
        self.test = len(self.test_files) > 0

        self.tensor_board = args.tensorBoard
        self.output_path = args.outputPath
        self.model_name = args.modelName
        self.output_program = args.outputProgram
        self.save_predictions = args.savePredictions

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
        start_func = time.process_time()
        self._read_input_file(self.program_files, "program")
        end_program = time.process_time()
        end_train = end_program
        if self.train:
            self._read_input_file(self.train_files, TRAIN_SET_NAME)
            end_train = time.process_time()
        end_validation = end_train
        end_test = end_train
        end_reading = end_train
        if self.valid > 0:
            self._read_input_file(self.validation_files, VALIDATION_SET_NAME)
            end_validation = time.process_time()
            end_reading = end_validation
        if self.test > 0:
            self._read_input_file(self.test_files, TEST_SET_NAME)
            end_test = time.process_time()
            end_reading = end_test
        self.neural_program.build_program()
        end_func = time.process_time()
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
        start_func = time.process_time()
        logger.info("Building model...")
        self.model = NeuralLogNetwork(self.neural_program, train=self.train)
        self.model.build_layers()
        self.output_map = self._get_output_map()
        self._read_parameters(self.output_map)
        self._log_parameters(
            ["loss_function", "optimizer", "regularizer", "metrics"],
            self.output_map.inverse
        )
        self.model.compile(
            loss=self.parameters["loss_function"],
            optimizer=self.parameters["optimizer"],
            regularizer=self.parameters["regularizer"],
            metrics=self.parameters["metrics"]
        )
        self.neural_dataset = NeuralLogDataset(self.model)
        end_func = time.process_time()

        logger.info("Model building time:\t%0.3fs", end_func - start_func)

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
            print_args(parameters)

    def fit(self):
        """
        Trains the neural network.
        """
        start_func = time.process_time()
        logger.info("Training the model...")
        self.batch_size = self.parameters["batch_size"]
        self.epochs = self.parameters["epochs"]
        self.validation_period = self.parameters["validation_period"]
        self._log_parameters(["batch_size", "epochs", "validation_period"])
        self.callbacks = self._get_callbacks()
        self._log_parameters(["callback"])
        history = self.model.fit(
            self.train_set,
            epochs=self.epochs,
            validation_data=self.validation_set,
            validation_freq=self.validation_period,
            callbacks=self.callbacks
        )
        end_func = time.process_time()
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
            callbacks.append(callback)

    def _adjust_config_for_callback(self, config, callback_class):
        config.setdefault("period", self.validation_period)
        if issubclass(callback_class, AbstractNeuralLogCallback):
            config["train_command"] = self
        elif issubclass(callback_class, tf.keras.callbacks.ModelCheckpoint):
            config.setdefault("save_best_only", True)
            config.setdefault("save_weights_only", True)
            has_no_filepath = "filepath" not in config
            config.setdefault("filepath", config["monitor"])
            if self.output_path is not None:
                suffix = config["filepath"]
                config["filepath"] = os.path.join(self.output_path, suffix)
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

    def _build_train_set(self):
        start_func = time.process_time()
        logger.info("Creating training dataset...")
        self.train_set = self.neural_dataset.get_dataset(TRAIN_SET_NAME)
        self.train_set = self.train_set.batch(self.batch_size)
        end_train = time.process_time()
        end_func = end_train
        if self.valid:
            self.validation_set = self.neural_dataset.get_dataset(
                VALIDATION_SET_NAME)
            self.validation_set = self.validation_set.batch(self.batch_size)
            end_func = time.process_time()
            logger.info("Train dataset creating time:      \t%0.3fs",
                        end_train - start_func)
            logger.info("Validation dataset creation time: \t%0.3fs",
                        end_func - end_train)
        logger.info("Total dataset creation time:      \t%0.3fs",
                    end_func - start_func)

    # noinspection PyMissingOrEmptyDocstring
    def run(self):
        self.build()
        if self.train:
            self._build_train_set()
            history = self.fit()
            if logger.isEnabledFor(logging.INFO):
                hist = history.history
                hist = dict(map(
                    lambda x: (get_formatted_name(
                        x[0], self.output_map.inverse), x[1]), hist.items()))
                logger.info(hist)

            if self.output_path is not None:
                logger.info("Saving the model...")
                filepath = os.path.join(self.output_path, self.model_name)
                self.model.save_weights(filepath)
                for metric in history.history:
                    array = np.array(history.history[metric])
                    # noinspection PyTypeChecker
                    metric = get_formatted_name(metric, self.output_map.inverse)
                    metric = METRIC_FILE_PREFIX + metric
                    metric_path = os.path.join(self.output_path,
                                               "{}.txt".format(metric))
                    np.savetxt(metric_path, array, fmt="%0.8f")

            if self.output_program is not None:
                self.model.update_program()
                output_program = os.path.join(
                    self.output_path, self.output_program)
                output = open(output_program, "w")
                print_neural_log_program(self.neural_program, output)
                output.close()

        if self.output_program is not None and self.save_predictions:
            if self.train:
                output = open(os.path.join(
                    self.output_path, TRAIN_PREDICTION_FILE), "w")
                print_neural_log_predictions(self.model, self.neural_program,
                                             self.train_set, writer=output)
                output.close()
            if self.valid:
                output = open(os.path.join(
                    self.output_path, VALIDATION_SET_NAME), "w")
                print_neural_log_predictions(self.model, self.neural_program,
                                             self.validation_set, writer=output)
                output.close()

            if self.test:
                output = open(os.path.join(
                    self.output_path, TEST_PREDICTION_FILE), "w")
                self.test_set = self.neural_dataset.get_dataset(TEST_SET_NAME)
                self.test_set = self.validation_set.batch(self.batch_size)
                print_neural_log_predictions(self.model, self.neural_program,
                                             self.test_set, writer=output)
                output.close()
