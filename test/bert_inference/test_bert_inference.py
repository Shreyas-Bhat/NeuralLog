"""
Tests the BERT inference.
"""
import logging
import os
import unittest

import numpy as np

from src.knowledge.program import NeuralLogProgram
from src.language.language import Predicate
from src.language.parser.ply.neural_log_parser import NeuralLogLexer, \
    NeuralLogParser
from src.network.dataset import LanguageDataset
from src.network.trainer import Trainer
from src.run import configure_log

RESOURCE_PATH = os.path.dirname(os.path.realpath(__file__))

VOCABULARY_FILE = os.path.join(RESOURCE_PATH, "vocab.txt")

MODEL_FILE = os.path.join(RESOURCE_PATH, "model", "weights")

DATASET_NAME = "examples"


# noinspection PyMissingOrEmptyDocstring
def get_clauses(filepath):
    # PLY
    lexer = NeuralLogLexer()
    parser = NeuralLogParser(lexer)
    parser.parse(filepath)
    return parser.get_clauses()


class TestBertInference(unittest.TestCase):

    # noinspection PyMissingOrEmptyDocstring
    @classmethod
    def setUpClass(cls) -> None:
        configure_log(level=logging.DEBUG)

        program = get_clauses(os.path.join(RESOURCE_PATH, "program.pl"))
        theory = get_clauses(os.path.join(RESOURCE_PATH, "theory.pl"))
        examples = get_clauses(os.path.join(RESOURCE_PATH, "examples.pl"))

        cls.program = NeuralLogProgram()  # type: NeuralLogProgram
        cls.program.add_clauses(program)
        cls.program.add_clauses(theory)
        cls.program.add_clauses(examples, example_set=DATASET_NAME)
        cls.program.build_program()
        cls.program.parameters["dataset_class"]["config"]["vocabulary_file"] = \
            VOCABULARY_FILE
        cls.program.parameters[Predicate("bert", 2)][
            "function_value"]["config"]["model_path"] = RESOURCE_PATH
        cls.trainer = Trainer(cls.program, output_path=None)
        cls.trainer.init_model()
        cls.trainer.read_parameters()
        # noinspection PyTypeChecker
        cls.dataset: LanguageDataset = \
            cls.trainer.build_dataset(override_targets=False)
        target_predicates = cls.dataset.target_predicates
        cls.target_predicate = target_predicates[0][0]
        cls.trainer.model.build_layers(target_predicates)
        cls.trainer.compile_module()

    def test_bert_inference(self):
        batch_size = 2
        dataset = self.dataset.get_dataset(DATASET_NAME, batch_size=batch_size)
        self.trainer.model.load_weights(MODEL_FILE)
        output_size = self.program.get_constant_size(self.target_predicate, -1)
        maximum_sentence_length = self.dataset.maximum_sentence_length
        count = 0
        for features, _ in dataset:
            x = self.trainer.model.call(features)
            shape = x[0].shape
            for example in x[0]:
                expected = \
                    np.loadtxt(os.path.join(RESOURCE_PATH, f"{count}.txt"))
                self.assertTrue(np.allclose(expected, example),
                                "Inference values are not expected.")
                count += 1

            self.assertGreaterEqual(batch_size, shape[0])
            self.assertEqual(maximum_sentence_length, shape[1])
            self.assertEqual(output_size, shape[2])

    def test_bert_train(self):
        batch_size = 2
        dataset = self.dataset.get_dataset(DATASET_NAME, batch_size=batch_size)
        hist = self.trainer.fit(dataset)
        self.assertGreater(hist.history["loss"][0], hist.history["loss"][-1],
                           f"Loss:\t{hist.history['loss']}")
