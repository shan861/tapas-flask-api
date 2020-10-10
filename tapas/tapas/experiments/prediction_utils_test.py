# coding=utf-8
# Copyright 2019 The Google AI Language Team Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Lint as: python3

import os

from absl import flags
from absl import logging
from absl.testing import absltest
from absl.testing import parameterized
import numpy as np
from tapas.datasets import table_dataset
from tapas.datasets import table_dataset_test_utils
from tapas.experiments import prediction_utils
from tapas.models import tapas_classifier_model
from tapas.models.bert import modeling
import tensorflow.compat.v1 as tf


FLAGS = flags.FLAGS

_SpanPredictionMode = tapas_classifier_model.SpanPredictionMode
_BATCH_SIZE = 8


class PredictionUtilsTest(tf.test.TestCase, parameterized.TestCase):

  def _predict_data(self):
    return os.path.join(
        'tapas'
        '/testdata/classification_examples.tfrecords')

  def test_read_classifier_dataset(self):
    examples_by_position = prediction_utils.read_classifier_dataset(
        predict_data=self._predict_data(),
        data_format='tfrecord',
        compression_type='',
        max_seq_length=512,
        max_predictions_per_seq=20,
        add_aggregation_function_id=False,
        add_classification_labels=False,
        add_answer=False)
    # Check that we loaded something.
    self.assertNotEmpty(examples_by_position)
    for examples_by_question_id in examples_by_position.values():
      self.assertNotEmpty(examples_by_question_id)
      for example in examples_by_question_id.values():
        # Test that at least some features are there.
        self.assertIn('input_ids', example)
        self.assertIn('label_ids', example)

  def _generator_kwargs(self):
    return dict(
        max_seq_length=10,
        max_predictions_per_seq=5,
        task_type=table_dataset.TableTask.CLASSIFICATION,
        add_aggregation_function_id=False,
        add_classification_labels=False,
        add_answer=False,
        include_id=False,
        vocab_size=10,
        segment_vocab_size=3,
        num_columns=3,
        num_rows=2,
        add_candidate_answers=False,
        max_num_candidates=10)

  def _create_estimator(
      self,
      span_prediction = _SpanPredictionMode.NONE,
  ):
    # Small bert model for testing.
    bert_config = modeling.BertConfig.from_dict({
        'vocab_size': 30522,
        'type_vocab_size': [3, 256, 256, 2, 256, 256, 10],
        'num_hidden_layers': 2,
        'num_attention_heads': 2,
        'hidden_size': 128,
        'intermediate_size': 512,
    })
    tapas_config = tapas_classifier_model.TapasClassifierConfig(
        bert_config=bert_config,
        init_checkpoint=None,
        learning_rate=0.0,
        num_train_steps=1,
        num_warmup_steps=1,
        use_tpu=False,
        positive_weight=1.0,
        num_aggregation_labels=0,
        num_classification_labels=0,
        aggregation_loss_importance=0.0,
        use_answer_as_supervision=False,
        answer_loss_importance=0.0,
        use_normalized_answer_loss=False,
        huber_loss_delta=0.0,
        temperature=1.0,
        agg_temperature=1.0,
        use_gumbel_for_cells=False,
        use_gumbel_for_agg=False,
        average_approximation_function='ratio',
        cell_select_pref=1.0,
        answer_loss_cutoff=0.0,
        grad_clipping=1.0,
        max_num_rows=64,
        max_num_columns=32,
        average_logits_per_cell=True,
        span_prediction=span_prediction,
        select_one_column=True)
    model_fn = tapas_classifier_model.model_fn_builder(tapas_config)

    estimator = tf.estimator.tpu.TPUEstimator(
        use_tpu=False,
        model_fn=model_fn,
        config=tf.estimator.tpu.RunConfig(model_dir=self.get_temp_dir()),
        train_batch_size=_BATCH_SIZE,
        predict_batch_size=_BATCH_SIZE,
        eval_batch_size=_BATCH_SIZE)

    return estimator

  def test_compute_prediction_sequence(self):
    """Tests that `compute_prediction_sequence` does not crash."""
    examples_by_position = prediction_utils.read_classifier_dataset(
        predict_data=self._predict_data(),
        data_format='tfrecord',
        compression_type='',
        max_seq_length=512,
        max_predictions_per_seq=20,
        add_aggregation_function_id=False,
        add_classification_labels=False,
        add_answer=False)

    # Make sure that for all examples there is a predecessor. This is always
    # true for the full data but the testing data is incomplete.
    for position in range(len(examples_by_position) - 1, 0, -1):
      for example_id in examples_by_position[position]:
        if example_id not in examples_by_position[position - 1]:
          example = examples_by_position[position][example_id]
          examples_by_position[position - 1][example_id] = example

    results = prediction_utils.compute_prediction_sequence(
        estimator=self._create_estimator(),
        examples_by_position=examples_by_position)
    self.assertNotEmpty(results)

  def test_span_selection(self):
    prediction = {
        'input_ids': np.array([1, 2, 3, 4]),
        'span_indexes': np.array([[1, 1], [1, 2], [2, 1]]),
        'span_logits': np.array([-100.0, 10.0, 5.0]),
    }
    answer = prediction_utils.get_answer_indexes(
        prediction, cell_classification_threshold=0.5)
    self.assertAllEqual(answer, [2, 3])

  def test_token_selection(self):
    prediction = {
        'input_ids': np.array([0, 1, 2, 3, 4]),
        'probabilities': np.array([1.0, 0.5, 0.2, 0.5, 0.3]),
        'column_ids': np.array([0, 1, 2, 3, 4]),
        'row_ids': np.array([0, 1, 1, 2, 2]),
        'segment_ids': np.array([0, 1, 1, 1, 1]),
    }
    answer = prediction_utils.get_answer_indexes(
        prediction, cell_classification_threshold=0.49999)
    logging.info(answer)
    self.assertAllEqual(answer, [1, 3])

  @parameterized.parameters(
      (_SpanPredictionMode.SPAN,),
      (_SpanPredictionMode.BOUNDARY,),
      (_SpanPredictionMode.NONE,),
  )
  def test_end_to_end(self, span_prediction):
    estimator = self._create_estimator(span_prediction=span_prediction)

    def _input_fn(params):
      return table_dataset_test_utils.create_random_dataset(
          num_examples=_BATCH_SIZE * 2,
          batch_size=params['batch_size'],
          repeat=False,
          generator_kwargs=self._generator_kwargs())

    result = estimator.predict(_input_fn)
    num_examples = 0
    for prediction in result:
      if span_prediction != _SpanPredictionMode.NONE:
        self.assertIn('span_logits', prediction)
        self.assertIn('span_indexes', prediction)
      logging.info('prediction: %s', prediction)
      _ = prediction_utils.get_answer_indexes(
          prediction,
          cell_classification_threshold=0.5,
      )
      num_examples += 1
    self.assertEqual(num_examples, _BATCH_SIZE * 2)


if __name__ == '__main__':
  absltest.main()
